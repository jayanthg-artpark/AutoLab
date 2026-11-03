"""The lab's experiment notebook: one record per finished run, which the assistant searches.

Each record (knowledge/experiments/<run id>.md) holds what the experiment needed, the
workflow and protocols exactly as they were sent, how the run went, the computed results,
the AI summary if there is one, and the scientist's rating and notes for next time. The
knowledge search ranks records rated "worked" highest and ones nobody rated lowest, and the
assistant reuses what worked and treats what failed as a warning (see the system prompt).

Written without AI: everything in a record is the lab's own data.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .analysis import describe_results
from .models import Design

FOLDER = "experiments"
RATING_LABEL = {"worked": "worked", "partly": "partly worked", "failed": "failed", "": "not reviewed"}
RATING_ADVICE = {
    "worked": "The scientist confirmed this run worked: a good starting point for a similar experiment.",
    "partly": "The scientist says this run only partly worked: reuse it only with the changes noted below.",
    "failed": "The scientist says this run failed: don't reuse it as it is; use it to avoid the same mistake.",
    "": "Nobody has rated this run yet, so its results are unconfirmed.",
}
# The record's second line; knowledge.py reads the rating back from it to rank the record.
RATING_LINE = re.compile(r"^Rating: (worked|partly worked|failed|not reviewed)$", re.MULTILINE)


def record_id(run_id: str) -> str:
    """The knowledge document id of a run's record, e.g. "experiments/run-7.md"."""
    return f"{FOLDER}/{re.sub(r'[^A-Za-z0-9_.-]', '_', run_id)}.md"


def rating_of(text: str) -> str:
    """worked | partly | failed, or "" when not reviewed (or not an experiment record)."""
    match = RATING_LINE.search(text[:2000])
    if not match:
        return ""
    return {label: key for key, label in RATING_LABEL.items()}[match.group(1)]


def write_record(knowledge_root: str | Path, run_id: str, text: str) -> str:
    """Saves the record; returns its knowledge document id."""
    doc = record_id(run_id)
    path = Path(knowledge_root) / doc
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".tmp")
    partial.write_text(text, encoding="utf-8")
    partial.replace(path)  # the knowledge index never sees half a record
    return doc


def experiment_record(
    run: dict[str, Any],
    design: Design | None,
    *,
    analyses: list[dict[str, Any]] | None = None,
    results: dict[str, Any] | None = None,
    note: str = "",
    feedback: dict[str, str] | None = None,
) -> str:
    """The record of a finished run, as Markdown."""
    protocol = run.get("protocol", {})
    rating = (feedback or {}).get("rating", "")
    req = design.requirements if design else None
    lines = [
        f"# Lab experiment: {protocol.get('name') or run['id']}",
        f"Rating: {RATING_LABEL[rating]}",
        "",
        " · ".join(filter(None, [
            f"Run {run['id']}", str(run.get("created_at", ""))[:10], protocol.get("scientist", ""), f"run {run['status']}",
        ])),
        RATING_ADVICE[rating],
    ]
    if req and req.assay:
        lines.append(f"Assay: {req.assay}")

    lines += ["", "## Scientist's notes for next time", (feedback or {}).get("notes") or "(none)"]

    if req:
        lines += ["", "## What the experiment needed"]
        for label, value in (("Goal", req.goal), ("Samples", req.samples), ("Controls", req.controls),
                             ("Replicates", req.replicates), ("Readout", req.readout)):
            if value:
                lines.append(f"- {label}: {value}")
        for label, items in (("Assumptions", req.assumptions), ("Based on", req.sources)):
            if items:
                lines.append(f"- {label}: " + "; ".join(items))

    lines += ["", "## Workflow"]
    if design and design.workflow:
        for n, step in enumerate(design.workflow, start=1):
            what = json.dumps(step.settings.model_dump()) if step.type == "read" else design.describe_step(step)
            lines.append(f"{n}. {step.type}" + (f" '{step.label}'" if step.label else "") + f": {what}")
    else:
        for n, step in enumerate(protocol.get("steps", []), start=1):
            lines.append(f"{n}. {step.get('type')}" + (f" '{step['label']}'" if step.get("label") else ""))

    lines += ["", "## How the run went", f"Status: {run['status']}"]
    if run.get("error"):
        lines.append(f"Error: {run['error']}")
    for step in run.get("steps", []):
        if step.get("status") in ("failed", "skipped") or step.get("key") == "operation":
            lines.append(f"- {step['label']} [{step['device']}]: {step['status']}" + (f" ({step['detail']})" if step.get("detail") else ""))

    result_lines = describe_results(analyses or [])
    if not result_lines:
        for m in (results or {}).get("measurements", []):
            numbers = [v for v in m["values"].values() if v is not None]
            stats = f"{len(numbers)} wells read"
            if numbers:
                stats += f", min {min(numbers):.4g}, max {max(numbers):.4g}, mean {sum(numbers) / len(numbers):.4g}"
            result_lines.append(f"- {m['label']} ({m.get('unit', '')}): {stats}")
    if result_lines:
        lines += ["", "## Results", *result_lines]

    if note:
        lines += ["", "## AI summary of the run", note]

    for p in design.protocols if design else []:
        if not p.code:
            continue
        lines += ["", f"## Protocol: {p.title or p.id} (plate slot {p.plate_slot})"]
        if p.summary:
            lines.append(p.summary)
        lines += ["```python", p.code.rstrip(), "```"]
    return "\n".join(lines) + "\n"
