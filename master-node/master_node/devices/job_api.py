"""Instrument servers that follow the lab's job API (see INSTRUMENT_API.md).

One contract for every instrument server (washer, qPCR, Thor, and any added
later), so a new instrument needs a registry entry, not master node code:

  GET  /health                 -> {"ready": bool, "detail": str}
                                  a transport also reports "at_home" and "holding_plate"
  POST /jobs                   <- {"operation": str, "params": {...},
                                   "plate": {"barcode": str, "labware": str, "slot": str}}
                               -> {"job_id": str}
  GET  /jobs/{job_id}          -> {"state": "queued|running|succeeded|failed|cancelled",
                                   "message": str, "result": {...}}
  POST /jobs/{job_id}/cancel
  POST /access/open            only when the registry entry sets options.access = true
  POST /access/close             (a drawer or lid the plate goes through)
  POST /reset                  transport only: a person checked the robot is home and
                                 empty-handed after an aborted or failed move

A job must only report "succeeded" once the instrument is done with the plate;
for a transfer, once the plate is placed and the robot is back home.

Every request carries the registry entry's api_key as X-API-Key when one is set.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from ..registry import Instrument
from .base import DeviceError, Location, ProgressCallback, ThorStatus
from .http import HttpDevice, key_header, poll

TERMINAL = {"succeeded", "failed", "cancelled"}


class _JobServer(HttpDevice):
    def __init__(self, settings: Instrument, transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(
            settings.url, settings.request_timeout_s, headers=key_header(settings.api_key), transport=transport
        )
        self.name = settings.name
        self._settings = settings

    async def _health(self) -> dict[str, Any]:
        data = await self._request("GET", "/health")
        if not isinstance(data, dict):
            raise DeviceError(f"{self.name}: /health returned {data!r}")
        return data

    async def _start_job(self, operation: str, params: dict[str, Any], plate: dict[str, Any]) -> str:
        data = await self._request("POST", "/jobs", json={"operation": operation, "params": params, "plate": plate})
        if not isinstance(data, dict) or "job_id" not in data:
            raise DeviceError(f"{self.name}: POST /jobs returned no job_id: {data!r}")
        return str(data["job_id"])

    async def _wait_job(self, job_id: str, on_progress: ProgressCallback | None) -> dict[str, Any]:
        job = await poll(
            lambda: self._request("GET", f"/jobs/{job_id}"),
            lambda d: (d or {}).get("state", "unknown"),
            TERMINAL,
            interval_s=self._settings.poll_interval_s,
            timeout_s=self._settings.timeout_s,
            what=f"{self.name} job {job_id}",
            on_progress=on_progress,
        )
        if job["state"] != "succeeded":
            raise DeviceError(f"{self.name} job {job_id} {job['state']}: {job.get('message') or 'no details'}")
        result = job.get("result") or {}
        if not isinstance(result, dict):
            raise DeviceError(f"{self.name} job {job_id}: result must be a JSON object, got {str(result)[:200]}")
        return result

    async def _cancel_job(self, job_id: str) -> None:
        await self._request("POST", f"/jobs/{job_id}/cancel")


class JobApiInstrument(_JobServer):
    async def check_ready(self) -> str | None:
        status = await self._health()
        if not status.get("ready"):
            raise DeviceError(f"{self.name} is not ready: {status.get('detail') or status}")
        return status.get("detail") or None

    async def open_access(self) -> str | None:
        if not self._settings.options.get("access"):
            return None
        await self._request("POST", "/access/open")
        return "opened for the transfer"

    async def close_access(self) -> str | None:
        if not self._settings.options.get("access"):
            return None
        await self._request("POST", "/access/close")
        return "closed"

    async def start(self, operation: str, params: dict[str, Any], plate: dict[str, Any]) -> str:
        if isinstance(params.get("source"), bytes):
            # A protocol file (liquid handling) travels base64-encoded.
            params = {**params, "protocol_b64": base64.b64encode(params["source"]).decode()}
            del params["source"]
        return await self._start_job(operation, params, plate)

    async def wait(self, handle: str, on_progress: ProgressCallback | None = None) -> dict[str, Any]:
        return await self._wait_job(handle, on_progress)

    async def cancel(self, handle: str) -> None:
        await self._cancel_job(handle)


class JobApiTransport(_JobServer):
    """Thor (or any plate mover) behind the job API: operation "transfer"."""

    async def status(self) -> ThorStatus:
        data = await self._health()
        return ThorStatus(
            at_home=bool(data.get("at_home")),
            holding_plate=bool(data.get("holding_plate")),
            message=str(data.get("detail", "")),
        )

    async def start_transfer(self, src: Location, dst: Location, plate_id: str) -> str:
        return await self._start_job(
            "transfer", {"from": src.as_dict(), "to": dst.as_dict()}, {"barcode": plate_id}
        )

    async def wait_transfer(self, task_id: str, on_progress: ProgressCallback | None = None) -> None:
        await self._wait_job(task_id, on_progress)

    async def cancel(self, task_id: str) -> None:
        await self._cancel_job(task_id)

    async def reset(self) -> ThorStatus:
        await self._request("POST", "/reset")
        return await self.status()
