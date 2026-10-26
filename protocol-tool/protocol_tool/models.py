"""Shapes for experiment designs, simulations and chat sessions.

A design is what the planner services build with the scientist:
- requirements: what the experiment needs, extracted from the request and documents
- protocols: Opentrons protocols, each simulated on its own
- workflow: the protocol IR, the ordered steps done to the plate (liquid handling
  with one of the protocols, read, wash, qPCR, hold). The master node picks an
  instrument for each step and adds the plate moves.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, field_validator, model_validator

DECK_SLOTS = [f"{row}{col}" for row in "ABCD" for col in (1, 2, 3, 4)]
WELL_PATTERN = re.compile(r"^([A-P])(\d{1,2})$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return secrets.token_urlsafe(9)


def code_hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()[:16]


def well_sort_key(well: str) -> tuple[str, int]:
    match = WELL_PATTERN.match(well)
    return (match[1], int(match[2])) if match else (well, 0)


def expand_wells(items: list[str]) -> list[str]:
    """Accepts well names and rectangular ranges ("A1:H6"); returns sorted unique wells."""
    wells: set[str] = set()
    for item in items:
        item = item.strip().upper()
        if ":" in item:
            start, end = (WELL_PATTERN.match(p.strip()) for p in item.split(":", 1))
            if not (start and end):
                raise ValueError(f"bad well range {item!r}")
            rows = range(min(ord(start[1]), ord(end[1])), max(ord(start[1]), ord(end[1])) + 1)
            cols = range(min(int(start[2]), int(end[2])), max(int(start[2]), int(end[2])) + 1)
            wells.update(f"{chr(r)}{c}" for r in rows for c in cols)
        elif item:
            wells.add(item)
    bad = [w for w in wells if not (m := WELL_PATTERN.match(w)) or not 1 <= int(m[2]) <= 24]
    if bad:
        raise ValueError(f"not well names: {', '.join(sorted(bad)[:5])}")
    return sorted(wells, key=well_sort_key)


class ReaderSettings(BaseModel):
    """Tecan read settings (Infinite M200 Pro). Mirrors master_node.models.ReadSpec, which re-validates them.

    read_from and shake_s are kept for older sessions; the reader cannot use them.
    """

    mode: Literal["absorbance", "fluorescence", "luminescence"] = "absorbance"
    wells: list[str] = Field(
        default_factory=lambda: [f"{r}{c}" for r in "ABCDEFGH" for c in range(1, 13)],
        min_length=1,
        max_length=384,
    )
    wavelengths_nm: list[int] = Field(default_factory=lambda: [600], max_length=10)
    excitation_nm: int | None = Field(default=None, ge=230, le=850)
    emission_nm: int | None = Field(default=None, ge=280, le=850)
    read_from: Literal["top", "bottom"] = "top"
    gain: int | None = Field(default=None, ge=1, le=255)  # None = optimal
    integration_ms: int = Field(default=1000, ge=1, le=10_000)
    flashes: int = Field(default=25, ge=1, le=100)
    shake_s: int = Field(default=0, ge=0, le=600)

    @field_validator("wells", mode="before")
    @classmethod
    def _expand(cls, wells: Any) -> Any:
        return expand_wells(wells) if isinstance(wells, list) else wells

    @field_validator("wavelengths_nm")
    @classmethod
    def _check_wavelengths(cls, wavelengths: list[int]) -> list[int]:
        if any(not 230 <= w <= 1000 for w in wavelengths):
            raise ValueError("absorbance wavelengths must be 230-1000 nm")
        return sorted(set(wavelengths))

    @model_validator(mode="after")
    def _check_mode(self) -> ReaderSettings:
        if self.mode == "absorbance" and not self.wavelengths_nm:
            raise ValueError("absorbance needs at least one wavelength")
        if self.mode == "fluorescence":
            if self.excitation_nm is None or self.emission_nm is None:
                raise ValueError("fluorescence needs excitation_nm and emission_nm")
            if self.emission_nm <= self.excitation_nm:
                raise ValueError("emission wavelength must be longer than excitation")
        return self

    def describe(self) -> str:
        if self.mode == "absorbance":
            what = "absorbance at " + ", ".join(f"{w} nm" for w in self.wavelengths_nm)
        elif self.mode == "fluorescence":
            what = f"fluorescence Ex {self.excitation_nm} / Em {self.emission_nm} nm"
        else:
            what = f"luminescence, {self.integration_ms} ms integration"
        return f"{what}; {len(self.wells)} wells"


@dataclass
class ModelTurn:
    """One complete response from the AI model, as Anthropic-format content blocks."""

    content: list[dict[str, Any]]
    stop_reason: str


# --- Deck, as reported by the Opentrons simulator -------------------------------


class Labware(BaseModel):
    id: str
    load_name: str
    display_name: str
    category: str = ""  # wellPlate, tipRack, reservoir, adapter, aluminumBlock, tubeRack...
    rows: int = 0
    cols: int = 0
    # Well -> liquid ids, for wells the protocol fills with load_liquid().
    liquids: dict[str, list[str]] = Field(default_factory=dict)


class Module(BaseModel):
    id: str
    model: str
    display_name: str


class SlotContents(BaseModel):
    module: Module | None = None
    # Bottom to top, e.g. [adapter, plate].
    labware: list[Labware] = Field(default_factory=list)
    trash: bool = False
    waste_chute: bool = False


class Liquid(BaseModel):
    id: str
    name: str
    description: str = ""
    color: str = "#888888"


class Pipette(BaseModel):
    mount: str
    name: str
    display_name: str


class Deck(BaseModel):
    slots: dict[str, SlotContents] = Field(default_factory=dict)
    off_deck: list[Labware] = Field(default_factory=list)
    pipettes: list[Pipette] = Field(default_factory=list)
    liquids: list[Liquid] = Field(default_factory=list)

    def plate_at(self, slot: str) -> Labware | None:
        contents = self.slots.get(slot)
        return contents.labware[-1] if contents and contents.labware else None


class Simulation(BaseModel):
    code_hash: str
    plate_slot: str
    ok: bool
    ran_at: datetime = Field(default_factory=utcnow)
    duration_s: float = 0
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    deck: Deck = Field(default_factory=Deck)
    steps: list[str] = Field(default_factory=list)
    stats: dict[str, int] = Field(default_factory=dict)
    # Per pipette: {"pipette", "channels", "racks": [{"slot", "load_name"}], "pickups"}.
    # Sent with the run so the master node can continue partly used tip racks.
    tips: list[dict[str, Any]] = Field(default_factory=list)
    # The lab rules (DeckRules.signature) this was checked against; "" = before rules were recorded.
    rules: str = ""


# --- Design: requirements, protocols, workflow ------------------------------------------


class Requirements(BaseModel):
    """What the experiment needs, as extracted from the scientist's request and documents."""

    assay: str = Field(default="", max_length=200)
    goal: str = Field(default="", max_length=1000)
    samples: str = Field(default="", max_length=1000)
    controls: str = Field(default="", max_length=1000)
    replicates: str = Field(default="", max_length=300)
    readout: str = Field(default="", max_length=500)
    assumptions: list[str] = Field(default_factory=list, max_length=30)
    open_questions: list[str] = Field(default_factory=list, max_length=20)
    # Knowledge documents (kit manuals, assay templates) this was based on.
    sources: list[str] = Field(default_factory=list, max_length=20)


class ProtocolDraft(BaseModel):
    """One Opentrons protocol, used by a liquid-handling step of the workflow."""

    id: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    title: str = ""
    summary: str = ""
    code: str = ""
    version: int = 0  # bumped each time the code changes
    plate_slot: str = ""  # where the transport places the sample plate on the Flex deck
    simulation: Simulation | None = None

    @property
    def simulation_current(self) -> bool:
        """True when the last simulation covers the code and plate slot as they are now."""
        sim = self.simulation
        return bool(sim and self.code and sim.code_hash == code_hash(self.code) and sim.plate_slot == self.plate_slot)

    def view(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["simulation_current"] = self.simulation_current
        return data


def new_step_id() -> str:
    return secrets.token_hex(3)


class _Step(BaseModel):
    id: str = Field(default_factory=new_step_id)
    # A registry instrument id; empty lets the master node pick one of the right kind.
    instrument: str = Field(default="", max_length=64)
    label: str = Field(default="", max_length=80)


class LiquidHandlingStep(_Step):
    type: Literal["liquid_handling"] = "liquid_handling"
    protocol_id: str


class ReadStep(_Step):
    type: Literal["read"] = "read"
    settings: ReaderSettings = Field(default_factory=ReaderSettings)


class WashStep(_Step):
    type: Literal["wash"] = "wash"
    program: str = Field(default="", max_length=128)  # a program saved on the washer
    cycles: int = Field(default=3, ge=1, le=20)
    volume_ul: int = Field(default=300, ge=10, le=3000)
    soak_s: int = Field(default=0, ge=0, le=3600)


class QpcrStep(_Step):
    type: Literal["qpcr"] = "qpcr"
    program: str = Field(min_length=1, max_length=128)  # run file or protocol saved on the qPCR
    params: dict[str, Any] = Field(default_factory=dict)


class HoldStep(_Step):
    type: Literal["hold"] = "hold"
    seconds: int = Field(ge=1, le=24 * 3600)


WorkflowStep = Annotated[
    Union[LiquidHandlingStep, ReadStep, WashStep, QpcrStep, HoldStep], Field(discriminator="type")
]
STEP_TYPES = ("liquid_handling", "read", "wash", "qpcr", "hold")


class WellGroup(BaseModel):
    """Replicate wells that belong together: one standard, one sample, one dose."""

    name: str = Field(min_length=1, max_length=60)
    wells: list[str] = Field(min_length=1, max_length=96)
    # Known x: standard concentration, dose, dilution. None for unknown samples.
    x: float | None = None
    # Samples: what the sample was diluted by before reading; results are multiplied back.
    dilution: float = Field(default=1.0, gt=0)
    # Dose-response: which curve the point belongs to (e.g. the compound).
    series: str = Field(default="", max_length=60)

    @field_validator("wells", mode="before")
    @classmethod
    def _expand(cls, wells: Any) -> Any:
        return expand_wells(wells) if isinstance(wells, list) else wells


ANALYSIS_KINDS = ("standard_curve", "dose_response", "dilution_series", "group_comparison")
FITS = ("linear", "linear_zero", "quadratic", "log_linear", "4pl", "5pl", "none")
MIN_POINTS = {"linear": 2, "linear_zero": 1, "quadratic": 3, "log_linear": 2, "4pl": 4, "5pl": 5, "none": 1}


class Analysis(BaseModel):
    """How a run's readings become a result: the plate map and the curve to fit.
    Computed deterministically (analysis.py) once the readings exist."""

    id: str = Field(default_factory=new_step_id)
    title: str = Field(min_length=1, max_length=120)
    kind: Literal["standard_curve", "dose_response", "dilution_series", "group_comparison"]
    # The reading to use, as the reader labels it: "Absorbance 595 nm", "Fluorescence Ex 485 / Em 535 nm", "Ct".
    measurement: str = Field(min_length=1, max_length=120)
    # Workflow step number of that read, when the workflow reads more than once.
    step: int | None = Field(default=None, ge=1, le=40)
    # Subtracted well by well first, e.g. "Absorbance 570 nm" for a 450 - 570 nm ELISA read.
    reference_measurement: str = Field(default="", max_length=120)
    # Their mean is subtracted from every signal.
    blank_wells: list[str] = Field(default_factory=list, max_length=96)
    fit: Literal["linear", "linear_zero", "quadratic", "log_linear", "4pl", "5pl", "none"] = "linear"
    x_label: str = Field(default="Concentration", max_length=60)
    x_unit: str = Field(default="", max_length=30)
    y_label: str = Field(default="", max_length=60)
    x_log: bool = False  # plot x on a log scale (dose-response, qPCR quantities)
    # Points with known x: the standard curve, or the doses / dilutions.
    standards: list[WellGroup] = Field(default_factory=list, max_length=48)
    # Standard curve: unknowns read off the curve. Group comparison: the groups compared.
    samples: list[WellGroup] = Field(default_factory=list, max_length=96)
    # Group comparison: the group the others are compared with (fold change, t-test).
    control: str = Field(default="", max_length=60)

    @field_validator("blank_wells", mode="before")
    @classmethod
    def _expand(cls, wells: Any) -> Any:
        return expand_wells(wells) if isinstance(wells, list) else wells

    @model_validator(mode="after")
    def _check(self) -> Analysis:
        if self.kind in ("standard_curve", "dose_response", "dilution_series"):
            groups: dict[str, int] = {}
            for s in self.standards:
                if s.x is None:
                    raise ValueError(f"standard '{s.name}' needs its known x (concentration, dose or dilution)")
                groups[s.series] = groups.get(s.series, 0) + 1
            need = MIN_POINTS[self.fit]
            for series, count in groups.items() or {"": 0}.items():
                if count < need:
                    raise ValueError(f"a {self.fit} fit needs at least {need} standards" + (f" in series '{series}'" if series else "") + f"; got {count}")
            if (self.fit in ("log_linear", "4pl", "5pl") or self.x_log) and any(s.x <= 0 for s in self.standards):
                raise ValueError(f"x must be above 0 for a {self.fit} fit or a log axis (leave the zero standard out, or use it as a blank)")
        if self.kind == "standard_curve" and not self.samples:
            raise ValueError("a standard curve needs samples to read off it")
        if self.kind == "group_comparison":
            if len(self.samples) < 2:
                raise ValueError("a group comparison needs at least 2 groups in samples")
            if self.control and self.control not in {g.name for g in self.samples}:
                raise ValueError(f"control '{self.control}' is not one of the groups")
        return self

    def wells(self) -> set[str]:
        return set(self.blank_wells) | {w for g in self.standards + self.samples for w in g.wells}


class Design(BaseModel):
    title: str = ""
    summary: str = ""
    requirements: Requirements | None = None
    protocols: list[ProtocolDraft] = Field(default_factory=list)
    workflow: list[WorkflowStep] = Field(default_factory=list)
    # How the readings become results: standard curves, dose-response, comparisons.
    analyses: list[Analysis] = Field(default_factory=list, max_length=10)

    def protocol(self, protocol_id: str) -> ProtocolDraft | None:
        return next((p for p in self.protocols if p.id == protocol_id), None)

    def step(self, step_id: str) -> WorkflowStep | None:
        return next((s for s in self.workflow if s.id == step_id), None)

    def read_steps(self) -> list[ReadStep]:
        return [s for s in self.workflow if s.type == "read"]

    def describe_step(self, step: WorkflowStep) -> str:
        if step.type == "liquid_handling":
            p = self.protocol(step.protocol_id)
            return f"protocol '{p.title or p.id}'" if p else f"protocol '{step.protocol_id}' (missing)"
        if step.type == "read":
            return step.settings.describe()
        if step.type == "wash":
            if step.program:
                return f"program '{step.program}'"
            return f"{step.cycles} x {step.volume_ul} uL" + (f", soak {step.soak_s} s" if step.soak_s else "")
        if step.type == "qpcr":
            return f"program '{step.program}'"
        return f"hold {step.seconds} s"

    def send_problems(self) -> list[str]:
        """Why the design can't go to the lab yet; empty when it can."""
        if not self.workflow:
            return ["There is no workflow yet. Describe the experiment in the chat."]
        problems = []
        for n, step in enumerate(self.workflow, start=1):
            if step.type != "liquid_handling":
                continue
            p = self.protocol(step.protocol_id)
            if p is None or not p.code:
                problems.append(f"Step {n} uses protocol '{step.protocol_id}', which has not been written yet.")
            elif not p.simulation_current:
                problems.append(f"Protocol '{p.title or p.id}' v{p.version} has changed since its last simulation. Run the simulation.")
            elif not p.simulation.ok:
                problems.append(f"Protocol '{p.title or p.id}' failed simulation; fix it before sending.")
        return problems


def _from_single_protocol(data: dict[str, Any]) -> dict[str, Any]:
    """Converts a session saved by the older version (one protocol, one reader panel)."""
    data = dict(data)
    design = dict(data.get("design") or {})
    simulation = data.pop("simulation", None)
    if "protocol_code" in design or "reader" in design:
        code = design.pop("protocol_code", "")
        version = design.pop("version", 0)
        plate_slot = design.pop("plate_slot", "")
        reader = design.pop("reader", None)
        protocols, workflow = [], []
        if code:
            protocols.append({"id": "main", "title": design.get("title", ""), "summary": design.get("summary", ""),
                              "code": code, "version": version, "plate_slot": plate_slot, "simulation": simulation})
            workflow.append({"id": "lh-main", "type": "liquid_handling", "protocol_id": "main"})
        if code or reader:
            workflow.append({"id": "read-1", "type": "read", **({"settings": reader} if reader else {})})
        design.setdefault("protocols", protocols)
        design.setdefault("workflow", workflow)
        data["design"] = design
    return data


# --- Design session ------------------------------------------------------------


class ToolActivity(BaseModel):
    name: str
    ok: bool
    summary: str


class ChatEntry(BaseModel):
    role: Literal["user", "assistant"]
    text: str = ""
    attachments: list[str] = Field(default_factory=list)
    tools: list[ToolActivity] = Field(default_factory=list)
    # The reply in the order it happened: {"type": "text", "text"} and {"type": "tool", ...activity}.
    parts: list[dict[str, Any]] = Field(default_factory=list)
    error: str = ""
    at: datetime = Field(default_factory=utcnow)


class Session(BaseModel):
    id: str = Field(default_factory=new_id)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    design: Design = Field(default_factory=Design)
    transcript: list[ChatEntry] = Field(default_factory=list)
    # Claude API conversation, append-only. Never sent to the browser.
    messages: list[dict[str, Any]] = Field(default_factory=list)
    submitted_runs: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _upgrade(cls, data: Any) -> Any:
        return _from_single_protocol(data) if isinstance(data, dict) else data

    def view(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude={"messages"})
        data["design"]["protocols"] = [p.view() for p in self.design.protocols]
        data["send_problems"] = self.design.send_problems()
        return data


class DesignPatch(BaseModel):
    """Edits the scientist makes by hand (the plate-reader panel)."""

    reader: ReaderSettings | None = None
    # The read step the reader settings belong to; empty means the first read step.
    step_id: str = ""


class SendRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scientist: str = Field(min_length=1, max_length=80)
    storage_slot: str = Field(min_length=1, max_length=32)
    barcode: str = Field(default="", max_length=64)
    notes: str = Field(default="", max_length=2000)
    # Plate type when no protocol loads the plate (otherwise the simulation says).
    labware: str = Field(default="", max_length=128)
    # From the plan the scientist reviewed and approved; sending fails if the design changed since.
    plan_hash: str = Field(default="", max_length=64)
