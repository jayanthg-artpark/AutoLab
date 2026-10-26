"""A person standing in for Thor until the robot is connected.

Each transfer waits until someone moves the plate by hand and confirms it
(the "Plate moved" button on the protocol tool's Runs tab, which calls
POST /runs/{id}/plate-moved on the master node).
"""

from __future__ import annotations

import asyncio
import itertools

from .base import DeviceError, Location, ProgressCallback, ThorStatus


class ManualThor:
    def __init__(self, timeout_s: float):
        self._timeout_s = timeout_s
        self._pending: dict[str, asyncio.Event] = {}
        self._ids = itertools.count(1)

    async def status(self) -> ThorStatus:
        return ThorStatus(at_home=True, holding_plate=False, message="manual: a person moves the plate")

    async def start_transfer(self, src: Location, dst: Location, plate_id: str) -> str:
        task_id = f"manual-{next(self._ids)}"
        self._pending[task_id] = asyncio.Event()
        return task_id

    async def wait_transfer(self, task_id: str, on_progress: ProgressCallback | None = None) -> None:
        event = self._pending[task_id]
        if on_progress:
            on_progress("waiting for a person to move the plate")
        try:
            await asyncio.wait_for(event.wait(), timeout=self._timeout_s)
        except asyncio.TimeoutError as exc:
            raise DeviceError(f"Nobody confirmed the plate move within {self._timeout_s / 60:.0f} minutes") from exc
        finally:
            self._pending.pop(task_id, None)

    def confirm(self, task_id: str) -> bool:
        event = self._pending.get(task_id)
        if event is None:
            return False
        event.set()
        return True

    async def cancel(self, task_id: str) -> None:
        self._pending.pop(task_id, None)

    async def reset(self) -> ThorStatus:
        return await self.status()  # always home: the person is the transport

    async def aclose(self) -> None:
        pass
