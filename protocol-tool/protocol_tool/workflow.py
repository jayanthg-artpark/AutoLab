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
