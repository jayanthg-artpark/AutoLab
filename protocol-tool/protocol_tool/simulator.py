"""Runs protocols through the Opentrons simulator and reads the deck from the result.

Uses `python -m opentrons.cli analyze`, the same protocol engine analysis the
Opentrons App and the Flex run before a protocol starts. It executes the
protocol's Python, so it runs in a subprocess with a timeout, a scratch working
directory and no API keys in its environment. Production deployments should
also isolate it in a container (see README).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path
from typing import Any

from .models import Deck, Labware, Liquid, Module, Pipette, Simulation, SlotContents, code_hash

MODULE_NAMES = {
    "heaterShakerModuleV1": "Heater-Shaker",
    "temperatureModuleV2": "Temperature Module",
    "thermocyclerModuleV2": "Thermocycler",
    "magneticBlockV1": "Magnetic Block",
    "absorbanceReaderV1": "Absorbance Reader",
    "flexStackerModuleV1": "Flex Stacker",
}
COMMAND_PREFIXES = {
    "heaterShaker": "Heater-Shaker",
    "temperatureModule": "Temperature Module",
    "thermocycler": "Thermocycler",
    "magneticModule": "Magnetic Module",
}
log = logging.getLogger(__name__)
SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|ANTHROPIC", re.IGNORECASE)


FLEX_ROBOT = "OT-3 Standard"  # how the analysis names the Flex
# Wells deeper than this (15 and 50 mL conical tubes) must be aspirated from relative to the
# liquid surface; the next deepest Opentrons labware (2 mL tubes, deep-well plates) is 45 mm.
DEEP_WELL_MM = 60
# Bump when the checks or what a simulation reports change, so stored designs are simulated again
# before they are sent. 2: aspirating from tubes at the liquid surface, and where liquid goes.
# 3: multi-channel pipettes only in plates and reservoirs. 4: tips reach only their length into tubes and wells.
CHECKS_VERSION = 4

# Pipette load names (what protocols write) -> the names the analysis and the robot report.
PIPETTE_INTERNAL = {
    "flex_1channel_50": "p50_single_flex", "flex_1channel_1000": "p1000_single_flex",
    "flex_8channel_50": "p50_multi_flex", "flex_8channel_1000": "p1000_multi_flex",
    "flex_96channel_200": "p200_96", "flex_96channel_1000": "p1000_96",
}


def _load_name(internal: str) -> str:
    return next((load for load, name in PIPETTE_INTERNAL.items() if name == internal), internal)


def _installed_text(installed: dict[str, str]) -> str:
    return " and ".join(f"{p} on the {m} mount" for m, p in sorted(installed.items()))


@lru_cache(maxsize=4)
def latest_api_level(python: str = "") -> str:
    """The newest Python Protocol API level the simulator's opentrons package supports, e.g. "2.31"."""
    if not python or Path(python).resolve() == Path(sys.executable).resolve():
        from opentrons.protocols.api_support.definitions import MAX_SUPPORTED_VERSION

        return str(MAX_SUPPORTED_VERSION)
    try:
        out = subprocess.run(
            [python, "-c", "from opentrons.protocols.api_support.definitions import MAX_SUPPORTED_VERSION as v; print(v)"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SimulatorUnavailable(f"Could not ask the simulator for its API level: {exc}") from exc
    level = out.stdout.strip()
    if not re.fullmatch(r"\d+\.\d+", level):
        raise SimulatorUnavailable(f"The simulator's Python reported no API level: {(out.stderr or out.stdout)[-300:]}")
    return level


@dataclass(frozen=True)
class DeckRules:
    """This lab's Flex: where Thor can put the sample plate, the fixed fixtures, the API
    level protocols must use, and the labware they may load."""

    thor_slots: tuple[str, ...] = ("B1", "B2", "B3", "C1", "C2", "C3", "D1", "D2")
    trash_slot: str = "A3"
    waste_chute_slot: str = "D3"  # "" when the deck has no waste chute
    api_level: str = ""  # required requirements["apiLevel"]; "" = not checked
    labware: tuple[str, ...] = ()  # load names allowed (fnmatch patterns); () = any Flex labware
    # Slots nothing may use: no labware, tip racks, modules or fixtures (Thor works there).
    blocked_slots: tuple[str, ...] = ("A1", "A2")
    # Pipettes on the robot as (mount, load name); () = not checked. And whether it has a gripper.
    pipettes: tuple[tuple[str, str], ...] = ()
    gripper: bool = True

    @classmethod
    def from_settings(cls, lab, python: str = "") -> DeckRules:
        level = lab.api_level
        if level == "latest":
            try:
                level = latest_api_level(python)
            except SimulatorUnavailable as exc:
                log.warning("API level not checked: %s", exc)
                level = ""
        return cls(tuple(lab.thor_slots), lab.trash_slot, lab.waste_chute_slot, level, tuple(lab.labware),
                   tuple(lab.blocked_slots), tuple(sorted(lab.pipettes.items())), lab.gripper)

    @property
    def signature(self) -> str:
        """Identifies the rules a simulation was checked against; a change means re-checking."""
        return "|".join([",".join(self.thor_slots), self.trash_slot, self.waste_chute_slot, self.api_level,
                         ",".join(self.labware), ",".join(self.blocked_slots),
                         ",".join(f"{m}={p}" for m, p in self.pipettes), str(self.gripper), f"checks{CHECKS_VERSION}"])

    def describe(self) -> str:
        """The deck rules as the AI model reads them in its instructions."""
        lines = [f"- Thor can place the sample plate only in {', '.join(self.thor_slots)}; it cannot reach the other slots."]
        if self.waste_chute_slot:
            lines.append(
                f"- Used tips go in the waste chute ({self.waste_chute_slot}, fixed): load it with "
                "`waste_chute = protocol.load_waste_chute()` and don't load the trash bin, so `drop_tip()` puts tips "
                f"there. Only if the scientist asks for the trash bin: `protocol.load_trash_bin(\"{self.trash_slot}\")` "
                f"({self.trash_slot}, fixed). Pipettes drop tips in whichever of the two is loaded first, so if a protocol "
                "loads both, load the waste chute first or set `pipette.trash_container = waste_chute`."
            )
        else:
            lines.append(f'- {self.trash_slot} holds the trash bin (fixed): load it with `protocol.load_trash_bin("{self.trash_slot}")`.')
        fixtures = " or ".join(s for s in (self.trash_slot, self.waste_chute_slot) if s)
        lines.append(f"- Never put labware or modules in {fixtures}.")
        if self.blocked_slots:
            blocked = " and ".join(self.blocked_slots)
            lines.append(
                f"- {blocked} must stay empty for the whole run: no labware, tip racks, reservoirs, modules or fixtures, "
                f"never move labware there, and no Thermocycler (it covers A1 from B1)."
            )
        free = [f"{r}{c}" for r in "ABCD" for c in (1, 2, 3)
                if f"{r}{c}" not in self.blocked_slots and f"{r}{c}" not in (self.trash_slot, self.waste_chute_slot)]
        lines.append(f"- Lab staff can set up other labware in {', '.join(free)} (and staging slots A4-D4 if used).")
        lines.append(
            "- Flex hardware only: Flex pipettes (flex_1channel_50, flex_1channel_1000, flex_8channel_50, flex_8channel_1000, "
            "flex_96channel_1000), Flex tip racks (opentrons_flex_96_tiprack_50ul / _200ul / _1000ul, or the "
            "opentrons_flex_96_filtertiprack_ versions) and Flex modules, in Flex deck coordinates (A1-D4), never "
            "numbered slots."
        )
        if self.labware:
            lines.append(f"- Labware you may load (anything else fails the check): {', '.join(self.labware)}.")
        else:
            lines.append(
                "- Labware: any standard Opentrons labware. Every definition, with its exact load name, wells, volumes and "
                "depths, is in the knowledge under reference/opentrons-labware/ (search_knowledge, e.g. \"50 mL Falcon tube "
                "rack\"). Load what the scientist says is on the deck, or the lab's stock labware when they don't say; never "
                "load a different labware as a stand-in (a 15 mL tube rack for 50 mL tubes puts the pipette in the wrong place)."
            )
        lines.append(
            f"- Aspirating from 15 and 50 mL tubes (wells deeper than {DEEP_WELL_MM} mm): always just below the liquid "
            "surface, never at the default 1 mm above the bottom or another fixed height (the check fails those). "
            "Declare the volume in each tube once with tube.load_liquid(liquid, volume_ul) and aspirate at "
            "tube.meniscus(z=-2, target=\"end\"); the robot follows the level down as it takes liquid. If the scientist "
            "didn't give the volume in a tube, ask for it: don't use measure_liquid_height there, because the level is "
            "then unknown before the run and the reach check below can't pass. See "
            "reference/opentrons-labware/liquid-height.md."
        )
        lines.append(
            f"- Reach: a tip reaches only its own length below the pipette (50 µL tips {TIP_REACH_MM[50]:g} mm, "
            f"200 µL {TIP_REACH_MM[200]:g} mm, 1000 µL {TIP_REACH_MM[1000]:g} mm). Deeper than that below the top of the "
            "tube or well, the pipette itself hits it, so the check fails any aspirate or dispense that goes deeper. "
            "For 15 and 50 mL tubes use 1000 µL tips, plan for the liquid the tips can't reach at the bottom (the "
            "level must stay within reach for the whole run; tell the scientist how much to put in), and dispense into "
            "tubes near the top: tube.top(-5)."
        )
        lines.append(
            "- Multi-channel pipettes (8- and 96-channel) only go into 96/384-well plates and reservoirs. Tubes (15/50 mL "
            "Falcon, 1.5/2 mL) take a single-channel pipette: only one 8-channel nozzle fits over a tube and the others "
            "hit the rack (the check fails it). Moving a tube's liquid into a reservoir first lets an 8-channel fill a plate."
        )
        if self.pipettes:
            lines.append("- Pipettes on the robot (load exactly these, on these mounts; nothing else): "
                         + ", ".join(f'protocol.load_instrument("{p}", "{m}")' for m, p in self.pipettes) + ".")
        if not self.gripper:
            lines.append("- There is no gripper: never call move_labware with use_gripper=True.")
        return "\n".join(lines)


def flex_problems(analysis: dict[str, Any], commands: list[dict[str, Any]], rules: DeckRules) -> list[str]:
    """What makes the protocol not a current Flex protocol: robot type, API level, non-Flex tip racks or
    unlisted labware. (The simulator itself already refuses pipettes and modules a Flex can't use.)"""
    problems = []
    if analysis.get("robotType") and analysis["robotType"] != FLEX_ROBOT:
        problems.append('This lab runs protocols on the Opentrons Flex: declare requirements = {"robotType": "Flex", ...}.')
    version = (analysis.get("config") or {}).get("apiVersion")
    if rules.api_level and isinstance(version, list) and len(version) == 2:
        used = f"{version[0]}.{version[1]}"
        if used != rules.api_level:
            problems.append(
                f"Use the lab's Opentrons API level {rules.api_level} (this protocol uses {used}): "
                f'requirements = {{"robotType": "Flex", "apiLevel": "{rules.api_level}"}}.'
            )
    if rules.pipettes:
        installed = dict(rules.pipettes)
        for p in analysis.get("pipettes", []):
            mount, name = p.get("mount", ""), p.get("pipetteName", "")
            want = installed.get(mount)
            if want is None:
                problems.append(f"Nothing is on the {mount} mount of the lab's Flex; use {_installed_text(installed)}.")
            elif PIPETTE_INTERNAL.get(want, want) != name:
                problems.append(f"The {mount} mount holds {want}, not {_load_name(name)}; use {_installed_text(installed)}.")
    if not rules.gripper and any(
        c.get("commandType") == "moveLabware" and (c.get("params") or {}).get("strategy") == "usingGripper" for c in commands
    ):
        problems.append("The lab's Flex has no gripper: don't move labware with use_gripper=True.")
    moved_into = {
        (cmd.get("params") or {}).get("newLocation", {}).get("slotName")
        for cmd in commands if cmd.get("commandType") == "moveLabware" and isinstance((cmd.get("params") or {}).get("newLocation"), dict)
    }
    for slot in sorted(moved_into & set(rules.blocked_slots)):
        problems.append(f"The protocol moves labware into {slot}, which must stay empty for the whole run.")
    seen = set()
    for cmd in commands:
        if cmd.get("commandType") != "loadLabware":
            continue
        name = (cmd.get("params") or {}).get("loadName", "")
        definition = (cmd.get("result") or {}).get("definition") or {}
        if not name or name in seen:
            continue
        seen.add(name)
        if (definition.get("parameters") or {}).get("isTiprack") and not name.startswith("opentrons_flex_"):
            problems.append(
                f"{name} is not a Flex tip rack. Use Flex tips: opentrons_flex_96_tiprack_50ul, _200ul or _1000ul "
                "(or the opentrons_flex_96_filtertiprack_ versions)."
            )
        elif rules.labware and not any(fnmatchcase(name, pattern) for pattern in rules.labware):
            problems.append(f"{name} is not on the lab's Flex labware list. Use one of: {', '.join(rules.labware)}.")
    return problems


def deck_problems(deck: Deck, plate_slot: str, rules: DeckRules) -> list[str]:
    """What is wrong with the deck for this lab: Thor's reach and the fixed fixtures."""
    problems = []
    allowed = ", ".join(rules.thor_slots)
    if not plate_slot:
        problems.append(f"No sample plate slot was given. Choose one of {allowed} (where Thor can place it).")
    elif plate_slot not in rules.thor_slots:
        problems.append(f"Thor cannot place the sample plate in {plate_slot}. Use one of {allowed}.")
    else:
        contents = deck.slots.get(plate_slot)
        if not contents or not contents.labware:
            problems.append(
                f"Nothing is loaded in {plate_slot}, where Thor will place the sample plate. "
                "Load the sample plate there."
            )
        elif contents.module or len(contents.labware) > 1:
            problems.append(
                f"Load the sample plate directly on the deck in {plate_slot}, "
                "not on a module or adapter: Thor places it there."
            )

    for slot, contents in deck.slots.items():
        if slot in rules.blocked_slots and (contents.labware or contents.module or contents.trash or contents.waste_chute):
            what = (contents.labware[-1].display_name if contents.labware else contents.module.display_name if contents.module
                    else "the trash bin" if contents.trash else "the waste chute")
            problems.append(f"{slot} must stay empty (the lab doesn't use {' or '.join(rules.blocked_slots)}); move {what} to another slot.")
        if contents.module and contents.module.model.startswith("thermocycler") and "A1" in rules.blocked_slots:
            problems.append("The Thermocycler covers A1, which must stay empty; this deck can't hold a Thermocycler.")

    fixtures = {slot: name for slot, name in ((rules.trash_slot, "the trash bin"), (rules.waste_chute_slot, "the waste chute")) if slot}
    for slot, contents in deck.slots.items():
        if slot in fixtures and (contents.labware or contents.module):
            what = contents.module.display_name if contents.module else contents.labware[-1].display_name
            problems.append(f"{slot} holds {fixtures[slot]}; move {what} to another slot.")
        if contents.trash and slot != rules.trash_slot:
            problems.append(f'The trash bin is in {rules.trash_slot}: use protocol.load_trash_bin("{rules.trash_slot}"), not {slot}.')
        if contents.waste_chute and not rules.waste_chute_slot:
            problems.append("This deck has no waste chute; drop tips in the trash bin instead.")
    return problems


class SimulatorUnavailable(RuntimeError):
    """The Opentrons package could not be run at all (as opposed to a protocol error)."""


@dataclass
class DeckTips:
    """The tip racks as the run will find them, from the master node's POST /tips/continue."""

    run_code: str  # the protocol with the master node's lines that start each pipette at its next unused tip
    used: dict[str, int]  # tips gone per rack slot, after the runs queued ahead
    load_names: dict[str, str] = field(default_factory=dict)  # what the master node's records say each slot holds
    runs_ahead: int = 0


async def simulate(
    code: str, plate_slot: str, python: str = "", timeout_s: float = 120, rules: DeckRules | None = None,
    deck_tips: DeckTips | None = None,
) -> Simulation:
    """Simulates the protocol; with `deck_tips`, starting from the tips left on the deck, as the run will."""
    started = time.monotonic()
    run_code = deck_tips.run_code if deck_tips else code
    analysis = await asyncio.to_thread(_run_analysis, run_code, python or sys.executable, timeout_s)
    sim = parse_analysis(analysis, code, plate_slot, rules, deck_tips)
    sim.duration_s = round(time.monotonic() - started, 2)
    return sim


def _run_analysis(code: str, python: str, timeout_s: float, attempts: int = 3) -> dict[str, Any]:
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    with tempfile.TemporaryDirectory(prefix="ot-sim-") as tmp:
        protocol = Path(tmp, "protocol.py")
        output = Path(tmp, "analysis.json")
        protocol.write_text(code, encoding="utf-8")
        for attempt in range(1, attempts + 1):
            try:
                proc = subprocess.run(
                    [python, "-m", "opentrons.cli", "analyze", "--json-output", str(output), str(protocol)],
                    cwd=tmp, env=env, capture_output=True, text=True, timeout=timeout_s,
                )
            except subprocess.TimeoutExpired:
                return {"result": "not-ok", "errors": [{"detail": f"Simulation timed out after {timeout_s:.0f} s"}]}
            except OSError as exc:
                raise SimulatorUnavailable(f"Could not start the Opentrons simulator: {exc}") from exc
            if output.exists():
                return json.loads(output.read_text(encoding="utf-8"))
            detail = (proc.stderr or proc.stdout).strip()[-1500:]
            if "No module named opentrons" in detail:
                raise SimulatorUnavailable("The opentrons package is not installed for the simulator's Python")
            # The simulator process itself occasionally crashes on Windows (access violation)
            # or trips over its own settings file on first use; that says nothing about the protocol.
            crashed = proc.returncode < 0 or proc.returncode > 255 or "robot_settings.json" in detail
            if not crashed or attempt == attempts:
                return {"result": "not-ok", "errors": [{"detail": detail or f"Simulator exited with code {proc.returncode}"}]}
            log.warning("Opentrons simulator crashed (exit code %s); retrying", proc.returncode)
        raise AssertionError("unreachable")


# --- Reading the analysis ---------------------------------------------------------


def parse_analysis(
    analysis: dict[str, Any], code: str, plate_slot: str, rules: DeckRules | None = None,
    deck_tips: DeckTips | None = None,
) -> Simulation:
    commands: list[dict[str, Any]] = analysis.get("commands", [])
    deck = _build_deck(analysis, commands, code)
    # A protocol that breaks the lab's deck rules fails like a simulator error, so it can't be sent.
    tips, tip_problems, tip_racks = _tip_use(analysis, commands, deck, deck_tips)
    rules = rules or DeckRules()
    errors = [_error_text(e) for e in analysis.get("errors", [])] + deck_problems(deck, plate_slot, rules)
    errors += flex_problems(analysis, commands, rules) + tip_problems + deep_well_problems(commands)
    errors += nozzle_problems(analysis, commands) + reach_problems(analysis, commands)
    if any("OutOfTips" in str(e) or "out of tips" in str(e).lower() for e in analysis.get("errors", [])):
        errors.append(_out_of_tips(tip_racks, deck_tips))

    warnings = []
    picked = {r["slot"] for r in tip_racks}
    for slot, contents in deck.slots.items():
        for lw in contents.labware:
            if lw.category == "tipRack" and slot not in picked:
                why = " (the racks on the deck before it have enough tips)" if deck_tips else ""
                warnings.append(f"The tip rack in {slot} is never used{why}. Remove it, so staff don't put a rack there for nothing.")
    plate = deck.plate_at(plate_slot) if plate_slot else None
    if plate is not None and plate.category not in ("wellPlate", ""):
        warnings.append(f"Slot {plate_slot} holds {plate.display_name}, not a well plate.")
    binned = sorted({m[1] for c in commands if c.get("commandType") == "moveToAddressableAreaForDropTip"
                     and (m := re.fullmatch(r"movableTrash([A-D][1-3])", (c.get("params") or {}).get("addressableAreaName", "")))})
    if rules.waste_chute_slot and binned:
        warnings.append(
            f"Tips are dropped in the trash bin in {', '.join(binned)}; this lab drops tips in the waste chute "
            f"({rules.waste_chute_slot}). Load the waste chute (protocol.load_waste_chute()) instead of the trash bin, "
            "unless the scientist asked for the trash bin."
        )
    if any(c.get("commandType") == "waitForResume" for c in commands):
        warnings.append("The protocol pauses for a person to resume it; runs here are unattended.")

    labware_names = {lw.id: lw.display_name for s in deck.slots.values() for lw in s.labware}
    labware_names.update({lw.id: lw.display_name for lw in deck.off_deck})
    steps = [text for c in commands if (text := _describe(c, labware_names))]
    stats = {
        "commands": len(commands),
        "tips": sum(c.get("commandType") == "pickUpTip" for c in commands),
        "aspirates": sum(c.get("commandType") == "aspirate" for c in commands),
        "dispenses": sum(c.get("commandType") == "dispense" for c in commands),
    }
    return Simulation(
        code_hash=code_hash(code),
        plate_slot=plate_slot,
        ok=analysis.get("result") == "ok" and not errors,
        errors=errors,
        warnings=warnings,
        deck=deck,
        steps=steps,
        stats=stats,
        tips=tips,
        rules=rules.signature,
        tip_state=dict(deck_tips.used) if deck_tips else None,
        tip_racks=tip_racks,
        runs_ahead=deck_tips.runs_ahead if deck_tips else 0,
    )


def deep_well_problems(commands: list[dict[str, Any]]) -> list[str]:
    """Aspirates from 15 and 50 mL tubes at a fixed height instead of just below the liquid surface.
    A plain aspirate(volume, tube) goes 1 mm above the bottom, so the tip, and in a full tube the
    pipette itself, goes deep into the liquid; a fixed height higher up draws air once the level drops."""
    wells: dict[str, tuple[str, dict[str, float]]] = {}
    for c in commands:
        definition = (c.get("result") or {}).get("definition") or {}
        if c.get("commandType") == "loadLabware" and definition.get("wells"):
            wells[c["result"]["labwareId"]] = (definition["metadata"]["displayName"],
                                               {name: w["depth"] for name, w in definition["wells"].items()})
    fixed: dict[str, float] = {}
    for c in commands:
        params = c.get("params") or {}
        if c.get("commandType") not in ("aspirate", "aspirateWhileTracking"):
            continue
        name, depths = wells.get(params.get("labwareId", ""), ("", {}))
        depth = depths.get(params.get("wellName", ""), 0.0)
        if depth > DEEP_WELL_MM and (params.get("wellLocation") or {}).get("origin") != "meniscus":
            fixed.setdefault(f"{params['wellName']} of {name}", depth)
    return [
        f"The protocol aspirates from {where} (a {depth:g} mm deep tube) at a fixed height, so the tip goes deep into "
        "the liquid or, higher up, draws air. Aspirate just below the surface: if the scientist gave the volume in the "
        "tube, declare it once with tube.load_liquid(liquid, volume_ul) and aspirate at "
        "tube.meniscus(z=-2, target=\"end\"); if not, ask the scientist how much is in the tube."
        for where, depth in fixed.items()
    ]


# How far each Flex tip reaches below the pipette (tip length less the part on the nozzle), as the analysis reports it.
TIP_REACH_MM = {50: 47.8, 200: 48.6, 1000: 85.9}
REACH_CLEARANCE_MM = 2.0  # the pipette must stay this far above the top of what it reaches into
_ORIGIN_DEPTH = {"bottom": 0.0, "center": 0.5, "top": 1.0}  # well-location origins, as a fraction of the well depth


def reach_problems(analysis: dict[str, Any], commands: list[dict[str, Any]]) -> list[str]:
    """Aspirates and dispenses deeper below the top of a tube or well than the tip reaches, so the pipette
    itself would hit it (and a partial-tip layout's other nozzles the labware). The analysis reports
    where the tip ends up, following the tracked liquid level for aspirates at the meniscus."""
    names = {p["id"]: p.get("pipetteName", "pipette") for p in analysis.get("pipettes", [])}
    definitions: dict[str, dict[str, Any]] = {}
    base: dict[str, float] = {}  # labware id -> height of the labware's bottom above the deck
    for c in commands:
        result, params = c.get("result") or {}, c.get("params") or {}
        if c.get("commandType") == "loadLabware" and (result.get("definition") or {}).get("wells"):
            definitions[result["labwareId"]] = result["definition"]
            if {"slotName", "addressableAreaName"} & set(params.get("location") or {}):
                base[result["labwareId"]] = 0.0  # straight on the deck
    for c in commands:  # on a module or adapter: from a move to a known height in one of its wells
        result, params = c.get("result") or {}, c.get("params") or {}
        lw, location = params.get("labwareId", ""), params.get("wellLocation") or {}
        if (lw in definitions and lw not in base and "z" in (result.get("position") or {})
                and location.get("origin", "top") in _ORIGIN_DEPTH):
            well = definitions[lw]["wells"].get(params.get("wellName", ""))
            if well:
                bottom = (result["position"]["z"] - (location.get("offset") or {}).get("z", 0)
                          - _ORIGIN_DEPTH[location.get("origin", "top")] * well["depth"])
                base[lw] = bottom - well["z"]

    reach: dict[str, tuple[float, float]] = {}  # pipette -> (reach, tip volume) of the tip it holds
    layouts: dict[str, dict[str, Any]] = {}
    probed: set[tuple[str, str]] = set()
    found: dict[tuple[str, str, str, bool, bool], dict[str, Any]] = {}
    for c in commands:
        kind, params, result = c.get("commandType"), c.get("params") or {}, c.get("result") or {}
        pipette, lw, well_name = params.get("pipetteId", ""), params.get("labwareId", ""), params.get("wellName", "")
        if kind == "pickUpTip" and result.get("tipLength"):
            reach[pipette] = (float(result["tipLength"]), float(result.get("tipVolume") or 0))
        elif kind == "configureNozzleLayout":
            layouts[pipette] = params.get("configurationParams") or {}
        elif kind in ("liquidProbe", "tryLiquidProbe"):
            probed.add((lw, well_name))
        if kind not in INTO_WELL or lw not in definitions or lw not in base or pipette not in reach:
            continue
        definition = definitions[lw]
        well = definition["wells"].get(well_name)
        z = (result.get("position") or {}).get("z")
        if well is None or z is None or (definition.get("parameters") or {}).get("isTiprack"):
            continue
        channels = _channels(names.get(pipette, ""))
        _, active = _active_nozzles(channels, layouts.get(pipette))
        bare = len(active) < channels  # nozzles without tips hang over the labware at the pipette's height
        top = base[lw] + (definition["dimensions"]["zDimension"] if bare else well["z"] + well["depth"])
        tip_reach, tip_volume = reach[pipette]
        if z + tip_reach >= top + REACH_CLEARANCE_MM - 1e-6:
            continue
        unknown = (lw, well_name) in probed and (params.get("wellLocation") or {}).get("origin") == "meniscus"
        key = (names.get(pipette, "pipette"), definition["metadata"]["displayName"], kind, bare, unknown)
        entry = found.setdefault(key, {"wells": [], "depth": 0.0, "reach": tip_reach, "tip": tip_volume})
        if well_name not in entry["wells"]:
            entry["wells"].append(well_name)
        entry["depth"] = max(entry["depth"], top - z)

    problems = []
    for (pipette, name, kind, bare, unknown), e in found.items():
        wells = f"{', '.join(e['wells'][:4])}{'...' if len(e['wells']) > 4 else ''}"
        action = "aspirates" if kind.startswith("aspirate") else "dispenses" if kind.startswith("dispense") else "goes"
        text = (f"The {_load_name(pipette)} {action} {e['depth']:.0f} mm below the top of {wells} of {name}, but its "
                f"{e['tip']:g} µL tips reach only {e['reach']:.0f} mm, so the pipette "
                f"{'and its nozzles without tips' if bare else 'itself'} would hit the {'labware' if bare else 'tube or well'}. ")
        if unknown:
            text += ("The level after measure_liquid_height isn't known before the run, so the check assumes the tube may "
                     "be nearly empty: ask the scientist how much is in the tube and declare it with "
                     "tube.load_liquid(liquid, volume_ul) instead.")
        elif action == "dispenses":
            text += "Dispense into it near the top instead: tube.top(-5)."
        else:
            text += ("Use longer tips (1000 µL tips reach 86 mm), or have more liquid in the tube so the level stays within "
                     "reach for the whole run (tell the scientist the volume to put in), or move the liquid to a reservoir.")
        problems.append(text)
    return problems


NOZZLE_PITCH_MM = 9.0
HEAD_ROWS = "ABCDEFGH"
# Commands that put the pipette's nozzles into a well (tip pickups and drops are in tip racks, which fit any head).
INTO_WELL = ("aspirate", "aspirateWhileTracking", "dispense", "dispenseWhileTracking", "blowout", "touchTip",
             "moveToWell", "liquidProbe", "tryLiquidProbe", "mix")


def _nozzle(name: str) -> tuple[int, int]:
    return HEAD_ROWS.index(name[0]), int(name[1:]) - 1


def _active_nozzles(channels: int, config: dict[str, Any] | None) -> tuple[tuple[int, int], list[tuple[int, int]]]:
    """(primary nozzle, active nozzles) as (row, column) on the head, from a configureNozzleLayout's params."""
    rows, cols = {1: (1, 1), 8: (8, 1)}.get(channels, (8, 12))
    grid = [(r, c) for r in range(rows) for c in range(cols)]
    config = config or {}
    primary = _nozzle(config.get("primaryNozzle") or "A1")
    style = (config.get("style") or "ALL").upper()
    if style == "SINGLE":
        return primary, [primary]
    if config.get("backLeftNozzle") and config.get("frontRightNozzle"):
        (r0, c0), (r1, c1) = _nozzle(config["backLeftNozzle"]), _nozzle(config["frontRightNozzle"])
        return primary, [(r, c) for r, c in grid if r0 <= r <= r1 and c0 <= c <= c1]
    if style == "COLUMN":
        return primary, [(r, c) for r, c in grid if c == primary[1]]
    if style == "ROW":
        return primary, [(r, c) for r, c in grid if r == primary[0]]
    return primary, grid


def _well_at(definition: dict[str, Any], x: float, y: float) -> str | None:
    """The well whose opening contains the point (labware coordinates), if any."""
    for name, w in definition["wells"].items():
        dx, dy = x - w["x"], y - w["y"]
        if w.get("shape") == "circular":
            if dx * dx + dy * dy <= (w["diameter"] / 2) ** 2 + 1e-6:
                return name
        elif abs(dx) <= w.get("xDimension", 0) / 2 + 1e-6 and abs(dy) <= w.get("yDimension", 0) / 2 + 1e-6:
            return name
    return None


def nozzle_problems(analysis: dict[str, Any], commands: list[dict[str, Any]]) -> list[str]:
    """A multi-channel head taken into labware its nozzles don't fit: an 8-channel in a tube rack puts
    one nozzle over the tube and the others onto the rack (the simulator only checks this for partial
    tip layouts). Every active nozzle must be over its own well, or share one trough of a reservoir
    (labware the Flex centres a multi-channel head on)."""
    pipettes = {p["id"]: p.get("pipetteName", "") for p in analysis.get("pipettes", [])}
    definitions: dict[str, dict[str, Any]] = {}
    layouts: dict[str, dict[str, Any]] = {}
    found: dict[tuple[str, str], list[str]] = {}
    for c in commands:
        kind, params = c.get("commandType"), c.get("params") or {}
        if kind == "loadLabware" and ((c.get("result") or {}).get("definition") or {}).get("wells"):
            definitions[c["result"]["labwareId"]] = c["result"]["definition"]
        elif kind == "configureNozzleLayout":
            layouts[params.get("pipetteId", "")] = params.get("configurationParams") or {}
        if kind not in INTO_WELL or params.get("labwareId") not in definitions:
            continue
        definition = definitions[params["labwareId"]]
        well = definition["wells"].get(params.get("wellName", ""))
        channels = _channels(pipettes.get(params.get("pipetteId", ""), ""))
        if channels == 1 or well is None or (definition.get("parameters") or {}).get("isTiprack"):
            continue
        primary, active = _active_nozzles(channels, layouts.get(params.get("pipetteId", "")))
        if len(active) < 2:
            continue
        centred = "centerMultichannelOnWells" in ((definition.get("parameters") or {}).get("quirks") or [])
        if centred:  # the Flex puts the middle of the active nozzles over the well
            rows, cols = [r for r, _ in active], [c for _, c in active]
            ref = ((min(rows) + max(rows)) / 2, (min(cols) + max(cols)) / 2)
        else:
            ref = primary
        offset = (params.get("wellLocation") or {}).get("offset") or {}
        x0, y0 = well["x"] + offset.get("x", 0), well["y"] + offset.get("y", 0)
        # Row A of the head is at the back (higher y), column 1 on the left.
        under = [_well_at(definition, x0 + NOZZLE_PITCH_MM * (c - ref[1]), y0 - NOZZLE_PITCH_MM * (r - ref[0]))
                 for r, c in active]
        if None in under or (not centred and len(set(under)) < len(under)):
            key = (pipettes.get(params["pipetteId"], "pipette"), definition["metadata"]["displayName"])
            wells = found.setdefault(key, [])
            if params["wellName"] not in wells:
                wells.append(params["wellName"])
    return [
        f"The {_load_name(pipette)} goes into {', '.join(wells[:4])}{'...' if len(wells) > 4 else ''} of {name} with "
        "several nozzles, but only one fits there: the others would hit the labware. Multi-channel pipettes only go "
        "into 96/384-well plates and reservoirs. Take liquid from tubes with a single-channel pipette (flex_1channel_...); "
        "if the robot has only a multi-channel, ask the scientist to move the liquid to a reservoir first."
        for (pipette, name), wells in found.items()
    ]


def _out_of_tips(tip_racks: list[dict[str, Any]], deck_tips: DeckTips | None) -> str:
    """Why a pipette ran out of tips, and the two ways to fix it."""
    left = ", ".join(f"{r['slot']} {r['load_name']} had {RACK_SIZE - r['before']} of {RACK_SIZE} left" for r in tip_racks)
    where = f"the racks on the deck ({left})" if deck_tips and left else f"its tip racks ({left})" if left else "its tip racks"
    return (f"A pipette ran out of tips: this protocol needs more than {where}. Either load another rack of the same "
            "type in a free slot and list it after the existing one in tip_racks=[...] (lab staff put a full rack "
            "there), or tell the scientist to replace the rack with a full one and click New rack on the Runs tab.")


def _tip_use(
    analysis: dict[str, Any], commands: list[dict[str, Any]], deck: Deck, deck_tips: DeckTips | None = None,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """How each pipette uses tips (in the order it uses its racks), problems (a rack shared by two
    pipettes, a rack type the lab's records don't match), and per rack: tips used before and after."""
    where = {lw.id: (slot, lw.load_name) for slot, contents in deck.slots.items() for lw in contents.labware}
    names = {p["id"]: p.get("pipetteName", "pipette") for p in analysis.get("pipettes", [])}
    used = deck_tips.used if deck_tips else {}
    uses: dict[str, dict[str, Any]] = {}
    owner: dict[str, str] = {}
    racks: dict[str, dict[str, Any]] = {}
    problems = set()
    for cmd in commands:
        if cmd.get("commandType") != "pickUpTip":
            continue
        params = cmd.get("params", {})
        pipette_id, labware_id = params.get("pipetteId", ""), params.get("labwareId", "")
        if labware_id not in where:
            continue
        slot, load_name = where[labware_id]
        name = names.get(pipette_id, "pipette")
        use = uses.setdefault(pipette_id, {"pipette": name, "channels": _channels(name), "racks": [], "pickups": 0})
        if not any(r["slot"] == slot for r in use["racks"]):
            use["racks"].append({"slot": slot, "load_name": load_name})
        use["pickups"] += 1
        if owner.setdefault(slot, pipette_id) != pipette_id:
            problems.add(
                f"The tip rack in {slot} is used by two pipettes. Give each pipette its own tip rack "
                "so the lab can keep track of the tips."
            )
        rack = racks.setdefault(slot, {"slot": slot, "load_name": load_name, "pipette": name,
                                       "before": used.get(slot, 0), "after": used.get(slot, 0)})
        well = params.get("wellName", "")
        if well in TIP_ORDER:
            # An 8-channel takes the whole column from its row-A well; a 96-channel the whole rack.
            taken = {1: 1, 8: 8}.get(use["channels"], RACK_SIZE)
            rack["after"] = max(rack["after"], TIP_ORDER.index(well) + taken)
            if TIP_ORDER.index(well) < rack["before"]:
                # The run starts each pipette at its first rack's next unused tip, but the Flex goes on
                # into its later racks from A1: tips that earlier protocols or runs already took.
                rack["reused"] = True
    for slot, rack in racks.items():
        if rack.pop("reused", False):
            gone = "empty" if rack["before"] >= RACK_SIZE else f"partly used ({RACK_SIZE - rack['before']} of {RACK_SIZE} left)"
            problems.add(
                f"Not enough tips on the deck: the {rack['pipette']} runs on into {slot}, but by then that rack is {gone} "
                f"(the protocols before this one in the workflow, or runs queued ahead, take those tips), and the Flex "
                f"would start it from A1 again. Load another {rack['load_name']} rack in a free slot and add it to "
                "tip_racks=[...] (lab staff put a full rack there), or use fewer tips (for example reuse a tip for "
                "the same reagent)."
            )
    if deck_tips:
        for slot, rack in racks.items():
            recorded = deck_tips.load_names.get(slot)
            if recorded and recorded != rack["load_name"]:
                problems.add(
                    f"The lab's records say {slot} holds {recorded}, but this protocol uses {rack['load_name']} there. "
                    f"Use the {recorded} in {slot}, or put this tip type in a free slot."
                )
    return list(uses.values()), sorted(problems), list(racks.values())


RACK_SIZE = 96
# The order pipettes take tips in: A1, B1 ... H1, A2 ... (as the master node counts them).
TIP_ORDER = [f"{row}{col}" for col in range(1, 13) for row in "ABCDEFGH"]


def _channels(pipette_name: str) -> int:
    if "_96" in pipette_name:
        return 96
    return 8 if "multi" in pipette_name else 1


def _error_text(error: dict[str, Any]) -> str:
    detail = str(error.get("detail", "Unknown error"))
    # "ProtocolCommandFailedError [line 9]: Error 4000 GENERAL_ERROR (...): PythonException: X" -> "[line 9] X"
    line = re.search(r"\[line \d+\]", detail)
    detail = re.sub(r"^.*?PythonException: ", "", detail)
    return f"{line[0]} {detail}" if line and line[0] not in detail else detail


def _build_deck(analysis: dict[str, Any], commands: list[dict[str, Any]], code: str) -> Deck:
    slots: dict[str, SlotContents] = {}
    module_slot: dict[str, str] = {}
    labware_slot: dict[str, str | None] = {}
    labware: dict[str, Labware] = {}
    off_deck: list[Labware] = []

    def slot(name: str) -> SlotContents:
        return slots.setdefault(name, SlotContents())

    # Initial positions come from the load commands (labware may move later in the run).
    for cmd in commands:
        kind, params, result = cmd.get("commandType"), cmd.get("params", {}), cmd.get("result") or {}
        if kind == "loadModule" and result.get("moduleId"):
            location = (params.get("location") or {}).get("slotName", "")
            model = result.get("model") or params.get("model", "")
            module_slot[result["moduleId"]] = location
            slot(location).module = Module(
                id=result["moduleId"], model=model, display_name=MODULE_NAMES.get(model, model)
            )
        elif kind == "loadLabware" and result.get("labwareId"):
            definition = result.get("definition") or {}
            ordering = definition.get("ordering") or []
            item = Labware(
                id=result["labwareId"],
                load_name=params.get("loadName", ""),
                display_name=params.get("displayName")
                or (definition.get("metadata") or {}).get("displayName")
                or params.get("loadName", ""),
                category=(definition.get("metadata") or {}).get("displayCategory", ""),
                cols=len(ordering),
                rows=max((len(col) for col in ordering), default=0),
            )
            labware[item.id] = item
            where = _resolve(params.get("location"), module_slot, labware_slot)
            labware_slot[item.id] = where
            if where:
                slot(where).labware.append(item)
            else:
                off_deck.append(item)
        elif kind == "loadLiquid":
            item = labware.get(params.get("labwareId", ""))
            if item:
                for well in params.get("volumeByWell") or {}:
                    item.liquids.setdefault(well, []).append(params.get("liquidId", ""))
        elif kind in ("moveToAddressableArea", "moveToAddressableAreaForDropTip"):
            area = params.get("addressableAreaName", "")
            if match := re.fullmatch(r"movableTrash([A-D][1-3])", area):
                slot(match[1]).trash = True
            elif "WasteChute" in area:  # 1ChannelWasteChute, 8ChannelWasteChute, ...
                slot("D3").waste_chute = True

    # Trash that the protocol loads but never uses still occupies its slot.
    for match in re.finditer(r"load_trash_bin\(\s*(?:location\s*=\s*)?[\"']([A-D][1-3])[\"']", code):
        slot(match[1]).trash = True
    if re.search(r"load_waste_chute\(", code):
        slot("D3").waste_chute = True

    _track_liquids(analysis, commands, labware)

    pipettes = [
        Pipette(mount=p.get("mount", ""), name=p.get("pipetteName", ""), display_name=_pipette_name(p.get("pipetteName", "")))
        for p in analysis.get("pipettes", [])
    ]
    liquids = [
        Liquid(id=l["id"], name=l.get("displayName", ""), description=l.get("description", ""),
               color=l.get("displayColor") or "#888888")
        for l in analysis.get("liquids", [])
    ]
    return Deck(
        slots=dict(sorted(slots.items())), off_deck=off_deck,
        pipettes=sorted(pipettes, key=lambda p: p.mount), liquids=liquids,
    )


ASPIRATES = ("aspirate", "aspirateInPlace", "aspirateWhileTracking")
DISPENSES = ("dispense", "dispenseInPlace", "dispenseWhileTracking")


def _track_liquids(analysis: dict[str, Any], commands: list[dict[str, Any]], labware: dict[str, Labware]) -> None:
    """Which wells the pipettes take liquid from and put it into, and which liquids end up where:
    a tip carries what it aspirated until it is dropped."""
    channels = {p["id"]: _channels(p.get("pipetteName", "")) for p in analysis.get("pipettes", [])}
    at: dict[str, tuple[str, str]] = {}  # pipette -> (labware, well) it is in
    carrying: dict[str, list[str]] = {}
    for cmd in commands:
        kind, params = cmd.get("commandType"), cmd.get("params") or {}
        pipette = params.get("pipetteId", "")
        if kind in ("pickUpTip", "dropTip", "dropTipInPlace"):
            carrying[pipette] = []
        if params.get("labwareId") in labware and params.get("wellName"):
            at[pipette] = (params["labwareId"], params["wellName"])
        elif kind in ("moveToAddressableArea", "moveToAddressableAreaForDropTip", "moveToCoordinates"):
            at.pop(pipette, None)
        if (kind not in ASPIRATES and kind not in DISPENSES) or pipette not in at:
            continue
        lw = labware[at[pipette][0]]
        reached = _wells_reached(lw, at[pipette][1], channels.get(pipette, 1))
        # µL per channel; channels sharing one well (a reservoir trough) all take from it.
        volume = float(params.get("volume") or 0) * (channels.get(pipette, 1) if len(reached) == 1 else 1)
        for well in reached:
            if kind in ASPIRATES:
                lw.aspirated[well] = lw.aspirated.get(well, 0) + volume
                held = carrying.setdefault(pipette, [])
                held += [i for i in lw.liquids.get(well, []) + lw.received.get(well, []) if i not in held]
            else:
                lw.dispensed[well] = lw.dispensed.get(well, 0) + volume
                got = lw.received.setdefault(well, [])
                got += [i for i in carrying.get(pipette, []) if i not in got]


def _wells_reached(lw: Labware, well: str, channels: int) -> list[str]:
    """The wells a pipette's channels are in when its first channel is at `well`: an 8-channel
    covers a column of a 96-well plate (every other row of a 384), a 96-channel the whole plate;
    in a reservoir or tube rack with fewer rows, the channels share the one well."""
    match = re.fullmatch(r"([A-P])(\d+)", well)
    if channels == 1 or not match or lw.rows < 8:
        return [well]
    if channels == 96 and lw.rows == 8 and lw.cols == 12:
        return [f"{r}{c}" for r in "ABCDEFGH" for c in range(1, 13)]
    step, start = lw.rows // 8, "ABCDEFGHIJKLMNOP".index(match[1])
    return [f"{'ABCDEFGHIJKLMNOP'[r]}{match[2]}" for r in range(start, min(lw.rows, start + 8 * step), step)]


def _resolve(location: Any, module_slot: dict[str, str], labware_slot: dict[str, str | None]) -> str | None:
    if not isinstance(location, dict):
        return None  # "offDeck"
    if "slotName" in location:
        return location["slotName"]
    if "moduleId" in location:
        return module_slot.get(location["moduleId"])
    if "labwareId" in location:
        return labware_slot.get(location["labwareId"])
    if "addressableAreaName" in location:
        return location["addressableAreaName"]
    return None


def _pipette_name(name: str) -> str:
    match = re.fullmatch(r"p(\d+)_(single|multi|96)(?:_flex)?", name)
    if not match:
        return name
    channels = {"single": "1-Channel", "multi": "8-Channel", "96": "96-Channel"}[match[2]]
    return f"Flex {channels} {match[1]} µL"


def _describe(cmd: dict[str, Any], names: dict[str, str]) -> str | None:
    kind, p = cmd.get("commandType", ""), cmd.get("params", {})
    lw = names.get(p.get("labwareId", ""), "labware")
    where = f"{p.get('wellName', '')} of {lw}"
    volume = p.get("volume")
    match kind:
        case "home" | "loadPipette" | "loadLabware" | "loadModule" | "loadLiquid" | "moveToAddressableAreaForDropTip" | "configureForVolume":
            return None
        case "pickUpTip":
            return f"Pick up tip from {where}"
        case "aspirate":
            return f"Aspirate {volume:g} µL from {where}"
        case "dispense":
            return f"Dispense {volume:g} µL into {where}"
        case "blowout":
            return f"Blow out in {where}"
        case "blowOutInPlace":
            return "Blow out"
        case "touchTip":
            return f"Touch tip in {where}"
        case "dropTip":
            return f"Drop tip in {lw}"
        case "dropTipInPlace":
            return "Drop tip in trash"
        case "comment":
            return f"Comment: {p.get('message', '')}"
        case "waitForDuration":
            return f"Wait {p.get('seconds', 0):g} s"
        case "waitForResume":
            return f"Pause: {p.get('message') or 'wait for a person to resume'}"
        case "moveLabware":
            return f"Move {lw} with the {p.get('strategy', 'gripper').replace('usingGripper', 'gripper')}"
    module, _, action = kind.partition("/")
    if module in COMMAND_PREFIXES:
        detail = ", ".join(f"{k}={v}" for k, v in p.items() if k != "moduleId")
        action = re.sub(r"(?<!^)(?=[A-Z])", " ", action).lower()
        return f"{COMMAND_PREFIXES[module]}: {action}" + (f" ({detail})" if detail else "")
    return kind
