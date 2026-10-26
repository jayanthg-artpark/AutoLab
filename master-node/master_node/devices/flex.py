"""Opentrons Flex over the robot's HTTP API (port 31950).

For each run: upload the protocol (POST /protocols), wait for the robot's own
analysis of it, create a run from it (POST /runs), play it, poll the run until
it ends, then dismiss it so the next run can be created.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from ..registry import Instrument
from .base import DeviceError, NoAccess, ProgressCallback
from .http import HttpDevice, poll

log = logging.getLogger(__name__)

# Run states after which the Flex will not move again on its own. Anything
# else (paused, blocked-by-open-door, awaiting-recovery...) keeps us waiting.
TERMINAL = {"succeeded", "failed", "stopped"}
ANALYSIS_TIMEOUT_S = 300.0


class FlexHttp(NoAccess, HttpDevice):
    name = "Flex"

    def __init__(self, settings: Instrument, transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(
            settings.url,
            settings.request_timeout_s,
            # Value of the Opentrons-Version header; "*" means the robot's latest.
            headers={"Opentrons-Version": str(settings.options.get("opentrons_version", "*"))},
            transport=transport,
        )
        self.name = settings.name
        self._settings = settings

    # --- Instrument interface ----------------------------------------------------------

    async def start(self, operation: str, params: dict[str, Any], plate: dict[str, Any]) -> str:
        if operation != "liquid_handling":
            raise DeviceError(f"{self.name} cannot do '{operation}'")
        return await self.start_run(params["protocol_filename"], params["source"], params.get("runtime_parameters") or {})

    async def wait(self, handle: str, on_progress: ProgressCallback | None = None) -> dict[str, Any]:
        await self.wait_run(handle, on_progress)
        return {}

    async def cancel(self, handle: str) -> None:
        await self.stop(handle)

    # --- Opentrons robot-server API ------------------------------------------------------

    async def check_ready(self) -> None:
        await self._request("GET", "/health")
        runs = await self._request("GET", "/runs")
        for run in runs.get("data", []):
            if run.get("current") and run.get("status") not in TERMINAL:
                raise DeviceError(f"Flex is busy with run {run.get('id')} ({run.get('status')})")

    async def start_run(
        self, filename: str, source: bytes, runtime_parameters: dict[str, Any]
    ) -> str:
        form = {"runTimeParameterValues": json.dumps(runtime_parameters)} if runtime_parameters else None
        uploaded = await self._request(
            "POST", "/protocols", files={"files": (filename, source, "text/x-python")}, data=form
        )
        protocol = uploaded["data"]
        await self._wait_for_analysis(protocol)

        body: dict[str, Any] = {"protocolId": protocol["id"]}
        if runtime_parameters:
            body["runTimeParameterValues"] = runtime_parameters
        run_id = (await self._request("POST", "/runs", json={"data": body}))["data"]["id"]
        try:
            await self._request(
                "POST", f"/runs/{run_id}/actions", json={"data": {"actionType": "play"}}
            )
        except DeviceError:
            await self._dismiss(run_id)
            raise
        return run_id

    async def wait_run(self, run_id: str, on_progress: ProgressCallback | None = None) -> None:
        run = await poll(
            lambda: self._request("GET", f"/runs/{run_id}"),
            lambda d: d["data"]["status"],
            TERMINAL,
            interval_s=self._settings.poll_interval_s,
            timeout_s=self._settings.timeout_s,
            what=f"Flex run {run_id}",
            on_progress=on_progress,
        )
        await self._dismiss(run_id)
        data = run["data"]
        if data["status"] != "succeeded":
            errors = "; ".join(e.get("detail", "") for e in data.get("errors") or [])
            raise DeviceError(f"Flex run {run_id} {data['status']}" + (f": {errors}" if errors else ""))

    async def stop(self, run_id: str) -> None:
        await self._request("POST", f"/runs/{run_id}/actions", json={"data": {"actionType": "stop"}})

    async def _wait_for_analysis(self, protocol: dict[str, Any]) -> None:
        summaries = protocol.get("analysisSummaries") or []
        if not summaries:
            return
        path = f"/protocols/{protocol['id']}/analyses/{summaries[-1]['id']}"
        analysis = await poll(
            lambda: self._request("GET", path),
            lambda d: d["data"].get("status", "unknown"),
            {"completed"},
            interval_s=min(self._settings.poll_interval_s, 1.0),
            timeout_s=ANALYSIS_TIMEOUT_S,
            what="Flex protocol analysis",
        )
        result = analysis["data"].get("result")
        if result and result != "ok":
            errors = analysis["data"].get("errors") or []
            detail = "; ".join(e.get("detail", "") for e in errors) or result
            raise DeviceError(f"Flex rejected the protocol during analysis: {detail}")

    async def _dismiss(self, run_id: str) -> None:
        """Clears the run as the robot's current run so a new one can be created."""
        try:
            await self._request("PATCH", f"/runs/{run_id}", json={"data": {"current": False}})
        except DeviceError as exc:
            log.warning("Could not dismiss Flex run %s: %s", run_id, exc)
