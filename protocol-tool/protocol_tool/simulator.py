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
from dataclasses import dataclass
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
                         ",".join(f"{m}={p}" for m, p in self.pipettes), str(self.gripper)])

    def describe(self) -> str:
        """The deck rules as the AI model reads them in its instructions."""
        lines = [f"- Thor can place the sample plate only in {', '.join(self.thor_slots)}; it cannot reach the other slots."]
        lines.append(f'- {self.trash_slot} holds the trash bin (fixed): load it with `protocol.load_trash_bin("{self.trash_slot}")`.')
        if self.waste_chute_slot:
            lines.append(f"- {self.waste_chute_slot} holds the waste chute (fixed): use `protocol.load_waste_chute()` if you need it.")
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
            "opentrons_flex_96_filtertiprack_ versions) and Flex-compatible modules. Never OT-2 labware, tip racks, "
            "pipettes or modules, and use Flex deck coordinates (A1-D4), not OT-2 slot numbers."
        )
        if self.labware:
            lines.append(f"- Labware you may load (anything else fails the check): {', '.join(self.labware)}.")
        if self.pipettes:
            lines.append("- Pipettes on the robot (load exactly these, on these mounts; nothing else): "
                         + ", ".join(f'protocol.load_instrument("{p}", "{m}")' for m, p in self.pipettes) + ".")
        if not self.gripper:
            lines.append("- There is no gripper: never call move_labware with use_gripper=True.")
        return "\n".join(lines)


def flex_problems(analysis: dict[str, Any], commands: list[dict[str, Any]], rules: DeckRules) -> list[str]:
    """What makes the protocol not a current Flex protocol: robot type, API level, OT-2 or unlisted labware.
    (The simulator itself already refuses OT-2 pipettes and modules on a Flex.)"""
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
                f"{name} is an OT-2 tip rack. Use Flex tips: opentrons_flex_96_tiprack_50ul, _200ul or _1000ul "
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


async def simulate(
    code: str, plate_slot: str, python: str = "", timeout_s: float = 120, rules: DeckRules | None = None
) -> Simulation:
    started = time.monotonic()
    analysis = await asyncio.to_thread(_run_analysis, code, python or sys.executable, timeout_s)
    sim = parse_analysis(analysis, code, plate_slot, rules)
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
    analysis: dict[str, Any], code: str, plate_slot: str, rules: DeckRules | None = None
) -> Simulation:
    commands: list[dict[str, Any]] = analysis.get("commands", [])
    deck = _build_deck(analysis, commands, code)
    # A protocol that breaks the lab's deck rules fails like a simulator error, so it can't be sent.
    tips, tip_problems = _tip_use(analysis, commands, deck)
    rules = rules or DeckRules()
    errors = [_error_text(e) for e in analysis.get("errors", [])] + deck_problems(deck, plate_slot, rules)
    errors += flex_problems(analysis, commands, rules) + tip_problems

    warnings = []
    plate = deck.plate_at(plate_slot) if plate_slot else None
    if plate is not None and plate.category not in ("wellPlate", ""):
        warnings.append(f"Slot {plate_slot} holds {plate.display_name}, not a well plate.")
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
    )


def _tip_use(analysis: dict[str, Any], commands: list[dict[str, Any]], deck: Deck) -> tuple[list[dict[str, Any]], list[str]]:
    """How each pipette uses tips, in the order it uses its racks, plus any rack shared by two pipettes."""
    where = {lw.id: (slot, lw.load_name) for slot, contents in deck.slots.items() for lw in contents.labware}
    names = {p["id"]: p.get("pipetteName", "pipette") for p in analysis.get("pipettes", [])}
    uses: dict[str, dict[str, Any]] = {}
    owner: dict[str, str] = {}
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
    return list(uses.values()), sorted(problems)


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
            elif area.startswith("wasteChute"):
                slot("D3").waste_chute = True

    # Trash that the protocol loads but never uses still occupies its slot.
    for match in re.finditer(r"load_trash_bin\(\s*(?:location\s*=\s*)?[\"']([A-D][1-3])[\"']", code):
        slot(match[1]).trash = True
    if re.search(r"load_waste_chute\(", code):
        slot("D3").waste_chute = True

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
