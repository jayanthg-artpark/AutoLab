"""Simulated devices, so the whole sequence runs without any hardware.

Delays are roughly real-world seconds multiplied by time_scale.
"""

from __future__ import annotations

import asyncio
import itertools
import random
from typing import Any

from .base import DeviceError, Location, NoAccess, ProgressCallback, ThorStatus


class _Sim:
    def __init__(self, time_scale: float = 1.0):
        self._time_scale = time_scale
        self._ids = itertools.count(1)

    async def _sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds * self._time_scale)

    async def aclose(self) -> None:
        pass


def _series(wells: list[str], low: float, high: float, seed: str) -> dict[str, float]:
    """A two-fold dilution series across the columns, with some noise."""
    rng = random.Random(seed or "sim")
    return {
        well: round(low + (high - low) * 0.6 ** (int(well[1:]) - 1) * rng.uniform(0.92, 1.08), 4)
        for well in wells
    }


ALL_96 = [f"{row}{col}" for row in "ABCDEFGH" for col in range(1, 13)]


class SimThor(_Sim):
    def __init__(self, time_scale: float = 1.0):
        super().__init__(time_scale)
        self._at_home = True
        self._holding = False
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self.transfers: list[tuple[Location, Location]] = []

    async def status(self) -> ThorStatus:
        return ThorStatus(at_home=self._at_home, holding_plate=self._holding, message="simulated")

    async def start_transfer(self, src: Location, dst: Location, plate_id: str) -> str:
        if not (self._at_home and not self._holding):
            raise DeviceError("Thor is busy")
        task_id = f"thor-{next(self._ids)}"
        self._tasks[task_id] = asyncio.create_task(self._transfer(src, dst))
        return task_id

    async def _transfer(self, src: Location, dst: Location) -> None:
        self._at_home = False
        await self._sleep(4)  # drive to the source and pick
        self._holding = True
        await self._sleep(4)  # drive to the destination and place
        self._holding = False
        self.transfers.append((src, dst))
        await self._sleep(2)  # return home
        self._at_home = True

    async def wait_transfer(self, task_id: str, on_progress: ProgressCallback | None = None) -> None:
        await self._tasks[task_id]

    async def cancel(self, task_id: str) -> None:
        self._tasks[task_id].cancel()
        # Stand-in for an operator recovering the robot.
        self._at_home, self._holding = True, False

    async def reset(self) -> ThorStatus:
        self._at_home, self._holding = True, False
        return await self.status()


class SimFlex(NoAccess, _Sim):
    def __init__(self, time_scale: float = 1.0, fail_runs: bool = False):
        super().__init__(time_scale)
        self.fail_runs = fail_runs
        self._runs: dict[str, asyncio.Task[None]] = {}
        self.stopped: list[str] = []

    async def start(self, operation: str, params: dict[str, Any], plate: dict[str, Any]) -> str:
        if operation != "liquid_handling":
            raise DeviceError(f"The Flex cannot do '{operation}'")
        return await self.start_run(params["protocol_filename"], params["source"], params.get("runtime_parameters") or {})

    async def wait(self, handle: str, on_progress: ProgressCallback | None = None) -> dict[str, Any]:
        await self.wait_run(handle, on_progress)
        return {}

    async def cancel(self, handle: str) -> None:
        await self.stop(handle)

    async def check_ready(self) -> None:
        busy = [run_id for run_id, task in self._runs.items() if not task.done()]
        if busy:
            raise DeviceError(f"Flex is busy with run {busy[0]}")

    async def start_run(
        self, filename: str, source: bytes, runtime_parameters: dict[str, Any]
    ) -> str:
        if b"def run(" not in source:
            raise DeviceError("Flex rejected the protocol during analysis: no run() function")
        await self._sleep(3)  # upload + analysis
        run_id = f"flex-{next(self._ids)}"
        self._runs[run_id] = asyncio.create_task(self._run())
        return run_id

    async def _run(self) -> None:
        await self._sleep(20)
        if self.fail_runs:
            raise DeviceError("simulated failure: no tip detected")

    async def wait_run(self, run_id: str, on_progress: ProgressCallback | None = None) -> None:
        if on_progress:
            on_progress("running")
        try:
            await self._runs[run_id]
        except DeviceError as exc:
            if on_progress:
                on_progress("failed")
            raise DeviceError(f"Flex run {run_id} failed: {exc}") from exc
        if on_progress:
            on_progress("succeeded")

    async def stop(self, run_id: str) -> None:
        self._runs[run_id].cancel()
        self.stopped.append(run_id)


class SimTecan(_Sim):
    def __init__(self, time_scale: float = 1.0):
        super().__init__(time_scale)
        self.tray_open = False
        self._reads: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self.aborted: list[str] = []

    async def check_ready(self) -> None:
        pass

    async def open_access(self) -> str | None:
        await self.open_tray()
        return "drawer opened"

    async def close_access(self) -> str | None:
        await self.close_tray()
        return "drawer closed"

    async def start(self, operation: str, params: dict[str, Any], plate: dict[str, Any]) -> str:
        if operation != "read":
            raise DeviceError(f"The Tecan reader cannot do '{operation}'")
        return await self.start_read(params, plate.get("barcode", ""), plate.get("labware", ""))

    async def wait(self, handle: str, on_progress: ProgressCallback | None = None) -> dict[str, Any]:
        return await self.wait_read(handle, on_progress)

    async def cancel(self, handle: str) -> None:
        await self.abort(handle)

    async def open_tray(self) -> None:
        await self._sleep(2)
        self.tray_open = True

    async def close_tray(self) -> None:
        await self._sleep(2)
        self.tray_open = False

    async def start_read(self, read: dict[str, Any], plate_id: str, labware: str) -> str:
        if self.tray_open:
            raise DeviceError("Tecan tray is still open")
        read_id = f"read-{next(self._ids)}"
        self._reads[read_id] = asyncio.create_task(self._read(read, plate_id))
        return read_id

    async def _read(self, read: dict[str, Any], plate_id: str) -> dict[str, Any]:
        await self._sleep(15)

        def series(low: float, high: float) -> dict[str, float]:
            return _series(read["wells"], low, high, plate_id)

        if read["mode"] == "absorbance":
            measurements = [
                {"label": f"Absorbance {nm} nm", "unit": "OD", "values": series(0.04, 1.8)}
                for nm in read["wavelengths_nm"] or [600]
            ]
        elif read["mode"] == "fluorescence":
            label = f"Fluorescence Ex {read['excitation_nm']} / Em {read['emission_nm']} nm"
            measurements = [{"label": label, "unit": "RFU", "values": series(120, 48000)}]
        else:
            measurements = [{"label": "Luminescence", "unit": "RLU", "values": series(50, 250000)}]
        return {"measurements": measurements, "metadata": {"plate_id": plate_id, "simulated": True}}

    async def wait_read(
        self, read_id: str, on_progress: ProgressCallback | None = None
    ) -> dict[str, Any]:
        return await self._reads[read_id]

    async def abort(self, read_id: str) -> None:
        self._reads[read_id].cancel()
        self.aborted.append(read_id)


class SimInstrument(NoAccess, _Sim):
    """A simulated job_api instrument (washer, qPCR, ...): each job takes a while, then succeeds."""

    DURATION_S = {"wash": 10, "qpcr": 30}

    def __init__(self, time_scale: float = 1.0, operations: tuple[str, ...] = ()):
        super().__init__(time_scale)
        self._operations = operations
        self._jobs: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self.jobs: list[tuple[str, dict[str, Any]]] = []
        self.cancelled: list[str] = []

    async def check_ready(self) -> None:
        busy = [job_id for job_id, task in self._jobs.items() if not task.done()]
        if busy:
            raise DeviceError(f"busy with job {busy[0]}")

    async def start(self, operation: str, params: dict[str, Any], plate: dict[str, Any]) -> str:
        if self._operations and operation not in self._operations:
            raise DeviceError(f"cannot do '{operation}'")
        job_id = f"job-{next(self._ids)}"
        self.jobs.append((operation, params))
        self._jobs[job_id] = asyncio.create_task(self._job(operation, plate))
        return job_id

    async def _job(self, operation: str, plate: dict[str, Any]) -> dict[str, Any]:
        await self._sleep(self.DURATION_S.get(operation, 5))
        if operation == "qpcr":
            # Ct values: more template (left columns) crosses the threshold sooner.
            ct = {well: round(40 - v, 2) for well, v in _series(ALL_96, 14, 26, plate.get("barcode", "")).items()}
            return {
                "measurements": [{"label": "Ct", "unit": "cycles", "values": ct}],
                "metadata": {"simulated": True},
            }
        return {"metadata": {"simulated": True}}

    async def wait(self, handle: str, on_progress: ProgressCallback | None = None) -> dict[str, Any]:
        if on_progress:
            on_progress("running")
        return await self._jobs[handle]

    async def cancel(self, handle: str) -> None:
        self._jobs[handle].cancel()
        self.cancelled.append(handle)
