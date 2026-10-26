"""HTTP API the protocol tool talks to, and the process entry point."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import secrets
from contextlib import asynccontextmanager, suppress
from typing import Any

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .config import Settings, load_settings
from .devices import DeviceError, Lab, build_lab
from .devices.manual import ManualThor
from .models import ProtocolSubmission, Run, RunStatus, new_run_id, well_sort_key
from .planner import PlanError, plan_run
from .registry import Registry, load_registry
from .sequencer import RunFinishedError, Sequencer
from .store import RunStore
from .tips import TipTracker

log = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 5.0  # per instrument, for the status bar


def create_app(
    settings: Settings | None = None,
    lab: Lab | None = None,
    registry: Registry | None = None,
    hold_scale: float = 1.0,
) -> FastAPI:
    settings = settings or load_settings()
    registry = registry or load_registry(settings.server.instruments)
    store = RunStore(settings.server.database)
    tips = TipTracker(settings.server.database)
    lab = lab or build_lab(registry, settings.sim.time_scale)
    sequencer = Sequencer(store, lab, registry, tips, hold_scale=hold_scale)
    drivers = {i.id: i.driver for i in registry.instruments}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sequencer.recover()
        worker = asyncio.create_task(sequencer.run_forever(), name="sequencer")
        log.info("Master node ready (%s)", ", ".join(f"{k}={v}" for k, v in drivers.items()))
        try:
            yield
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            await lab.aclose()
            store.close()
            tips.close()

    async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
        expected = settings.server.api_key
        if expected and not secrets.compare_digest((x_api_key or "").encode(), expected.encode()):
            raise HTTPException(401, "Missing or wrong X-API-Key header")

    app = FastAPI(title="Master node", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.server.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    api = APIRouter(dependencies=[Depends(require_api_key)])

    def get_run_or_404(run_id: str) -> Run:
        run = store.get(run_id)
        if run is None:
            raise HTTPException(404, f"No run {run_id}")
        return run

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "modes": drivers}

    @api.get("/instruments")
    async def instruments() -> dict[str, Any]:
        """The instrument registry: what each instrument is, where the plate goes on it, and its limits."""
        return {"instruments": registry.view()}

    @api.get("/devices")
    async def device_status() -> dict[str, Any]:
        """Live readiness of each instrument, for the operator's status bar."""

        async def probe(check) -> dict[str, Any]:
            # Bounded, so one instrument that is off doesn't stall the whole status bar.
            try:
                return {"ok": True, "detail": await asyncio.wait_for(check(), PROBE_TIMEOUT_S) or "ready"}
            except asyncio.TimeoutError:
                return {"ok": False, "detail": f"no answer within {PROBE_TIMEOUT_S:.0f} s (switched off or off the network?)"}
            except Exception as exc:
                return {"ok": False, "detail": str(exc)}

        async def transport_check() -> str:
            status = await lab.transport.status()
            if not status.clear:
                raise RuntimeError("away from home" if not status.at_home else "holding a plate")
            return status.message if isinstance(lab.transport, ManualThor) else "home, empty-handed"

        checks = {i.id: lab.instruments[i.id].check_ready for i in registry.instruments if i.kind != "transport"}
        if registry.transport and lab.transport:
            checks = {registry.transport.id: transport_check, **checks}
        results = await asyncio.gather(*(probe(check) for check in checks.values()))
        status = {}
        for instrument_id, result in zip(checks, results):
            entry = registry.get(instrument_id)
            status[instrument_id] = {**result, "mode": entry.driver, "name": entry.name, "kind": entry.kind}
        return status

    @api.post("/transport/reset")
    async def reset_transport() -> dict[str, Any]:
        """A person checked Thor is parked and empty-handed after an aborted or failed move, and says so."""
        if lab.transport is None:
            raise HTTPException(404, "This lab has no transport")
        if running := store.list(status=RunStatus.running, limit=1):
            raise HTTPException(409, f"Run {running[0].id} is still running; abort it first")
        try:
            status = await lab.transport.reset()
        except DeviceError as exc:
            raise HTTPException(502, str(exc)) from exc
        log.info("Transport reset by a person: %s", status.message)
        return {"at_home": status.at_home, "holding_plate": status.holding_plate, "detail": status.message}

    @api.post("/plan")
    async def preview_plan(submission: ProtocolSubmission) -> dict[str, Any]:
        """Selects instruments and checks limits without queuing anything: what /runs would do."""
        try:
            steps, warnings = plan_run(submission, registry)
        except PlanError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"steps": [s.model_dump(mode="json") for s in steps], "warnings": warnings}

    @api.post("/runs", status_code=201)
    async def submit_run(submission: ProtocolSubmission) -> dict[str, Any]:
        try:
            steps, warnings = plan_run(submission, registry)
        except PlanError as exc:
            raise HTTPException(422, str(exc)) from exc
        run = Run(id=new_run_id(), protocol=submission, steps=steps)
        run.add_event(f"Received '{submission.name}' from {submission.scientist}")
        for warning in warnings:
            run.add_event(warning, "warning")
        sequencer.submit(run)
        return run_view(run)

    @api.get("/tips")
    async def tip_racks() -> dict[str, Any]:
        """Tip racks on the Flex and how many tips each has left."""
        return {"racks": [rack.view() for rack in tips.racks()]}

    @api.post("/tips/{slot}/fresh")
    async def fresh_tip_rack(slot: str, load_name: str | None = None) -> dict[str, Any]:
        """Staff put a full rack in this slot."""
        rack = tips.mark_fresh(slot, load_name)
        if rack is None:
            raise HTTPException(404, f"No tip rack recorded in {slot}; give load_name for a new one")
        return rack.view()

    @api.delete("/tips/{slot}")
    async def remove_tip_rack(slot: str) -> dict[str, Any]:
        """Staff took the rack in this slot off the deck."""
        if not tips.remove(slot):
            raise HTTPException(404, f"No tip rack recorded in {slot}")
        return {"removed": slot}

    @api.get("/runs")
    async def list_runs(limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
        return [run_summary(run) for run in store.list(limit=limit)]

    @api.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        return run_view(get_run_or_404(run_id))

    @api.post("/runs/{run_id}/plate-moved", status_code=202)
    async def plate_moved(run_id: str) -> dict[str, Any]:
        """A person confirms they moved the plate (Thor in manual mode)."""
        try:
            confirmed = sequencer.confirm_plate_moved(run_id)
        except KeyError:
            raise HTTPException(404, f"No run {run_id}") from None
        if not confirmed:
            raise HTTPException(409, f"Run {run_id} is not waiting for a plate to be moved")
        return {"confirmed": True}

    @api.post("/runs/{run_id}/abort", status_code=202)
    async def abort_run(run_id: str) -> dict[str, Any]:
        try:
            return run_view(sequencer.request_abort(run_id))
        except KeyError:
            raise HTTPException(404, f"No run {run_id}") from None
        except RunFinishedError:
            raise HTTPException(409, f"Run {run_id} has already finished") from None

    @api.get("/runs/{run_id}/results")
    async def get_results(run_id: str) -> dict[str, Any]:
        run = get_run_or_404(run_id)
        if run.results is None:
            raise HTTPException(404, f"Run {run_id} has no results yet")
        return run.results.model_dump()

    @api.get("/runs/{run_id}/results.csv")
    async def get_results_csv(run_id: str) -> Response:
        run = get_run_or_404(run_id)
        if run.results is None:
            raise HTTPException(404, f"Run {run_id} has no results yet")
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["run_id", "plate", "well", "measurement", "unit", "value"])
        for m in run.results.measurements:
            for well in sorted(m.values, key=well_sort_key):
                writer.writerow([run.id, run.protocol.plate.barcode, well, m.label, m.unit, m.values[well]])
        return Response(
            out.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{run.id}.csv"'},
        )

    app.include_router(api)
    return app


def run_view(run: Run) -> dict[str, Any]:
    """A run as the API returns it: without the protocol file bodies or the readings."""
    view = run.model_dump(mode="json", exclude={"results": True})
    for step in view["protocol"]["steps"]:
        step.pop("protocol_b64", None)
    view["has_results"] = run.results is not None
    return view


def run_summary(run: Run) -> dict[str, Any]:
    step = run.current_step
    return {
        "id": run.id,
        "name": run.protocol.name,
        "scientist": run.protocol.scientist,
        "status": run.status.value,
        "current_step": step.label if step else None,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
        "error": run.error,
    }



def serve() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    uvicorn.run(create_app(settings), host=settings.server.host, port=settings.server.port)
