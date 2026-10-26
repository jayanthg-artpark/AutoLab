"""Decode + plan: turns a submitted workflow into the ordered steps the
sequencer executes. Deterministic, no AI:

1. Select instruments: each workflow step gets an instrument of the right kind
   from the registry (the one the step names, or the first whose limits allow it).
2. Check safety limits: every step must be within its instrument's limits.
3. Add plate moves: the plate starts in storage; before each step that happens
   on a different instrument, the transport moves it there, then the master
   node waits for the transport to clear before the instrument starts.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any

from .devices import Location
from .models import ProtocolSubmission, Step, StepStatus, WorkflowStep, describe_step, utcnow
from .registry import KIND_LABEL, STEP_KIND, STORAGE, Instrument, Registry

MAX_PROTOCOL_BYTES = 1_000_000
OPERATION_LABEL = {"liquid_handling": "Run protocol", "read": "Read plate", "wash": "Wash plate", "qpcr": "qPCR run"}


class PlanError(ValueError):
    """The submission cannot be run as given."""


def decode_protocol_source(protocol_b64: str) -> bytes:
    try:
        source = base64.b64decode(protocol_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PlanError("liquid_handling.protocol_b64 is not valid base64") from exc
    if not source.strip():
        raise PlanError("The Opentrons protocol file is empty")
    if len(source) > MAX_PROTOCOL_BYTES:
        raise PlanError(f"The Opentrons protocol is larger than {MAX_PROTOCOL_BYTES // 1000} kB")
    return source


def plan_run(submission: ProtocolSubmission, registry: Registry) -> tuple[list[Step], list[str]]:
    """Validates the submission and returns (steps, warnings)."""
    transport = registry.transport
    plate = submission.plate
    location = Location(STORAGE, plate.storage_slot)
    warnings: list[str] = []
    steps = [Step(key="decode_plan", label="Decode + plan", device="master")]

    for index, op in enumerate(submission.steps):
        where = f"Step {index + 1} ({op.type.replace('_', ' ')})"
        if op.type == "hold":
            steps.append(Step(key="hold", label=op.label or "Hold", device="master",
                              params={"op": index, "seconds": op.seconds, "summary": describe_step(op)}))
            continue

        inst = select_instrument(op, registry, plate.labware, where)
        slot = inst.default_slot
        if op.type == "liquid_handling":
            slot = op.deck_slot or inst.default_slot
            mover = transport.name if transport else "The transport"
            if slot not in inst.plate_slots:
                raise PlanError(
                    f"{mover} cannot place the plate in {inst.name} slot {slot}; it can use {', '.join(inst.plate_slots)}"
                )
            warnings += _check_protocol(op, slot, where)

        dest = Location(inst.id, slot)
        if dest != location:
            if transport is None:
                raise PlanError(f"{where} needs the plate moved from {location} to {dest}, but the registry has no transport")
            to = "Fetch plate" if location.station == STORAGE else "Move plate"
            between = [s for s in (location.station, inst.id) if s != STORAGE]
            steps.append(Step(key="transfer", label=f"{to} to {inst.name}", device=transport.id,
                              params={"from": location.as_dict(), "to": dest.as_dict(), "access": between}))
            steps.append(Step(key="clear", label=f"{inst.name} is clear", device=transport.id,
                              params={"station": inst.id, "access": between}))
            location = dest
        steps.append(Step(key="operation", label=op.label or OPERATION_LABEL[op.type], device=inst.id,
                          params={"op": index, "slot": slot, "summary": describe_step(op)}))

    for n, step in enumerate(steps, start=1):
        step.id = f"{n}-{step.key}"
    decode = steps[0]
    decode.status = StepStatus.done
    decode.started_at = decode.finished_at = utcnow()
    decode.detail = f"Workflow validated; {len(steps) - 1} steps planned"
    return steps, warnings


def select_instrument(op: WorkflowStep, registry: Registry, labware: str, where: str) -> Instrument:
    """The instrument for a workflow step: the one it names, or the first of its kind whose limits allow it."""
    kind = STEP_KIND[op.type]
    if op.instrument:
        inst = registry.get(op.instrument)
        if inst is None:
            raise PlanError(f"{where}: no instrument '{op.instrument}' in the registry")
        if inst.kind != kind:
            raise PlanError(f"{where}: {inst.name} is a {KIND_LABEL[inst.kind].lower()}, not a {KIND_LABEL[kind].lower()}")
        problems = check_limits(op, inst, labware)
        if problems:
            raise PlanError(f"{where}: outside {inst.name}'s limits: {'; '.join(problems)}")
        return inst
    candidates = registry.of_kind(kind)
    if not candidates:
        raise PlanError(f"{where} needs a {KIND_LABEL[kind].lower()}, and the registry has none")
    rejected = []
    for inst in candidates:
        problems = check_limits(op, inst, labware)
        if not problems:
            return inst
        rejected.append(f"{inst.name}: {'; '.join(problems)}")
    raise PlanError(f"{where}: no {KIND_LABEL[kind].lower()} can do it ({' | '.join(rejected)})")


def check_limits(op: WorkflowStep, inst: Instrument, labware: str) -> list[str]:
    """Problems with running `op` on `inst`, from the [instrument.limits] in the registry."""
    limits: dict[str, Any] = inst.limits
    problems = []
    allowed_labware = limits.get("labware")
    if allowed_labware and labware not in allowed_labware:
        problems.append(f"plate {labware} is not one of {', '.join(allowed_labware)}")

    if op.type == "read":
        read = op.settings
        if read.method:
            if limits.get("methods") is not None and read.method not in limits["methods"]:
                problems.append(f"method '{read.method}' is not allowed")
            return problems
        modes = limits.get("modes")
        if modes and read.mode not in modes:
            problems.append(f"{read.mode} is not available (only {', '.join(modes)})")
        if read.mode == "absorbance":
            problems += _outside("wavelength", read.wavelengths_nm, limits.get("wavelength_nm"))
            most = limits.get("max_wavelengths")
            if most and len(read.wavelengths_nm) > most:
                problems.append(f"at most {most} wavelengths per read")
        elif read.mode == "fluorescence":
            problems += _outside("excitation", [read.excitation_nm], limits.get("excitation_nm"))
            problems += _outside("emission", [read.emission_nm], limits.get("emission_nm"))
        elif read.mode == "luminescence":
            problems += _outside("integration", [read.integration_ms], limits.get("integration_ms"), "ms")
    elif op.type == "liquid_handling":
        most_kb = limits.get("max_protocol_kb")
        if most_kb and len(op.protocol_b64) * 3 / 4 > most_kb * 1000:
            problems.append(f"protocol larger than {most_kb} kB")
    elif op.type in ("wash", "qpcr"):
        programs = limits.get("programs")
        if programs is not None and op.program and op.program not in programs:
            problems.append(f"program '{op.program}' is not one of {', '.join(programs) or '(none)'}")
        if op.type == "wash":
            if not op.program and limits.get("programs_only"):
                problems.append("this washer only runs saved programs; name one")
            problems += _outside("volume", [op.volume_ul], [0, limits["max_volume_ul"]] if "max_volume_ul" in limits else None, "uL")
            problems += _outside("cycles", [op.cycles], [1, limits["max_cycles"]] if "max_cycles" in limits else None, "")
    return problems


def _outside(what: str, values: list[Any], bounds: list[float] | None, unit: str = "nm") -> list[str]:
    if not bounds:
        return []
    low, high = bounds
    bad = [v for v in values if v is not None and not low <= v <= high]
    suffix = f" {unit}" if unit else ""
    return [f"{what} {', '.join(map(str, bad))}{suffix} outside {low}-{high}{suffix}"] if bad else []


def _check_protocol(op, slot: str, where: str) -> list[str]:
    try:
        text = decode_protocol_source(op.protocol_b64).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PlanError("The Opentrons protocol must be a UTF-8 Python file") from exc
    if not re.search(r"^def run\(", text, re.MULTILINE):
        raise PlanError(f"{where}: the Opentrons protocol has no top-level run(protocol) function")
    warnings = []
    if not re.search(r"""["']robotType["']\s*:\s*["']Flex["']""", text):
        warnings.append(
            f"{where}: the protocol does not declare requirements = {{'robotType': 'Flex'}}; the Flex may reject it"
        )
    if f'"{slot}"' not in text and f"'{slot}'" not in text:
        warnings.append(f"{where}: the plate will be placed in slot {slot}, but the protocol never mentions that slot")
    return warnings
