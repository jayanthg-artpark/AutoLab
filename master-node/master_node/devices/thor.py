"""Jetson Thor (navigation + manipulation) over HTTP.

The Thor side is expected to expose this small task API, typically a thin
bridge in front of its ROS 2 stack:

  GET  /status                 -> {"at_home": bool, "holding_plate": bool, "message": str}
  POST /tasks                  <- {"type": "transfer",
                                   "from": {"station": str, "slot": str},
                                   "to":   {"station": str, "slot": str},
                                   "plate_id": str}
                               -> {"task_id": str}
  GET  /tasks/{task_id}        -> {"state": "queued|running|succeeded|failed|cancelled",
                                   "message": str}
  POST /tasks/{task_id}/cancel

A transfer must only report "succeeded" once the plate is placed and the arm
is back home ("Placed, home" in the sequence diagram).

Only the master node may command Thor: when [thor] api_key is set, every request
carries it as X-API-Key, and the bridge must reject requests without it (401).
"""

from __future__ import annotations

import httpx

from ..config import DeviceSettings
from .base import DeviceError, Location, ProgressCallback, ThorStatus
from .http import HttpDevice, key_header, poll

TERMINAL = {"succeeded", "failed", "cancelled"}


class ThorHttp(HttpDevice):
    name = "Thor"

    def __init__(self, settings: DeviceSettings, transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(
            settings.base_url, settings.request_timeout_s, headers=key_header(settings.api_key), transport=transport
        )
        self._settings = settings

    async def status(self) -> ThorStatus:
        data = await self._request("GET", "/status")
        return ThorStatus(
            at_home=bool(data.get("at_home")),
            holding_plate=bool(data.get("holding_plate")),
            message=str(data.get("message", "")),
        )

    async def start_transfer(self, src: Location, dst: Location, plate_id: str) -> str:
        data = await self._request(
            "POST",
            "/tasks",
            json={"type": "transfer", "from": src.as_dict(), "to": dst.as_dict(), "plate_id": plate_id},
        )
        if not data or "task_id" not in data:
            raise DeviceError(f"Thor: POST /tasks returned no task_id: {data!r}")
        return str(data["task_id"])

    async def wait_transfer(self, task_id: str, on_progress: ProgressCallback | None = None) -> None:
        task = await poll(
            lambda: self._request("GET", f"/tasks/{task_id}"),
            lambda d: d.get("state", "unknown"),
            TERMINAL,
            interval_s=self._settings.poll_interval_s,
            timeout_s=self._settings.timeout_s,
            what=f"Thor task {task_id}",
            on_progress=on_progress,
        )
        if task["state"] != "succeeded":
            raise DeviceError(f"Thor task {task_id} {task['state']}: {task.get('message') or 'no details'}")

    async def cancel(self, task_id: str) -> None:
        await self._request("POST", f"/tasks/{task_id}/cancel")
