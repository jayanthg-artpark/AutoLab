"""Interfaces the sequencer uses to drive lab instruments.

Every instrument that works on the plate (liquid handler, reader, washer, qPCR)
has the same interface, whatever its driver: the sequencer starts an operation,
waits for it, and can cancel it. The transport (Thor, or a person standing in
for it) moves the plate between instruments.

Long operations are split into start (returns a handle as soon as the
instrument accepts the command) and wait (blocks until it finishes), so the
sequencer can record the handle and stop the instrument if the run is aborted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

# Called with the device's new state each time it changes, e.g. "running", "paused".
ProgressCallback = Callable[[str], None]


class DeviceError(Exception):
    """A device refused a command, reported a failure, or could not be reached."""


@dataclass(frozen=True)
class Location:
    station: str  # "storage" or an instrument id from the registry
    slot: str

    def __str__(self) -> str:
        return f"{self.station}/{self.slot}"

    def as_dict(self) -> dict[str, str]:
        return {"station": self.station, "slot": self.slot}


@dataclass(frozen=True)
class ThorStatus:
    at_home: bool
    holding_plate: bool
    message: str = ""

    @property
    def clear(self) -> bool:
        """True when the transport is parked and empty-handed, so no station is obstructed."""
        return self.at_home and not self.holding_plate


class Transport(Protocol):
    """Moves plates between storage and instruments (Jetson Thor)."""

    async def status(self) -> ThorStatus: ...

    async def start_transfer(self, src: Location, dst: Location, plate_id: str) -> str: ...

    async def wait_transfer(self, task_id: str, on_progress: ProgressCallback | None = None) -> None:
        """Returns once the plate is placed and the transport is back home."""

    async def cancel(self, task_id: str) -> None: ...

    async def reset(self) -> ThorStatus:
        """A person checked the transport is parked and empty-handed (after an aborted or failed
        move) and says so, so runs can start again."""

    async def aclose(self) -> None: ...


class Instrument(Protocol):
    """An instrument that works on the plate where it sits."""

    async def check_ready(self) -> str | None:
        """Raises DeviceError if the instrument is unreachable or busy; may return a status line."""

    async def open_access(self) -> str | None:
        """Makes the plate position reachable for the transport (e.g. opens a drawer).
        Returns what it did ("drawer opened"), or None if this instrument has nothing to open."""

    async def close_access(self) -> str | None: ...

    async def start(self, operation: str, params: dict[str, Any], plate: dict[str, Any]) -> str:
        """Starts `operation` (a workflow step type) and returns a handle.

        `plate` is {"barcode", "labware", "slot"}."""

    async def wait(self, handle: str, on_progress: ProgressCallback | None = None) -> dict[str, Any]:
        """Returns what the operation produced once it succeeds; raises DeviceError otherwise.

        Measurements come as {"measurements": [{"label": "Absorbance 600 nm", "unit": "OD",
        "values": {"A1": 0.051, ...}}], "metadata": {...}}; anything else is kept as is.
        """

    async def cancel(self, handle: str) -> None: ...

    async def aclose(self) -> None: ...


class NoAccess:
    """For instruments whose plate position is always reachable."""

    async def open_access(self) -> str | None:
        return None

    async def close_access(self) -> str | None:
        return None


@dataclass
class Lab:
    """The instruments the master node drives, by registry id."""

    transport: Transport | None
    instruments: dict[str, Instrument]

    async def aclose(self) -> None:
        for device in [self.transport, *self.instruments.values()]:
            if device is not None:
                await device.aclose()
