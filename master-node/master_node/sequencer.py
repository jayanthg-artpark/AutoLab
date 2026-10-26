"""Executes planned runs step by step, one run at a time.

The lab has one plate mover and one of most instruments, so runs queue up and
execute strictly in order. Every state change is saved before the next device
command, so the API always shows where a run is.

If a step fails the run stops there and the remaining steps are skipped: the
plate stays wherever it is and a person decides what happens next.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Literal

from .devices import Lab, Location
from .devices.base import ProgressCallback
from .devices.manual import ManualThor
from .models import Measurement, ReadResult, Run, RunStatus, Step, StepStatus, utcnow
from .planner import decode_protocol_source
from .registry import Registry
from .store import RunStore
from .tips import RACK_SIZE, TipProblem, TipTracker, with_tip_state

log = logging.getLogger(__name__)

StepHandler = Callable[[Run, Step], Awaitable[str]]
MEASURING = ("read", "qpcr")


class InterlockError(Exception):
    """A safety precondition for the next device command was not met."""


class RunFinishedError(Exception):
    """The run has already finished, so it cannot be aborted."""


class Sequencer:
    def __init__(
        self, store: RunStore, lab: Lab, registry: Registry, tips: TipTracker | None = None, hold_scale: float = 1.0
    ):
        self._store = store
        self._lab = lab
        self._registry = registry
        self._tips = tips
        self._hold_scale = hold_scale  # tests shorten holds; always 1.0 in the lab
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._active: tuple[str, asyncio.Task[str]] | None = None
        self._abort_requested: set[str] = set()
        # Keyed by Step.key, as produced by planner.plan_run.
        self._handlers: dict[str, StepHandler] = {
            "transfer": self._transfer_plate,
            "clear": self._confirm_clear,
            "operation": self._operation,
            "hold": self._hold,
        }

    # --- Queue -----------------------------------------------------------------

    def submit(self, run: Run) -> None:
        self._store.save(run)
        self._queue.put_nowait(run.id)

    def recover(self) -> None:
        """Called once at startup, before run_forever.

        Runs that were mid-flight when the node stopped are marked interrupted:
        the plate's position is unknown, so a person must check before anything
        moves. Runs that never started are queued again.
        """
        for run in self._store.list(status=RunStatus.running, limit=1000):
            for step in run.steps:
                if step.status is StepStatus.running:
                    self._end_step(step, StepStatus.failed, "Interrupted: master node restarted")
            self._finish(run, RunStatus.interrupted, "Master node restarted mid-run; check plate location")
        for run in reversed(self._store.list(status=RunStatus.queued, limit=1000)):
            if any(s.key not in self._handlers for s in run.steps if s.status is StepStatus.pending):
                self._finish(run, RunStatus.failed, "Planned by an older version of the master node; send it again")
                continue
            self._queue.put_nowait(run.id)

    async def run_forever(self) -> None:
        while True:
            run_id = await self._queue.get()
            try:
                await self._execute(run_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Unexpected error while executing run %s", run_id)
                run = self._store.get(run_id)
                if run and not run.status.finished:
                    self._finish(run, RunStatus.failed, "Internal error in master node; see logs")

    def confirm_plate_moved(self, run_id: str) -> bool:
        """A person moved the plate the run is waiting for. False if it wasn't waiting."""
        run = self._store.get(run_id)
        if run is None:
            raise KeyError(run_id)
        move = run.context.get("manual_move")
        transport = self._lab.transport
        return bool(move and isinstance(transport, ManualThor) and transport.confirm(move["task_id"]))

    def request_abort(self, run_id: str) -> Run:
        """Raises KeyError if the run does not exist, RunFinishedError if it has finished."""
        run = self._store.get(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.status.finished:
            raise RunFinishedError(run_id)
        if run.status is RunStatus.queued:
            self._finish(run, RunStatus.aborted, "Aborted before it started")
            return run
        self._abort_requested.add(run_id)
        if self._active and self._active[0] == run_id:
            self._active[1].cancel()
        return run

    # --- Execution -------------------------------------------------------------

    async def _execute(self, run_id: str) -> None:
        run = self._store.get(run_id)
        if run is None or run.status is not RunStatus.queued:
            return  # aborted while queued
        run.status = RunStatus.running
        self._event(run, "Run started")

        try:
            await self._preflight(run)
        except Exception as exc:
            self._finish(run, RunStatus.failed, f"Preflight check failed: {exc}")
            return

        for step in run.steps:
            if step.status is not StepStatus.pending:
                continue
            if run.id in self._abort_requested:
                await self._stop_in_flight(run)
                self._finish(run, RunStatus.aborted, "Aborted by user; check plate location")
                return
            if not await self._run_step(run, step):
                return
        self._finish(run, RunStatus.succeeded)

    async def _run_step(self, run: Run, step: Step) -> bool:
        """Runs one step. Returns False if the run ended (failed or aborted)."""
        step.status = StepStatus.running
        step.started_at = utcnow()
        self._event(run, f"{step.label}...")

        task = asyncio.create_task(self._handlers[step.key](run, step))
        self._active = (run.id, task)
        try:
            detail = await task
        except asyncio.CancelledError:
            if run.id not in self._abort_requested:
                # The master node itself is shutting down. Leave devices alone.
                self._end_step(step, StepStatus.failed, "Interrupted: master node shut down")
                self._finish(run, RunStatus.interrupted, "Master node shut down mid-run; check plate location")
                raise
            self._end_step(step, StepStatus.failed, "Aborted")
            await self._stop_in_flight(run)
            self._finish(run, RunStatus.aborted, f"Aborted during '{step.label}'; check plate location")
            return False
        except Exception as exc:
            log.warning("Run %s: %s failed: %s", run.id, step.label, exc)
            self._end_step(step, StepStatus.failed, str(exc))
            self._finish(run, RunStatus.failed, f"{step.label}: {exc}")
            return False
        finally:
            self._active = None

        self._end_step(step, StepStatus.done, detail)
        self._event(run, f"{step.label}: {detail}")
        return True

    async def _preflight(self, run: Run) -> None:
        checked = []
        if any(s.key == "transfer" for s in run.steps):
            transport = self._transport()
            status = await transport.status()
            if not status.clear:
                raise InterlockError(
                    f"{self._name(self._registry.transport.id)} is not parked and empty-handed "
                    f"(at_home={status.at_home}, holding_plate={status.holding_plate})"
                )
            checked.append(f"{self._name(self._registry.transport.id)} home")
        for instrument_id in dict.fromkeys(s.device for s in run.steps if s.key == "operation"):
            await self._instrument(instrument_id).check_ready()
            checked.append(f"{self._name(instrument_id)} ready")
        if self._tips:
            # Later protocols in the same run continue where the earlier ones leave the racks.
            used: dict[str, int] = {}
            for op in run.protocol.steps:
                if op.type == "liquid_handling" and op.tips:
                    try:
                        _, after = self._tips.plan(op.tips, used)
                    except TipProblem as exc:
                        raise InterlockError(str(exc)) from exc
                    used.update({slot: count for slot, (_, count) in after.items()})
            if used:
                checked.append("enough tips")
        self._event(run, "Preflight OK: " + ", ".join(checked or ["nothing to check"]))

    async def _stop_in_flight(self, run: Run) -> None:
        task_id = run.context.pop("transport_task_id", None)
        if task_id is not None and self._lab.transport:
            await self._send_stop(run, self._name(self._registry.transport.id), task_id, self._lab.transport.cancel)
        job = run.context.pop("job", None)
        if job is not None:
            await self._send_stop(run, self._name(job["instrument"]), job["handle"], self._instrument(job["instrument"]).cancel)

    async def _send_stop(self, run: Run, device: str, handle: str, stop: Callable[[str], Awaitable[None]]) -> None:
        try:
            await stop(handle)
            self._event(run, f"Sent stop to {device} ({handle})", "warning")
        except Exception as exc:
            self._event(run, f"Could not stop {device} ({handle}): {exc}", "error")

    # --- Step handlers -----------------------------------------------------------
    # Each returns a short detail line for the step. Handles for device work in
    # flight go into run.context so an abort can stop them, and are removed once
    # that work has finished.

    async def _transfer_plate(self, run: Run, step: Step) -> str:
        src, dst = Location(**step.params["from"]), Location(**step.params["to"])
        for instrument_id in step.params.get("access", []):
            # Logged, so a run that fails mid-transfer shows a drawer left open on purpose.
            if done := await self._instrument(instrument_id).open_access():
                self._event(run, f"{self._name(instrument_id)}: {done}")
        transport = self._transport()
        task_id = await transport.start_transfer(src, dst, run.protocol.plate.barcode)
        run.context["transport_task_id"] = task_id
        manual = isinstance(transport, ManualThor)
        if manual:
            # Shown on the Runs tab with a "Plate moved" button (see confirm_plate_moved).
            run.context["manual_move"] = {"task_id": task_id, "from": src.as_dict(), "to": dst.as_dict()}
        mover = self._name(step.device)
        self._event(run, f"{'Move by hand' if manual else f'{mover} task {task_id}'}: {src} -> {dst}")
        try:
            await transport.wait_transfer(task_id, self._progress(run, mover))
        finally:
            run.context.pop("manual_move", None)
        run.context.pop("transport_task_id")
        return f"Plate placed at {dst} by hand" if manual else f"Plate placed at {dst}; {mover} reports home"

    async def _confirm_clear(self, run: Run, step: Step) -> str:
        entry = self._registry.transport
        station = self._name(step.params["station"])
        deadline = time.monotonic() + entry.clear_timeout_s
        while True:
            status = await self._transport().status()
            if status.clear:
                break
            if time.monotonic() >= deadline:
                raise InterlockError(
                    f"{entry.name} did not clear the {station} (at_home={status.at_home}, "
                    f"holding_plate={status.holding_plate})"
                )
            await asyncio.sleep(entry.poll_interval_s)
        # Only now that nothing is in the way: close drawers and lids again.
        for instrument_id in step.params.get("access", []):
            if done := await self._instrument(instrument_id).close_access():
                self._event(run, f"{self._name(instrument_id)}: {done}")
        return f"{entry.name} is home and empty-handed; {station} is clear"

    async def _operation(self, run: Run, step: Step) -> str:
        op = run.protocol.steps[step.params["op"]]
        instrument = self._instrument(step.device)
        name = self._name(step.device)
        plate = {"barcode": run.protocol.plate.barcode, "labware": run.protocol.plate.labware, "slot": step.params["slot"]}

        after = None
        if op.type == "liquid_handling":
            source = decode_protocol_source(op.protocol_b64)
            if self._tips and op.tips:
                # Start each pipette at its rack's first unused tip (see tips.py).
                before, after = self._tips.plan(op.tips)
                source = with_tip_state(source, before)
            params: dict[str, Any] = {
                "protocol_filename": op.protocol_filename, "source": source, "runtime_parameters": op.runtime_parameters,
            }
        elif op.type == "read":
            params = op.settings.model_dump()
        else:
            params = op.model_dump(exclude={"type", "instrument", "label"})

        handle = await instrument.start(op.type, params, plate)
        run.context["job"] = {"instrument": step.device, "handle": handle}
        self._event(run, f"{name} started {handle}: {step.params['summary']}")
        if after:
            # Count the tips as used now: if the run fails part-way, a few unused tips
            # are written off rather than a used tip being picked up again.
            self._tips.commit(after)
            left = ", ".join(f"{slot} {RACK_SIZE - used} left" for slot, (_, used) in sorted(after.items()))
            self._event(run, f"Tip racks after this protocol: {left}")
        output = await instrument.wait(handle, self._progress(run, name))
        run.context.pop("job")
        return self._keep_output(run, step, output) or f"{name} finished {handle}"

    async def _hold(self, run: Run, step: Step) -> str:
        await asyncio.sleep(step.params["seconds"] * self._hold_scale)
        return f"Held {step.params['summary']}"

    # --- Helpers -----------------------------------------------------------------

    def _keep_output(self, run: Run, step: Step, output: dict[str, Any]) -> str:
        """Saves what an instrument returned; returns a detail line if it returned measurements."""
        output = dict(output or {})
        raw = output.pop("measurements", None) or []
        if output:
            run.outputs[step.id] = output
        if not raw:
            return ""
        measurements = [Measurement.model_validate(m) for m in raw]
        measuring_steps = sum(1 for op in run.protocol.steps if op.type in MEASURING)
        if measuring_steps > 1:
            n = step.params["op"] + 1
            for m in measurements:
                m.label = f"Step {n} {step.label}: {m.label}"
        if run.results is None:
            run.results = ReadResult(measurements=[], metadata={})
        run.results.measurements += measurements
        run.results.metadata.update(output.get("metadata") or {})
        count = sum(len(m.values) for m in measurements)
        return f"Stored {count} values from {self._name(step.device)}"

    def _transport(self):
        if self._lab.transport is None:
            raise InterlockError("No transport in the registry")
        return self._lab.transport

    def _instrument(self, instrument_id: str):
        try:
            return self._lab.instruments[instrument_id]
        except KeyError:
            raise InterlockError(f"Instrument '{instrument_id}' is no longer in the registry") from None

    def _name(self, instrument_id: str) -> str:
        entry = self._registry.get(instrument_id)
        return entry.name if entry else instrument_id

    def _progress(self, run: Run, device: str) -> ProgressCallback:
        return lambda state: self._event(run, f"{device}: {state}")

    def _event(
        self, run: Run, message: str, level: Literal["info", "warning", "error"] = "info"
    ) -> None:
        run.add_event(message, level)
        log.log(
            {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}[level],
            "Run %s: %s", run.id, message,
        )
        self._store.save(run)

    @staticmethod
    def _end_step(step: Step, status: StepStatus, detail: str) -> None:
        step.status = status
        step.finished_at = utcnow()
        step.detail = detail

    def _finish(self, run: Run, status: RunStatus, error: str = "") -> None:
        run.status = status
        run.error = error
        for step in run.steps:
            if step.status is StepStatus.pending:
                step.status = StepStatus.skipped
        self._abort_requested.discard(run.id)
        if status is RunStatus.succeeded:
            self._event(run, "Run succeeded")
        else:
            self._event(run, f"Run {status.value}: {error}", "error")
