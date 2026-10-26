"""Data shapes shared by the API, planner, sequencer and store.

A submission is the protocol IR: the plate, and the ordered steps to do to it
(liquid handling, read, wash, qPCR, hold). Steps name the kind of work, not how
to get the plate there: the planner picks an instrument for each step from the
registry and adds the plate moves in between.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_run_id() -> str:
    """Sortable and readable on a lab screen, e.g. 20261006-142200-a1b2."""
    return f"{utcnow():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


# --- What the protocol tool sends -------------------------------------------


class PlateSpec(BaseModel):
    barcode: str = Field(default="", max_length=64)
    storage_slot: str = Field(
        min_length=1, max_length=32, description="Storage location Thor fetches the plate from"
    )
    labware: str = Field(default="corning_96_wellplate_360ul_flat", max_length=128)


class TipRackRef(BaseModel):
    slot: str = Field(pattern=r"^[A-D][1-4]$")
    load_name: str = Field(min_length=1, max_length=128)


class TipUse(BaseModel):
    """How one pipette in the protocol uses tips (from the protocol tool's simulation)."""

    pipette: str = Field(min_length=1, max_length=64)
    channels: Literal[1, 8, 96]
    racks: list[TipRackRef] = Field(min_length=1)  # in the order the pipette uses them
    pickups: int = Field(ge=0)


class LiquidHandlingSpec(BaseModel):
    protocol_filename: str = Field(pattern=r"^[\w\-. ]{1,120}\.py$")
    protocol_b64: str = Field(description="Opentrons Python protocol, base64-encoded")
    runtime_parameters: dict[str, Any] = Field(default_factory=dict)
    # Flex deck slot for the plate; must match where the protocol loads it.
    deck_slot: str | None = Field(default=None, pattern=r"^[A-D][1-4]$")
    # Tip use per pipette; lets the master node continue partly used tip racks.
    tips: list[TipUse] = Field(default_factory=list)


WELL_PATTERN = re.compile(r"^([A-P])(\d{1,2})$")  # up to a 384-well plate (A-P x 1-24)


def well_sort_key(well: str) -> tuple[str, int]:
    match = WELL_PATTERN.match(well)
    return (match[1], int(match[2])) if match else (well, 0)


class ReadSpec(BaseModel):
    """Tecan read settings. Ranges follow the lab's Infinite M200 Pro.

    read_from and shake_s are kept for older runs; this reader's API ignores them.
    """

    mode: Literal["absorbance", "fluorescence", "luminescence"] = "absorbance"
    wells: list[str] = Field(min_length=1, max_length=384)

    # Absorbance: one measurement per wavelength.
    wavelengths_nm: list[int] = Field(default_factory=list, max_length=10)
    # Fluorescence intensity.
    excitation_nm: int | None = Field(default=None, ge=230, le=850)
    emission_nm: int | None = Field(default=None, ge=280, le=850)
    read_from: Literal["top", "bottom"] = "top"
    gain: int | None = Field(default=None, ge=1, le=255, description="None = optimal gain")
    # Luminescence.
    integration_ms: int = Field(default=1000, ge=1, le=10_000)

    flashes: int = Field(default=25, ge=1, le=100)
    shake_s: int = Field(default=0, ge=0, le=600, description="Orbital shake before reading")
    # A method saved on the Tecan PC; when set the reader runs it instead of the settings above.
    method: str = Field(default="", max_length=128)

    @field_validator("wells")
    @classmethod
    def _check_wells(cls, wells: list[str]) -> list[str]:
        cleaned = sorted({w.strip().upper() for w in wells}, key=well_sort_key)
        bad = [w for w in cleaned if not (m := WELL_PATTERN.match(w)) or not 1 <= int(m[2]) <= 24]
        if bad:
            raise ValueError(f"not well names: {', '.join(bad[:5])}")
        return cleaned

    @field_validator("wavelengths_nm")
    @classmethod
    def _check_wavelengths(cls, wavelengths: list[int]) -> list[int]:
        out_of_range = [w for w in wavelengths if not 230 <= w <= 1000]
        if out_of_range:
            raise ValueError(f"absorbance wavelengths must be 230-1000 nm, got {out_of_range}")
        return sorted(set(wavelengths))

    @model_validator(mode="after")
    def _check_mode(self) -> ReadSpec:
        if self.method:
            return self
        if self.mode == "absorbance" and not self.wavelengths_nm:
            raise ValueError("absorbance needs at least one wavelength")
        if self.mode == "fluorescence":
            if self.excitation_nm is None or self.emission_nm is None:
                raise ValueError("fluorescence needs excitation_nm and emission_nm")
            if self.emission_nm <= self.excitation_nm:
                raise ValueError("emission wavelength must be longer than excitation")
        return self

    def describe(self) -> str:
        if self.method:
            return f"method '{self.method}'"
        if self.mode == "absorbance":
            what = "absorbance " + ", ".join(f"{w} nm" for w in self.wavelengths_nm)
        elif self.mode == "fluorescence":
            what = f"fluorescence Ex {self.excitation_nm} / Em {self.emission_nm} nm"
        else:
            what = f"luminescence, {self.integration_ms} ms"
        return f"{what}, {len(self.wells)} wells"


# --- Workflow steps (the protocol IR) ----------------------------------------------
# `instrument` is a registry id; empty lets the planner pick one of the right kind.


class _StepBase(BaseModel):
    instrument: str = Field(default="", max_length=64)
    label: str = Field(default="", max_length=80)


class LiquidHandlingStep(_StepBase, LiquidHandlingSpec):
    type: Literal["liquid_handling"] = "liquid_handling"


class ReadStep(_StepBase):
    type: Literal["read"] = "read"
    settings: ReadSpec


class WashStep(_StepBase):
    type: Literal["wash"] = "wash"
    # A program saved on the washer; when empty the settings below are used.
    program: str = Field(default="", max_length=128)
    cycles: int = Field(default=3, ge=1, le=20)
    volume_ul: int = Field(default=300, ge=10, le=3000)
    soak_s: int = Field(default=0, ge=0, le=3600)


class QpcrStep(_StepBase):
    type: Literal["qpcr"] = "qpcr"
    # The run file or protocol saved on the qPCR instrument.
    program: str = Field(min_length=1, max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)


class HoldStep(_StepBase):
    """The plate waits where it is, e.g. an incubation at room temperature."""

    type: Literal["hold"] = "hold"
    seconds: int = Field(ge=1, le=24 * 3600)


WorkflowStep = Annotated[
    Union[LiquidHandlingStep, ReadStep, WashStep, QpcrStep, HoldStep], Field(discriminator="type")
]


def describe_step(step: WorkflowStep) -> str:
    if step.type == "liquid_handling":
        return f"run {step.protocol_filename}"
    if step.type == "read":
        return step.settings.describe()
    if step.type == "wash":
        return f"program '{step.program}'" if step.program else f"{step.cycles}x {step.volume_ul} uL" + (f", soak {step.soak_s} s" if step.soak_s else "")
    if step.type == "qpcr":
        return f"program '{step.program}'"
    return f"{step.seconds} s"


class ProtocolSubmission(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scientist: str = Field(min_length=1, max_length=80)
    plate: PlateSpec
    steps: list[WorkflowStep] = Field(min_length=1, max_length=40)
    notes: str = Field(default="", max_length=2000)
    # How the protocol tool turns the readings into results (plate map, curve fits).
    # Kept with the run as given; the master node doesn't interpret it.
    analysis: list[dict[str, Any]] = Field(default_factory=list, max_length=10)

    @model_validator(mode="before")
    @classmethod
    def _from_fixed_sequence(cls, data: Any) -> Any:
        """Accepts the older shape, one Flex protocol then one read, as two steps."""
        if isinstance(data, dict) and "steps" not in data and "liquid_handling" in data:
            data = dict(data)
            steps = [{"type": "liquid_handling", **data.pop("liquid_handling")}]
            if data.get("read") is not None:
                steps.append({"type": "read", "settings": data.pop("read")})
            data.pop("read", None)
            data["steps"] = steps
        return data


# --- Plate reader output -----------------------------------------------------


class Measurement(BaseModel):
    label: str
    unit: str = ""
    values: dict[str, float | None]  # well name ("A1") -> reading


class ReadResult(BaseModel):
    measurements: list[Measurement]
    metadata: dict[str, Any] = Field(default_factory=dict)


# --- Run state ---------------------------------------------------------------


class RunStatus(StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    aborted = "aborted"
    # The master node stopped mid-run; device state is unknown.
    interrupted = "interrupted"

    @property
    def finished(self) -> bool:
        return self not in (RunStatus.queued, RunStatus.running)


class StepStatus(StrEnum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"
    skipped = "skipped"


class Step(BaseModel):
    """One thing the orchestrator does, as planned from the workflow."""

    # What the orchestrator does: decode_plan, transfer, clear, operation or hold.
    key: str
    label: str
    # Who the master node talks to for this step: "master" or a registry instrument id.
    device: str
    # Unique within the run, e.g. "3-operation".
    id: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    status: StepStatus = StepStatus.pending
    started_at: datetime | None = None
    finished_at: datetime | None = None
    detail: str = ""


class Event(BaseModel):
    at: datetime = Field(default_factory=utcnow)
    level: Literal["info", "warning", "error"] = "info"
    message: str


class Run(BaseModel):
    id: str
    status: RunStatus = RunStatus.queued
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    protocol: ProtocolSubmission
    steps: list[Step]
    events: list[Event] = Field(default_factory=list)
    # Handles for device operations in flight (Flex run id etc.), used to stop them on abort.
    context: dict[str, Any] = Field(default_factory=dict)
    # Every measurement the run's instruments returned (reads, qPCR), for the plate heatmap.
    results: ReadResult | None = None
    # Anything else an instrument returned, by step id.
    outputs: dict[str, Any] = Field(default_factory=dict)
    error: str = ""

    def add_event(self, message: str, level: Literal["info", "warning", "error"] = "info") -> None:
        self.events.append(Event(message=message, level=level))

    @property
    def current_step(self) -> Step | None:
        return next((s for s in self.steps if s.status is StepStatus.running), None)
