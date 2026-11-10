"""Fills the knowledge folder from public sources, so the assistant can learn from other labs.

  python -m protocol_tool.knowledge_import opentrons-library
      Every Flex protocol in the Opentrons Protocol Library (github.com/Opentrons/Protocols):
      description, default parameters and code -> knowledge/protocols/opentrons-library/
  python -m protocol_tool.knowledge_import opentrons-docs
      The Opentrons Python API documentation for the Flex (Apache-2.0)
      -> knowledge/reference/opentrons-api/
  python -m protocol_tool.knowledge_import opentrons-labware
      Every standard Opentrons labware definition (plates, tube racks, reservoirs, Flex tip racks,
      adapters): load name, wells, volumes and depths, from the installed opentrons package
      (no download) -> knowledge/reference/opentrons-labware/
  python -m protocol_tool.knowledge_import protocols-io "ELISA" "qPCR" --limit 25
      Public protocols on protocols.io matching each search (CC BY 4.0; needs a free token,
      PROTOCOLS_IO_TOKEN in .env) -> knowledge/protocols/protocols-io/
  python -m protocol_tool.knowledge_import github owner/repo [owner/repo ...]
      Every Flex protocol in another lab's public GitHub repository, with the repository's
      license -> knowledge/protocols/github/
  python -m protocol_tool.knowledge_import url https://example.com/kit-manual.pdf [--folder manuals]
      Any document on the web (kit manual, IFU, paper; PDF, HTML or text).

Each imported document records its source, authors and terms (in the document and in
knowledge/sources.json), so the assistant can cite it. Running an import again
refreshes it. Downloads are only read as text, never run. The lab has only a Flex, so
protocols written for other robots, and the parts of the docs about them, are left out.
"""

from __future__ import annotations

import argparse
import html
import io
import json
import logging
import os
import re
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from .config import load_settings
from .knowledge import SOURCES_FILE
from .model_status import load_key_file

log = logging.getLogger(__name__)

OPENTRONS_LIBRARY_ZIP = "https://codeload.github.com/Opentrons/Protocols/zip/refs/heads/develop"
OPENTRONS_TREE = "https://api.github.com/repos/Opentrons/opentrons/git/trees/edge?recursive=1"
OPENTRONS_RAW = "https://raw.githubusercontent.com/Opentrons/opentrons/edge/"
# The Python Protocol API docs (Markdown, MkDocs) and the official example protocols.
OPENTRONS_DOCS_DIR = "docs/python-api/docs/"
OPENTRONS_EXAMPLES_DIR = "api/docs/v2/example_protocols/"
PROTOCOLS_IO = "https://www.protocols.io"
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024
MAX_CODE_CHARS = 40_000
USER_AGENT = "lab-protocol-tool knowledge import"

LIBRARY_TERMS = (
    "From the Opentrons Protocol Library (Opentrons and community authors). Kept as an internal "
    "reference for this lab; credit the authors and don't republish."
)
DOCS_LICENSE = "Apache-2.0 (Opentrons)"
PROTOCOLS_IO_LICENSE = "CC BY 4.0 (protocols.io public content): cite the authors and DOI"

# The lab has only a Flex: anything about the other Opentrons robot is left out of the knowledge.
OTHER_ROBOT = re.compile(r"\bOT-?2\b", re.IGNORECASE)
# Pipettes, tip racks and modules only the other robot uses, as they appear in code and tables.
OTHER_ROBOT_HARDWARE = re.compile(
    r"\bp(10|20|50|300|1000)_(single|multi)(_gen[12])?\b|\bopentrons_96_(filter)?tiprack_\d|\bmagdeck\b"
    r"|\bmagnetic module( gen[12])?\b|\btemperature module gen1\b|\bthermocycler module gen1\b", re.IGNORECASE)
# "OT-3" is what older Flex protocols declare.
FLEX_CODE = re.compile(r"""["']robotType["']\s*:\s*["'](Flex|OT-3)["']""")
# API docs pages only about hardware the Flex can't use.
OTHER_ROBOT_PAGES = {"adapting-ot2-flex", "modules-magnetic-module", "reference-magnetic-module"}


class ImportProblem(Exception):
    """An import can't go ahead (missing token, bad URL, download failed)."""


def slugify(text: str, limit: int = 80) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit] or "document"


class Sources:
    """knowledge/sources.json: where each imported document came from."""

    def __init__(self, root: Path):
        self.path = root / SOURCES_FILE
        try:
            self.entries: dict[str, dict] = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except ValueError:
            self.entries = {}

    def add(self, doc_id: str, **info: Any) -> None:
        self.entries[doc_id] = {**info, "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(dict(sorted(self.entries.items())), indent=1), encoding="utf-8")


def _write(root: Path, doc_id: str, text: str) -> None:
    path = root / doc_id
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _client(timeout: float = 60.0, transport: httpx.BaseTransport | None = None, headers: dict | None = None) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True, transport=transport,
                        headers={"User-Agent": USER_AGENT, **(headers or {})})


def _download(client: httpx.Client, url: str, dest: io.BufferedIOBase, progress: Callable[[str], None]) -> None:
    with client.stream("GET", url) as response:
        if response.is_error:
            raise ImportProblem(f"{url} returned {response.status_code}")
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise ImportProblem(f"{url} is larger than {MAX_DOWNLOAD_BYTES // 2**20} MB")
            dest.write(chunk)
            if total // (5 * 2**20) != (total - len(chunk)) // (5 * 2**20):
                progress(f"  downloaded {total // 2**20} MB")


# --- Opentrons Protocol Library --------------------------------------------------------------


def import_opentrons_library(root: Path, *, transport: httpx.BaseTransport | None = None,
                             progress: Callable[[str], None] = print) -> int:
    """Every Python API v2 protocol in the library, one Markdown document each."""
    progress("Downloading the Opentrons Protocol Library (github.com/Opentrons/Protocols)...")
    with tempfile.TemporaryFile() as archive, _client(300, transport) as client:
        _download(client, OPENTRONS_LIBRARY_ZIP, archive, progress)
        archive.seek(0)
        with zipfile.ZipFile(archive) as z:
            return _library_from_zip(root, z, progress)


def _library_from_zip(root: Path, z: zipfile.ZipFile, progress: Callable[[str], None]) -> int:
    folders: dict[str, dict[str, zipfile.ZipInfo]] = {}
    for info in z.infolist():
        parts = info.filename.split("/")
        # <repo>-<branch>/protocols/<name>/<file>
        if len(parts) == 4 and parts[1] == "protocols" and parts[3] and not info.is_dir():
            folders.setdefault(parts[2], {})[parts[3]] = info
    sources = Sources(root)
    target = root / "protocols" / "opentrons-library"
    count = 0
    for old in target.glob("*.md"):  # refreshed below; protocols for other robots stay out
        old.unlink()
        sources.entries.pop(f"protocols/opentrons-library/{old.name}", None)
    for name, files in sorted(folders.items()):
        code_name = next((f for f in files if f.endswith(".flex.py") or f.endswith(".apiv2.py")), None)
        if code_name is None or ".hide-from-search" in files or "README.md" not in files:
            continue  # API v1 only, hidden from the library, or undocumented
        readme = z.read(files["README.md"]).decode("utf-8", errors="replace").strip()
        code = z.read(files[code_name]).decode("utf-8", errors="replace")
        if ".flex." not in code_name and not FLEX_CODE.search(code):
            continue  # written for another robot
        if len(code) > MAX_CODE_CHARS:
            code = code[:MAX_CODE_CHARS] + "\n# ... (cut: longer than the knowledge import keeps)"
        title = next((line.lstrip("#").strip() for line in readme.splitlines() if line.startswith("#")), name)
        url = f"https://library.opentrons.com/p/{name}"
        parts = [
            f"# {title}",
            "",
            f"Source: Opentrons Protocol Library, {url}",
            "Robot: Flex, Opentrons Python API v2.",
            f"Terms: {LIBRARY_TERMS}",
            "",
            readme,
        ]
        if "fields.json" in files:
            fields = z.read(files["fields.json"]).decode("utf-8", errors="replace")
            if len(fields) < 6000:
                parts += ["", "## Parameters (fields.json; the code reads them with get_values)", "```json", fields.strip(), "```"]
        parts += ["", f"## Protocol code ({code_name})", "```python", code.rstrip(), "```", ""]
        doc_id = f"protocols/opentrons-library/{slugify(name)}.md"
        _write(root, doc_id, "\n".join(parts))
        sources.add(doc_id, source="Opentrons Protocol Library", url=url, license=LIBRARY_TERMS, robot="Flex")
        count += 1
        if count % 200 == 0:
            progress(f"  {count} protocols written")
    sources.save()
    progress(f"Imported {count} Flex protocols into {target}")
    return count


# --- Opentrons Protocol Library, downloaded from its website --------------------------------------

LIBRARY_FOLDER = "protocols/opentrons-flex-library"


def _metadata(code: str) -> dict[str, Any]:
    """The protocol's metadata dict, read without running the code (only plain literals)."""
    import ast
    import warnings

    try:
        with warnings.catch_warnings():  # e.g. invalid escapes in the downloaded code
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(code)
    except SyntaxError:
        return {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "metadata" for t in node.targets):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                return {}
            return value if isinstance(value, dict) else {}
    return {}


def import_library_folder(root: Path, folder: Path, *, progress: Callable[[str], None] = print) -> int:
    """Flex protocols downloaded from library.opentrons.com into one folder: the .py files, with the
    download's manifest.csv (name, slug, saved_as) for each protocol's title and library page."""
    import csv

    folder = Path(folder)
    if not folder.is_dir():
        raise ImportProblem(f"{folder} is not a folder")
    names: dict[str, tuple[str, str]] = {}  # file name -> (title, slug)
    manifest = folder / "manifest.csv"
    if manifest.exists():
        with manifest.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("status") == "downloaded" and row.get("saved_as"):
                    names[Path(row["saved_as"].replace("\\", "/")).name] = (row.get("name") or "", row.get("slug") or "")
    sources = Sources(root)
    target = root / LIBRARY_FOLDER
    for old in target.glob("*.md"):  # refreshed below
        old.unlink()
        sources.entries.pop(f"{LIBRARY_FOLDER}/{old.name}", None)
    count = skipped = 0
    for path in sorted(folder.glob("*.py")):
        code = path.read_text(encoding="utf-8", errors="replace")
        if not FLEX_CODE.search(code):
            skipped += 1  # written for another robot
            continue
        meta = _metadata(code)
        title, slug = names.get(path.name, ("", ""))
        title = title or str(meta.get("protocolName") or path.stem)
        url = f"https://library.opentrons.com/p/{slug}" if slug else "https://library.opentrons.com/"
        if len(code) > MAX_CODE_CHARS:
            code = code[:MAX_CODE_CHARS] + "\n# ... (cut: longer than the knowledge import keeps)"
        parts = [f"# {title}", "", f"Source: Opentrons Protocol Library, {url}", "Robot: Flex, Opentrons Python API v2.",
                 f"Terms: {LIBRARY_TERMS}", ""]
        if meta.get("author"):
            parts.append(f"Author: {meta['author']}")
        if meta.get("description"):
            parts += ["", str(meta["description"]).strip()]
        parts += ["", f"## Protocol code ({path.name})", "```python", code.rstrip(), "```", ""]
        doc_id = f"{LIBRARY_FOLDER}/{slugify(slug or path.stem)}.md"
        _write(root, doc_id, "\n".join(parts))
        sources.add(doc_id, source="Opentrons Protocol Library", url=url, license=LIBRARY_TERMS, robot="Flex")
        count += 1
    sources.save()
    progress(f"Imported {count} Flex protocols into {target}" + (f" ({skipped} for other robots left out)" if skipped else ""))
    return count


# --- Opentrons Python API documentation ---------------------------------------------------------


def import_opentrons_docs(root: Path, *, transport: httpx.BaseTransport | None = None,
                          progress: Callable[[str], None] = print) -> int:
    """The Python Protocol API docs and official example protocols from the Opentrons monorepo."""
    headers = {"Accept": "application/vnd.github+json"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    sources = Sources(root)
    folder = root / "reference" / "opentrons-api"
    count = 0
    progress("Downloading the Opentrons Python API documentation (github.com/Opentrons/opentrons)...")
    with _client(60, transport, headers) as client:
        tree = client.get(OPENTRONS_TREE)
        if tree.is_error:
            raise ImportProblem(f"GitHub returned {tree.status_code}: {tree.text[:200]}"
                                + (" (rate limit: set GITHUB_TOKEN, or try again in an hour)" if tree.status_code == 403 else ""))
        paths = [e["path"] for e in tree.json().get("tree", []) if e.get("type") == "blob"]
        pages = [p for p in paths if p.startswith(OPENTRONS_DOCS_DIR) and p.endswith(".md")]
        examples = [p for p in paths if p.startswith(OPENTRONS_EXAMPLES_DIR) and p.endswith(".py")]
        if not pages:
            raise ImportProblem(f"No documentation found under {OPENTRONS_DOCS_DIR}; the Opentrons repository layout has changed")
        for old in folder.glob("*.md"):  # refreshed below; pages only about other robots stay out
            old.unlink()
            sources.entries.pop(f"reference/opentrons-api/{old.name}", None)
        for path in pages + examples:
            if path in examples and "flex" not in Path(path).stem:
                continue  # an example for another robot
            if slugify(path.removeprefix(OPENTRONS_DOCS_DIR).removesuffix(".md")) in OTHER_ROBOT_PAGES:
                continue
            response = client.get(OPENTRONS_RAW + path)
            if response.is_error:
                progress(f"  skipped {path}: {response.status_code}")
                continue
            text = response.text
            if path in examples and _other_robot_code(text):
                continue  # uses the other robot's pipettes or tips
            if path not in examples and not flex_only(_without_front_matter(text)).strip():
                continue  # nothing about the Flex
            if path in examples:
                name = Path(path).stem
                url = f"https://github.com/Opentrons/opentrons/blob/edge/{path}"
                doc_id = f"reference/opentrons-api/example-{slugify(name)}.md"
                body = [f"# Official example protocol: {name} (Flex)", "", f"Source: {url}", f"License: {DOCS_LICENSE}", "",
                        "```python", text.rstrip(), "```", ""]
            else:
                rel = path.removeprefix(OPENTRONS_DOCS_DIR).removesuffix(".md")
                url = "https://docs.opentrons.com/python-api/" + (rel.removesuffix("index").rstrip("/") + "/" if rel != "index" else "")
                title = _md_title(text) or rel
                doc_id = f"reference/opentrons-api/{slugify(rel)}.md"
                body = [f"# {title}", "", f"Source: Opentrons Python Protocol API documentation, {url}",
                        f"License: {DOCS_LICENSE}", "", flex_only(_without_front_matter(text)).strip(), ""]
            _write(root, doc_id, "\n".join(body))
            sources.add(doc_id, source="Opentrons Python API docs", url=url, license=DOCS_LICENSE)
            count += 1
    sources.save()
    progress(f"Imported {count} documentation pages and examples into {folder}")
    return count


def flex_only(markdown: str) -> str:
    """The docs page without what is about the other Opentrons robot: its sections, tabs, list items,
    code examples, table rows and the sentences that mention it."""
    out: list[str] = []
    code: list[str] | None = None  # an open code block, kept only if it is all Flex
    skip_indent: int | None = None  # dropping the indented content of a tab or list item about the other robot
    skip_level = 0  # dropping a section whose heading names the other robot
    for line in markdown.splitlines():
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if code is not None:
            code.append(line)
            if stripped.startswith(("```", "~~~")):
                if not _other_robot_code("\n".join(code)):
                    out.extend(code)
                code = None
            continue
        if skip_indent is not None:
            if not stripped or indent > skip_indent:
                continue
            skip_indent = None
        heading = re.match(r"(#{1,6})\s", line)
        if heading:
            if skip_level and len(heading.group(1)) <= skip_level:
                skip_level = 0
            if OTHER_ROBOT.search(line):
                skip_level = len(heading.group(1))
        if skip_level:
            continue
        if stripped.startswith(("```", "~~~")):
            code = [line]
        elif stripped.startswith("|") and OTHER_ROBOT_HARDWARE.search(line):
            continue  # a table row about the other robot's hardware
        elif not OTHER_ROBOT.search(line):
            out.append(line)
        elif re.match(r'===\s+"|([-*+]|\d+\.)\s', stripped):
            skip_indent = indent  # a tab or list item about the other robot, with what is indented under it
        elif not stripped.startswith("|"):  # table rows about the other robot go
            kept = [part for part in re.split(r"(?<=[.!?])\s+", stripped) if not OTHER_ROBOT.search(part)]
            if kept:
                out.append(line[:indent] + " ".join(kept))
    if code and not _other_robot_code("\n".join(code)):
        out.extend(code)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


def _other_robot_code(code: str) -> bool:
    return bool(OTHER_ROBOT.search(code) or OTHER_ROBOT_HARDWARE.search(code))


# --- Opentrons labware definitions --------------------------------------------------------------------

LABWARE_DIR = "reference/opentrons-labware"
LABWARE_CATEGORIES = {
    "wellPlate": "well plate", "tubeRack": "tube rack", "reservoir": "reservoir", "tipRack": "tip rack",
    "aluminumBlock": "aluminum block", "adapter": "adapter", "lid": "lid", "other": "other labware",
}
LIQUID_HEIGHT_NOTE = (
    "Liquid height: the robot knows the well's shape, not how full it is. Unless told otherwise it aspirates "
    "1 mm above the bottom, so in a deep tube or reservoir the tip, and even the pipette, goes deep into the liquid. "
    "Aspirate just below the surface instead: with the volume known, declare it with well.load_liquid(liquid, "
    "volume_ul) and aspirate at well.meniscus(z=-2, target=\"end\"). A tip reaches only its own length below the "
    "pipette (1000 µL tips 86 mm, 50 and 200 µL tips 48 mm): deeper than that, the pipette itself hits the tube. In 15 "
    "and 50 mL tubes declare the volume (ask the scientist if it wasn't given), keep the level within reach and "
    "dispense near the top: see reference/opentrons-labware/liquid-height.md."
)
LIQUID_HEIGHT_PAGE = """\
# Aspirating at the liquid surface: tubes, reservoirs and deep wells

How this lab aspirates from tubes, and how Opentrons handles liquid height (Python API 2.21 and later, Flex).

## What the robot does by default

The robot knows each well's shape from its labware definition, not how much liquid is in it. aspirate(volume, well)
goes to 1 mm above the bottom of the well (pipette.well_bottom_clearance.aspirate changes the 1 mm). In a 15 or
50 mL conical tube that is about 110 mm under the rim: the tip, and in a full tube the pipette itself, goes deep
into the liquid, liquid coats the outside of the tip, and volumes are less accurate. A fixed height higher up, such as
well.bottom(40) or well.top(-5), only works for one fill level: once the level drops below it, the pipette draws air.

## The lab's rule

From 15 and 50 mL tubes (wells deeper than 60 mm), always aspirate just below the liquid surface (the meniscus).
The simulation check fails any other aspirate from them. Shallower wells (plates, 1.5 and 2 mL tubes, reservoirs)
may use the default, but the same methods work there too.

## Volume in the tube known

Declare the starting volume once, then aspirate relative to the meniscus. The robot works out the surface from the
tube's shape and lowers it as it takes liquid, so every aspirate stays just below the surface:

```python
buffer = protocol.define_liquid("Buffer", display_color="#4a90d9")
tube = rack["A1"]
tube.load_liquid(buffer, 40000)  # 40 mL, in µL
for well in plate.wells()[:8]:
    pipette.pick_up_tip()
    pipette.aspirate(30, tube.meniscus(z=-2, target="end"))  # 2 mm below where the surface ends up
    pipette.dispense(30, well)
    pipette.drop_tip()
```

target="end" aims at where the surface will be after this aspirate, so the tip is still in liquid at the end;
target="start" aims at the surface before it. Without load_liquid (or a measurement) the simulation fails with
LiquidHeightUnknownError, so a missing volume is caught before the run.

## How deep a tip reaches

A tip reaches only its own length below the pipette: 1000 µL tips 86 mm, 200 µL tips 48.6 mm, 50 µL tips 47.8 mm.
Deeper than that below the top of a tube or well, the pipette itself (and, with a partial tip layout, the nozzles
without tips) hits the tube or the rack. A 50 or 15 mL Falcon tube is about 113 mm deep, so:

- Take liquid from these tubes with 1000 µL tips. Even they don't reach the bottom 27 mm or so of a 50 mL tube,
  so the tube needs liquid left over at the end of the run. Tell the scientist how much to put in.
- 50 and 200 µL tips reach only the top 48 mm: the tube must stay nearly full.
- Dispense into a tube near the top, tube.top(-5), never at the default 1 mm above the bottom.

The simulation check follows the level as the protocol takes liquid and fails any aspirate or dispense that goes
deeper than the tip reaches. It needs the volume in each tube for that, from load_liquid.

## Volume not known

In 15 and 50 mL tubes, ask the scientist for the volume. measure_liquid_height() finds the level only during the
run, so the check can't show that the tip stays within reach, and fails.

In plates and reservoirs, where any tip reaches the bottom, the Flex can find the surface itself:
measure_liquid_height() lowers a fresh tip from the top of the well until its pressure sensor touches liquid,
records the height, and the protocol aspirates from there. Pick up a new tip right before each measurement (a wet
tip reads wrongly); the tip used to measure can then aspirate.

```python
pipette.pick_up_tip()
pipette.measure_liquid_height(reservoir["A1"])  # Flex 1-, 8- (A1 and H1 channels) and 96-channel pipettes
pipette.aspirate(30, reservoir["A1"].meniscus(z=-2, target="end"))
pipette.dispense(30, plate["A1"])
pipette.drop_tip()
```

After one measurement the robot keeps tracking the level for later aspirates from the same well. If the well is
empty the Flex stops and asks for liquid (error recovery). In the simulation the measurement always succeeds, and
the level is taken to be at the bottom.

## Notes

- transfer(), distribute() and consolidate() accept the same location: pipette.transfer(30, tube.meniscus(z=-2,
  target="end"), plate["A1"]).
- Use z=-2 to z=-5: deep enough to stay in liquid while aspirating, shallow enough to keep the outside of the tip dry.
- pipette.liquid_presence_detection = True only checks that there is liquid before each aspirate; it does not move the
  aspirate height. Use measure_liquid_height() for that.
- Dispense into an empty plate as usual (1 mm above the bottom, or well.top(-1) for drops from above). Into
  tubes, dispense near the top: tube.top(-5).
"""


def _volume(ul: float) -> str:
    return f"{ul / 1000:g} mL ({ul:g} µL)" if ul >= 1000 else f"{ul:g} µL"


def _labware_doc(d: dict, version: int, opentrons_version: str) -> str:
    """One labware definition as a short Markdown page: what it is, how to load it, its wells."""
    name, meta, wells = d["parameters"]["loadName"], d["metadata"], d["wells"]
    category = LABWARE_CATEGORIES.get(meta["displayCategory"], meta["displayCategory"])
    rows = sorted({w.rstrip("0123456789") for w in wells}, key=lambda r: (len(r), r))
    brand = d.get("brand", {})
    dims = d["dimensions"]
    lines = [
        f"# Labware: {meta['displayName']} ({name})", "",
        f"Load: protocol.load_labware(\"{name}\", \"D1\")  (any free slot)",
        f"Category: {category}. Brand: {brand.get('brand', '')}"
        + (f" (catalogue {', '.join(brand['brandId'])})" if brand.get("brandId") else ""),
        f"Size: {dims['xDimension']:g} x {dims['yDimension']:g} mm, {dims['zDimension']:g} mm tall",
    ]
    if wells:
        lines.append(f"Wells: {len(wells)} in {len(rows)} rows ({rows[0]}-{rows[-1]}) x {len(d['ordering'])} columns")
    groups = [g for g in d.get("groups") or [{"wells": list(wells), "metadata": {}}] if g["wells"]]
    for group in groups:
        first = wells[group["wells"][0]]
        size = (f"{first['diameter']:g} mm across" if first["shape"] == "circular"
                else f"{first['xDimension']:g} x {first['yDimension']:g} mm")
        bottom = group["metadata"].get("wellBottomShape", "")
        what = f"{len(group['wells'])} x {group['metadata']['displayName']}" if len(groups) > 1 else "Each well"
        where = f" in {', '.join(group['wells'])}" if len(groups) > 1 else ""
        lines.append(f"{what}{where}: holds {_volume(first['totalLiquidVolume'])}, {first['depth']:g} mm deep, "
                     f"{first['shape']} {size}" + (f", {bottom} bottom" if bottom else "")
                     + f"; its bottom is {first['z']:g} mm above the slot")
    if d["parameters"].get("isTiprack"):
        lines.append(f"Tip length: {d['parameters']['tipLength']:g} mm. Give it to a pipette with "
                     f"load_instrument(..., tip_racks=[rack]).")
    if adapters := [a for a in d.get("stackingOffsetWithLabware", {}) if a != name]:
        lines.append(f"Sits on: {', '.join(adapters)}")
    if modules := list(d.get("stackingOffsetWithModule", {})):
        lines.append(f"Fits modules: {', '.join(modules)}")
    if category in ("tube rack", "reservoir") or (category in ("well plate", "aluminum block")
                                                   and max((w["depth"] for w in wells.values()), default=0) > 25):
        lines += ["", LIQUID_HEIGHT_NOTE]
    lines += ["", f"Source: Opentrons labware definition {name} v{version}, from the opentrons {opentrons_version} package. "
                  f"License: {DOCS_LICENSE}", ""]
    return "\n".join(lines)


def import_opentrons_labware(root: Path, *, progress: Callable[[str], None] = print) -> int:
    """Every standard Opentrons labware definition a Flex protocol can load, from the installed opentrons package."""
    import opentrons
    from opentrons_shared_data.labware import list_definitions, load_definition

    latest: dict[str, tuple[int, int]] = {}
    for name, version, schema in list_definitions():
        latest[name] = max(latest.get(name, (0, 0)), (version, schema))
    sources = Sources(root)
    folder = root / LABWARE_DIR
    for old in folder.glob("*.md"):  # definitions an upgrade dropped
        old.unlink()
        sources.entries.pop(f"{LABWARE_DIR}/{old.name}", None)
    index: dict[str, list[str]] = {}
    for name, (version, schema) in sorted(latest.items()):
        d = load_definition(name, version, schema)
        category = d["metadata"]["displayCategory"]
        # Fixed trash and test definitions aren't loaded with load_labware; only Flex tip racks fit Flex pipettes.
        if category not in LABWARE_CATEGORIES or name.startswith("schema3test") or (
                d["parameters"].get("isTiprack") and not name.startswith("opentrons_flex_")):
            continue
        doc_id = f"{LABWARE_DIR}/{name}.md"
        _write(root, doc_id, _labware_doc(d, version, opentrons.__version__))
        sources.add(doc_id, source="Opentrons labware library", url=f"https://labware.opentrons.com/{name}",
                    license=DOCS_LICENSE)
        index.setdefault(LABWARE_CATEGORIES[category], []).append(f"- {name}: {d['metadata']['displayName']}")
    lines = ["# Labware: every standard Opentrons labware a Flex protocol can load", "",
             f"From the opentrons {opentrons.__version__} package. Each has its own page in {LABWARE_DIR}/ with its "
             "wells, volumes and depths. Pick the definition that matches what is really on the deck: never use "
             "another labware as a stand-in (a 15 mL tube rack for 50 mL tubes puts the pipette in the wrong place).", ""]
    for category, entries in index.items():
        lines += [f"## {category[0].upper()}{category[1:]}s", "", *entries, ""]
    _write(root, f"{LABWARE_DIR}/index.md", "\n".join(lines))
    sources.add(f"{LABWARE_DIR}/index.md", source="Opentrons labware library", url="https://labware.opentrons.com/",
                license=DOCS_LICENSE)
    _write(root, f"{LABWARE_DIR}/liquid-height.md", LIQUID_HEIGHT_PAGE)
    sources.add(f"{LABWARE_DIR}/liquid-height.md", source="This lab, from the Opentrons Python API docs",
                url="https://docs.opentrons.com/python-api/building-block-commands/liquids/", license=DOCS_LICENSE)
    sources.save()
    count = sum(len(entries) for entries in index.values())
    progress(f"Imported {count} labware definitions into {folder}")
    return count


def _without_front_matter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:]
    return text


def _md_title(text: str) -> str:
    match = re.search(r"^title:\s*['\"]?(.+?)['\"]?\s*$", text[:2000], re.MULTILINE) if text.startswith("---") else None
    if match:
        return match.group(1)
    for line in _without_front_matter(text).splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


# --- Any lab's GitHub repository of Opentrons protocols -----------------------------------------------


def import_github(root: Path, repo: str, *, transport: httpx.BaseTransport | None = None,
                  progress: Callable[[str], None] = print) -> int:
    """Every Opentrons protocol (.py with a run() function) in a public GitHub repository, with its license."""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ImportProblem("Give the repository as owner/name, e.g. openplant/openplant_automation_protocols")
    headers = {"Accept": "application/vnd.github+json"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    sources = Sources(root)
    count = 0
    with _client(60, transport, headers) as client:
        info = client.get(f"https://api.github.com/repos/{repo}")
        if info.is_error:
            raise ImportProblem(f"GitHub returned {info.status_code} for {repo}")
        meta = info.json()
        branch = meta.get("default_branch", "main")
        spdx = ((meta.get("license") or {}).get("spdx_id") or "").replace("NOASSERTION", "")
        license = spdx or "no license stated: internal reference only, credit the authors, don't republish"
        tree = client.get(f"https://api.github.com/repos/{repo}/git/trees/{branch}", params={"recursive": "1"})
        if tree.is_error:
            raise ImportProblem(f"GitHub returned {tree.status_code} listing {repo}")
        paths = [e["path"] for e in tree.json().get("tree", []) if e.get("type") == "blob" and e["path"].endswith(".py")]
        progress(f"{repo} ({license}): checking {len(paths)} Python files...")
        for path in paths:
            response = client.get(f"https://raw.githubusercontent.com/{repo}/{branch}/{path}")
            if response.is_error:
                continue
            code = response.text
            if "opentrons" not in code or not re.search(r"^def run\(", code, re.MULTILINE):
                continue  # not an Opentrons protocol
            if not FLEX_CODE.search(code):
                continue  # written for another robot
            if len(code) > MAX_CODE_CHARS:
                code = code[:MAX_CODE_CHARS] + "\n# ... (cut: longer than the knowledge import keeps)"
            name_match = re.search(r"""["']protocolName["']\s*:\s*["'](.+?)["']""", code)
            title = name_match.group(1) if name_match else Path(path).stem.replace("_", " ")
            url = f"https://github.com/{repo}/blob/{branch}/{path}"
            doc_id = f"protocols/github/{slugify(repo, 50)}/{slugify(path.removesuffix('.py'), 80)}.md"
            _write(root, doc_id, "\n".join([
                f"# {title}", "", f"Source: {url}", "Robot: Flex, Opentrons Python API.",
                f"License: {license}", "", "```python", code.rstrip(), "```", "",
            ]))
            sources.add(doc_id, source=f"github.com/{repo}", url=url, license=license, robot="Flex")
            count += 1
    sources.save()
    progress(f"Imported {count} Flex protocols from {repo}")
    return count


# --- protocols.io --------------------------------------------------------------------------------


def import_protocols_io(root: Path, queries: list[str], *, limit: int = 20, token: str | None = None,
                        transport: httpx.BaseTransport | None = None, progress: Callable[[str], None] = print) -> int:
    """Public protocols matching each query, with their steps, one Markdown document each."""
    load_key_file("PROTOCOLS_IO_TOKEN")
    token = token or os.environ.get("PROTOCOLS_IO_TOKEN")
    if not token:
        raise ImportProblem(
            "protocols.io needs an access token. Sign in at https://www.protocols.io/developers, copy the "
            "client access token, and add PROTOCOLS_IO_TOKEN=<token> to protocol-tool\\.env."
        )
    sources = Sources(root)
    count = 0
    with _client(60, transport, {"Authorization": f"Bearer {token}"}) as client:
        for query in queries:
            progress(f"Searching protocols.io for '{query}'...")
            found = _get(client, f"{PROTOCOLS_IO}/api/v3/protocols",
                         params={"filter": "public", "key": query, "page_size": min(limit, 100), "order_field": "relevance"})
            for item in (found.get("items") or [])[:limit]:
                try:
                    doc_id = _protocols_io_one(client, root, item, sources)
                except ImportProblem as exc:
                    progress(f"  skipped '{item.get('title')}': {exc}")
                    continue
                if doc_id:
                    count += 1
                    progress(f"  {doc_id}")
    sources.save()
    progress(f"Imported {count} protocols.io protocols into {root / 'protocols' / 'protocols-io'}")
    return count


def _get(client: httpx.Client, url: str, **kwargs: Any) -> dict:
    response = client.get(url, **kwargs)
    if response.status_code == 401:
        raise ImportProblem("protocols.io refused the token (401); check PROTOCOLS_IO_TOKEN")
    if response.is_error:
        raise ImportProblem(f"{url} returned {response.status_code}: {response.text[:200]}")
    data = response.json()
    if isinstance(data, dict) and data.get("status_code") not in (None, 0):
        raise ImportProblem(f"{url}: {data.get('error_message') or data.get('status_text') or data['status_code']}")
    return data


def _protocols_io_one(client: httpx.Client, root: Path, item: dict, sources: Sources) -> str | None:
    pid = item.get("id")
    if not pid:
        return None
    data = _get(client, f"{PROTOCOLS_IO}/api/v4/protocols/{pid}", params={"content_format": "markdown"})
    protocol = data.get("payload") or data.get("protocol") or {}
    if protocol.get("public") is False:
        return None
    steps = protocol.get("steps")
    if not steps:
        steps = _get(client, f"{PROTOCOLS_IO}/api/v4/protocols/{pid}/steps", params={"content_format": "markdown"}).get("steps") or []
    title = _plain(protocol.get("title") or item.get("title") or f"protocols.io {pid}")
    authors = ", ".join(a.get("name", "") for a in (protocol.get("authors") or item.get("authors") or []) if a.get("name"))
    doi = protocol.get("doi") or item.get("doi") or ""
    url = protocol.get("url") or (f"https://www.protocols.io/view/{protocol.get('uri') or item.get('uri')}" if (protocol.get("uri") or item.get("uri")) else "")
    lines = [
        f"# {title}", "",
        f"Source: protocols.io, {url}" + (f" (doi:{doi.removeprefix('dx.doi.org/')})" if doi else ""),
        f"Authors: {authors or 'see the source'}",
        f"License: {PROTOCOLS_IO_LICENSE}", "",
    ]
    for key, heading in (("description", "Description"), ("guidelines", "Guidelines"), ("before_start", "Before start"),
                         ("warning", "Warnings"), ("materials_text", "Materials")):
        text = _plain(protocol.get(key))
        if text:
            lines += [f"## {heading}", text, ""]
    lines.append("## Steps")
    section = None
    for n, step in enumerate(steps, start=1):
        sec = _plain(step.get("section"))
        if sec and sec != section:
            lines += ["", f"### {sec}"]
            section = sec
        text = _plain(step.get("step")) or _components_text(step.get("components"))
        lines.append(f"{n}. {text}")
    doc_id = f"protocols/protocols-io/{pid}-{slugify(title, 60)}.md"
    _write(root, doc_id, "\n".join(lines) + "\n")
    sources.add(doc_id, source="protocols.io", url=url, doi=doi, authors=authors, license=PROTOCOLS_IO_LICENSE)
    return doc_id


def _components_text(components: Any) -> str:
    """Step text from the older component layout: the text fields of each component."""
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, inner in value.items():
                if key in ("body", "description", "text", "title") and isinstance(inner, str) and inner.strip():
                    found.append(_plain(inner))
                else:
                    walk(inner)
        elif isinstance(value, list):
            for inner in value:
                walk(inner)

    walk(components)
    return " ".join(t for t in found if t)


def _plain(value: Any) -> str:
    """Text from protocols.io fields: Markdown, HTML, or Draft.js JSON."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value)
    text = str(value).strip()
    if text.startswith("{") and '"blocks"' in text:
        try:
            return "\n".join(b.get("text", "") for b in json.loads(text).get("blocks", [])).strip()
        except ValueError:
            pass
    if "<" in text and ">" in text:
        return _html_text(text)
    return text


# --- Any URL -------------------------------------------------------------------------------------


class _TextFromHtml(HTMLParser):
    SKIP = {"script", "style", "nav", "header", "footer", "noscript", "svg", "form"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skipping = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.out.append("\n")
        if tag in ("h1", "h2", "h3") and not self.skipping:
            self.out.append("#" * int(tag[1]) + " ")
        if tag == "li" and not self.skipping:
            self.out.append("- ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skipping:
            self.skipping -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skipping:
            self.out.append(data)


def _html_text(markup: str) -> str:
    parser = _TextFromHtml()
    parser.feed(markup)
    text = html.unescape("".join(parser.out))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n\s*", "\n\n", text).strip()


def import_url(root: Path, url: str, *, folder: str = "manuals", title: str = "", license: str = "",
               transport: httpx.BaseTransport | None = None, progress: Callable[[str], None] = print) -> str:
    """One document from the web: PDFs are kept as PDFs; HTML and text become Markdown."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ImportProblem("Give an http(s) URL")
    if folder not in ("manuals", "templates", "protocols", "reference"):
        raise ImportProblem("folder must be manuals, templates, protocols or reference")
    buffer = io.BytesIO()
    with _client(120, transport) as client:
        with client.stream("GET", url) as response:
            if response.is_error:
                raise ImportProblem(f"{url} returned {response.status_code}")
            kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
            for chunk in response.iter_bytes():
                buffer.write(chunk)
                if buffer.tell() > MAX_DOWNLOAD_BYTES:
                    raise ImportProblem(f"{url} is larger than {MAX_DOWNLOAD_BYTES // 2**20} MB")
    data = buffer.getvalue()
    name = Path(parsed.path).stem or parsed.netloc
    sources = Sources(root)
    if kind == "application/pdf" or data[:5] == b"%PDF-":
        doc_id = f"{folder}/{slugify(title or name)}.pdf"
        (root / doc_id).parent.mkdir(parents=True, exist_ok=True)
        (root / doc_id).write_bytes(data)
    else:
        text = data.decode("utf-8", errors="replace")
        page_title = title
        if "html" in kind or text.lstrip()[:15].lower().startswith(("<!doctype html", "<html")):
            parser = _TextFromHtml()
            parser.feed(text)
            page_title = page_title or parser.title.strip()
            text = _html_text(text)
        page_title = page_title or name
        doc_id = f"{folder}/{slugify(page_title)}.md"
        _write(root, doc_id, "\n".join([f"# {page_title}", "", f"Source: {url}"] + ([f"License: {license}"] if license else []) + ["", text, ""]))
    sources.add(doc_id, source=parsed.netloc, url=url, license=license)
    sources.save()
    progress(f"Imported {url} as {doc_id}")
    return doc_id


# --- Command line ----------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m protocol_tool.knowledge_import", description=__doc__.split("\n\n")[0])
    parser.add_argument("--knowledge", help="knowledge folder (default: [knowledge] folder in config.toml)")
    sub = parser.add_subparsers(dest="source", required=True)
    sub.add_parser("opentrons-library", help="every protocol in the Opentrons Protocol Library")
    lib = sub.add_parser("opentrons-library-folder",
                         help="Flex protocols downloaded from library.opentrons.com into a folder (with its manifest.csv)")
    lib.add_argument("path")
    sub.add_parser("opentrons-docs", help="the Opentrons Python API documentation")
    sub.add_parser("opentrons-labware", help="every standard Opentrons labware definition (from the installed opentrons)")
    pio = sub.add_parser("protocols-io", help="public protocols.io protocols matching search terms")
    pio.add_argument("queries", nargs="+")
    pio.add_argument("--limit", type=int, default=20, help="protocols per search (default 20)")
    gh = sub.add_parser("github", help="every Opentrons protocol in a lab's public GitHub repository")
    gh.add_argument("repos", nargs="+", help="owner/name")
    one = sub.add_parser("url", help="one document from the web (kit manual, IFU, paper)")
    one.add_argument("url")
    one.add_argument("--folder", default="manuals", choices=["manuals", "templates", "protocols", "reference"])
    one.add_argument("--title", default="")
    one.add_argument("--license", default="", help="terms to record, e.g. 'CC BY 4.0'")
    args = parser.parse_args(argv)

    root = Path(args.knowledge or load_settings().knowledge.folder)
    try:
        if args.source == "opentrons-library":
            import_opentrons_library(root)
        elif args.source == "opentrons-library-folder":
            import_library_folder(root, Path(args.path))
        elif args.source == "opentrons-docs":
            import_opentrons_docs(root)
        elif args.source == "opentrons-labware":
            import_opentrons_labware(root)
        elif args.source == "protocols-io":
            import_protocols_io(root, args.queries, limit=args.limit)
        elif args.source == "github":
            for repo in args.repos:
                import_github(root, repo)
        else:
            import_url(root, args.url, folder=args.folder, title=args.title, license=args.license)
    except (ImportProblem, httpx.HTTPError) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
