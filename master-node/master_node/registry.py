"""Instrument registry: every instrument the master node can use, from instruments.toml.

Each [[instrument]] entry says what the instrument is (kind), how the master node
talks to it (driver), where its server is, where the transport puts the plate
on it, and the safety limits runs must stay within. The planner picks
instruments from here and checks every step against their limits before a run
is accepted.

Drivers:
  sim             simulated, for trying the whole flow without hardware
  manual          (transport only) a person moves the plate and confirms it
  job_api         an instrument server that follows the lab's job API (INSTRUMENT_API.md)
  opentrons_flex  an Opentrons Flex, through the robot's own HTTP API
  tecan_infinite  the Tecan Infinite M200 Pro reader API on the Tecan PC
"""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

KINDS = ("transport", "liquid_handler", "plate_reader", "plate_washer", "qpcr")
DRIVERS = ("sim", "manual", "job_api", "opentrons_flex", "tecan_infinite")
# Drivers that only work for one kind of instrument.
DRIVER_KIND = {"manual": "transport", "opentrons_flex": "liquid_handler", "tecan_infinite": "plate_reader"}
# The kind of instrument each workflow step type needs.
STEP_KIND = {"liquid_handling": "liquid_handler", "read": "plate_reader", "wash": "plate_washer", "qpcr": "qpcr"}
KIND_LABEL = {
    "transport": "Transport", "liquid_handler": "Liquid handler", "plate_reader": "Plate reader",
    "plate_washer": "Plate washer", "qpcr": "qPCR",
}
STORAGE = "storage"  # where plates come from; not an instrument

# Flex deck slots the transport can reach: not A1/A2 (out of reach), A3 (trash bin) or D3 (waste chute).
FLEX_PLATE_SLOTS = ["B1", "B2", "B3", "C1", "C2", "C3", "D1", "D2"]


class RegistryError(ValueError):
    """instruments.toml is missing something or contradicts itself."""


@dataclass
class Instrument:
    id: str
    kind: str
    name: str = ""
    driver: str = "sim"
    url: str = ""
    # Sent as X-API-Key so the instrument server accepts commands only from the master node.
    api_key: str = ""
    # Longest one operation (a transfer, a protocol run, a read) may take.
    timeout_s: float = 600.0
    poll_interval_s: float = 2.0
    request_timeout_s: float = 30.0
    # Where the transport can put the plate on this instrument, and where it does unless a step says otherwise.
    plate_slots: list[str] = field(default_factory=lambda: ["carrier"])
    default_slot: str = ""
    # Transport only: how long to wait for it to report home and empty-handed after a move.
    clear_timeout_s: float = 30.0
    # Driver settings, e.g. opentrons_version for the Flex or access = true for a job_api
    # server with a drawer or lid the master node must open before a plate is moved.
    options: dict[str, Any] = field(default_factory=dict)
    # Safety limits checked before a run is accepted; see check_limits in planner.py.
    limits: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.id.replace("_", " ").title()
        if not self.default_slot and self.plate_slots:
            self.default_slot = self.plate_slots[0]

    def view(self) -> dict[str, Any]:
        """As the API shows it: no key."""
        data = asdict(self)
        data.pop("api_key")
        data["kind_label"] = KIND_LABEL[self.kind]
        return data


@dataclass
class Registry:
    instruments: list[Instrument]

    def __post_init__(self) -> None:
        self._validate()

    def get(self, instrument_id: str) -> Instrument | None:
        return next((i for i in self.instruments if i.id == instrument_id), None)

    def of_kind(self, kind: str) -> list[Instrument]:
        return [i for i in self.instruments if i.kind == kind]

    @property
    def transport(self) -> Instrument | None:
        found = self.of_kind("transport")
        return found[0] if found else None

    def view(self) -> list[dict[str, Any]]:
        return [i.view() for i in self.instruments]

    def _validate(self) -> None:
        seen: set[str] = set()
        for inst in self.instruments:
            where = f"instrument {inst.id!r}"
            if not inst.id or not inst.id.replace("_", "").replace("-", "").isalnum():
                raise RegistryError(f"{where}: id must be letters, digits, '-' or '_'")
            if inst.id == STORAGE:
                raise RegistryError(f"{where}: '{STORAGE}' is reserved for plate storage")
            if inst.id in seen:
                raise RegistryError(f"{where}: listed twice")
            seen.add(inst.id)
            if inst.kind not in KINDS:
                raise RegistryError(f"{where}: kind must be one of {', '.join(KINDS)}, got {inst.kind!r}")
            if inst.driver not in DRIVERS:
                raise RegistryError(f"{where}: driver must be one of {', '.join(DRIVERS)}, got {inst.driver!r}")
            needs = DRIVER_KIND.get(inst.driver)
            if needs and needs != inst.kind:
                raise RegistryError(f"{where}: driver {inst.driver!r} is for a {needs}, not a {inst.kind}")
            if inst.driver not in ("sim", "manual") and not inst.url:
                raise RegistryError(f"{where}: driver {inst.driver!r} needs a url")
            if inst.kind != "transport":
                if not inst.plate_slots:
                    raise RegistryError(f"{where}: plate_slots must list at least one slot")
                if inst.default_slot not in inst.plate_slots:
                    raise RegistryError(f"{where}: default_slot {inst.default_slot!r} is not in plate_slots")
        if len(self.of_kind("transport")) > 1:
            raise RegistryError("Only one transport instrument is supported")


def default_registry() -> Registry:
    """Used when there is no instruments.toml: the lab's three instruments, all simulated."""
    return Registry([
        Instrument(id="thor", kind="transport", name="Jetson Thor"),
        Instrument(id="flex", kind="liquid_handler", name="Opentrons Flex",
                   plate_slots=list(FLEX_PLATE_SLOTS), default_slot="D2", timeout_s=4 * 3600.0),
        Instrument(id="tecan", kind="plate_reader", name="Tecan Infinite M200 Pro", timeout_s=1800.0),
    ])


def load_registry(path: str | Path) -> Registry:
    path = Path(path)
    if not path.exists():
        return default_registry()
    with path.open("rb") as f:
        raw = tomllib.load(f)
    unknown = set(raw) - {"instrument"}
    if unknown:
        raise RegistryError(f"{path}: unknown sections {sorted(unknown)}; each instrument is an [[instrument]] table")
    known = {f.name for f in fields(Instrument)}
    instruments = []
    for n, entry in enumerate(raw.get("instrument", []), start=1):
        extra = set(entry) - known
        if extra:
            raise RegistryError(f"{path}: instrument {entry.get('id', n)!r} has unknown keys {sorted(extra)}")
        try:
            instruments.append(Instrument(**entry))
        except TypeError as exc:
            raise RegistryError(f"{path}: instrument {entry.get('id', n)!r}: {exc}") from exc
    if not instruments:
        raise RegistryError(f"{path}: no [[instrument]] entries")
    try:
        return Registry(instruments)
    except RegistryError as exc:
        raise RegistryError(f"{path}: {exc}") from exc
