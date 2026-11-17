"""Turns a design into the run submission the master node accepts (the protocol IR).

Deterministic, no AI. The master node then selects instruments, checks safety
limits and adds the plate moves (its POST /plan shows that without queuing),
and the scientist approves exactly that plan before it is sent.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any

from .models import Design

DEFAULT_LABWARE = "corning_96_wellplate_360ul_flat"


class WorkflowError(ValueError):
    """The design can't become a run yet."""


def slug(title: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")[:60].lower() or "protocol"


def build_submission(
    design: Design, *, name: str, scientist: str, storage_slot: str, barcode: str = "", notes: str = "", labware: str = ""
) -> dict[str, Any]:
    problems = design.send_problems()
    if problems:
        raise WorkflowError(" ".join(problems))
    steps: list[dict[str, Any]] = []
    plate_labware = ""
    for step in design.workflow:
        common = {"instrument": step.instrument, "label": step.label}
        if step.type == "liquid_handling":
            p = design.protocol(step.protocol_id)
            sim = p.simulation
            plate = sim.deck.plate_at(p.plate_slot)
            plate_labware = plate_labware or (plate.load_name if plate else "")
            steps.append({
                "type": "liquid_handling", **common,
                "protocol_filename": f"{slug(p.title or p.id)}.py",
                "protocol_b64": base64.b64encode(p.code.encode()).decode(),
                "runtime_parameters": {},
                "deck_slot": p.plate_slot,
                "tips": sim.tips,
            })
        elif step.type == "read":
            steps.append({"type": "read", **common, "settings": step.settings.model_dump()})
        else:
            steps.append(step.model_dump(exclude={"id"}))
    summary = "\n\n".join(part for part in (design.summary, notes) if part)
    return {
        "name": name,
        "scientist": scientist,
        "plate": {"barcode": barcode, "storage_slot": storage_slot, "labware": plate_labware or labware or DEFAULT_LABWARE},
        "steps": steps,
        "notes": summary[:2000],
        "analysis": [a.model_dump(mode="json") for a in design.analyses],
    }


def liquid_protocols(design: Design) -> list:
    """The protocols the workflow runs, in the order it runs them (each once)."""
    ids = dict.fromkeys(s.protocol_id for s in design.workflow if s.type == "liquid_handling")
    return [p for pid in ids if (p := design.protocol(pid)) and p.code]


def shared_tubes(design: Design) -> list[str]:
    """Tubes and wells that two protocols of one run declare with different liquids.

    Each protocol declares what its tubes hold when it starts (load_liquid), so the simulator can't see that
    a tube an earlier protocol filled or used is declared as something else later: on the deck it is one tube.
    The sample plate is left out (liquids are meant to build up there)."""
    first: dict[tuple[str, str, str], tuple[str, str]] = {}  # (slot, load name, well) -> (protocol, liquid)
    problems = []
    for p in liquid_protocols(design):
        if not p.simulation_current:
            continue
        deck = p.simulation.deck
        names = {liquid.id: liquid.name for liquid in deck.liquids}
        for slot, contents in deck.slots.items():
            if slot == p.plate_slot:
                continue
            for labware in contents.labware:
                for well, ids in labware.liquids.items():
                    liquid = " + ".join(sorted(names.get(i, i) for i in ids))
                    key = (slot, labware.load_name, well)
                    if key not in first:
                        first[key] = (p.id, liquid)
                    elif first[key][1].strip().lower() != liquid.strip().lower():
                        earlier, what = first[key]
                        problems.append(f"{well} of the {labware.display_name} in {slot}: '{earlier}' declares it as "
                                        f"{what}, '{p.id}' as {liquid}")
    return problems


def tips_before(design: Design, protocol_id: str) -> list[list[dict[str, Any]]]:
    """Tip use of the protocols that run before this one in the workflow (they take their tips first)."""
    out = []
    for p in liquid_protocols(design):
        if p.id == protocol_id:
            break
        if p.simulation_current and p.simulation.tips:
            out.append(p.simulation.tips)
    return out


def plan_hash(submission: dict[str, Any]) -> str:
    """Identifies exactly what would be sent; approval is tied to it."""
    return hashlib.sha256(json.dumps(submission, sort_keys=True).encode()).hexdigest()[:16]


def describe_plan(steps: list[dict[str, Any]], names: dict[str, str]) -> str:
    """The master node's planned steps, as the assistant reads them."""
    lines = []
    for step in steps[1:]:  # skip "Decode + plan"
        device = "master node" if step["device"] == "master" else names.get(step["device"], step["device"])
        summary = step.get("params", {}).get("summary")
        lines.append(f"- {step['label']} ({device})" + (f": {summary}" if summary else ""))
    return "\n".join(lines)
