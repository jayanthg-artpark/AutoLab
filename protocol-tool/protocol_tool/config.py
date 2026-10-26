"""Protocol tool settings, loaded from a TOML file.

The path comes from $PROTOCOL_TOOL_CONFIG, falling back to ./config.toml.
Every setting has a default; see config.example.toml. API keys are read from
the environment or a .env file next to this project, never from this file.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

DEFAULT_INVENTORY = """\
- Pipettes: Flex 1-Channel 1000 uL (flex_1channel_1000) on the left mount, Flex 8-Channel 50 uL (flex_8channel_50) on the right mount
- Gripper: none (labware cannot be moved by the robot; never use move_labware with use_gripper=True)
- Modules: none
- Tip racks: opentrons_flex_96_tiprack_50ul, opentrons_flex_96_tiprack_200ul, opentrons_flex_96_tiprack_1000ul (and the filtertiprack versions)
- Labware in stock: corning_96_wellplate_360ul_flat, nest_96_wellplate_200ul_flat, nest_12_reservoir_15ml, nest_1_reservoir_195ml, opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap, opentrons_15_tuberack_falcon_15ml_conical
- Fixtures: a trash bin in slot A3 and a waste chute in slot D3"""


DEFAULT_LABWARE = (
    "opentrons_flex_*",  # Flex tip racks, filter tip racks and adapters
    "corning_96_wellplate_360ul_flat",
    "nest_96_wellplate_200ul_flat",
    "nest_12_reservoir_15ml",
    "nest_1_reservoir_195ml",
    "opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap",
    "opentrons_15_tuberack_falcon_15ml_conical",
)


@dataclass
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 5500
    database: str = "protocol_tool.db"
    # When set, the browser asks for this password before showing anything (any user name works).
    # Set it whenever the site is reachable by others, e.g. through a Cloudflare tunnel.
    access_password: str = ""


@dataclass
class AssistantSettings:
    # Which AI model designs the protocols: "claude" (paid), "gemini" (free tier),
    # "ollama" (free, runs on this PC), or "openai" (any OpenAI-compatible server).
    provider: str = "claude"
    model: str = ""  # empty = the provider's default (see providers.py)
    url: str = ""  # OpenAI-compatible base URL; empty = the provider's default
    api_key_env: str = ""  # environment/.env variable holding the key; empty = the provider's default
    # Models to try when the main one is overloaded, out of quota or retired; None = the provider's default.
    fallback_models: list[str] | None = None
    effort: str = "high"  # Claude only: low | medium | high | xhigh | max
    # Most tool calls (look up, write protocol, simulate, fix, set workflow) the model may make in one reply.
    max_tool_rounds: int = 16
    max_upload_mb: int = 20


@dataclass
class MasterSettings:
    url: str = "http://localhost:8000"
    api_key: str = ""
    timeout_s: float = 15.0


@dataclass
class SimulatorSettings:
    # Python interpreter that has the opentrons package; empty means this one.
    python: str = ""
    timeout_s: float = 120.0


@dataclass
class LabSettings:
    # Flex deck slots where Thor can place the sample plate (keep in step with the master
    # node's [stations] thor_slots). Thor can't reach A1/A2; A3 and D3 hold fixtures.
    thor_slots: list[str] = field(default_factory=lambda: ["B1", "B2", "B3", "C1", "C2", "C3", "D1", "D2"])
    trash_slot: str = "A3"
    waste_chute_slot: str = "D3"  # "" if the deck has no waste chute
    # Slots protocols may not use at all: no labware, tip racks, modules or fixtures.
    blocked_slots: list[str] = field(default_factory=lambda: ["A1", "A2"])
    # What is physically on the Flex (its /instruments): load names by mount. A protocol that
    # loads anything else fails the check, since the robot would refuse to start it.
    pipettes: dict[str, str] = field(default_factory=lambda: {"left": "flex_1channel_1000", "right": "flex_8channel_50"})
    gripper: bool = False
    # Python Protocol API level every protocol must use. "latest" = the newest the installed
    # opentrons package supports; the Flex's robot software must be at least as new.
    api_level: str = "latest"
    # Labware protocols may load (load names; * wildcards allowed). Everything else fails the
    # simulation check. OT-2 tip racks always fail. Empty list = any Flex labware.
    labware: list[str] = field(default_factory=lambda: list(DEFAULT_LABWARE))
    # What the assistant may use on the Flex. Edit this to match the lab.
    inventory: str = DEFAULT_INVENTORY


@dataclass
class KnowledgeSettings:
    # Folder with manuals/ (kit manuals, IFUs) and templates/ (assay templates) the assistant can search.
    folder: str = "knowledge"


@dataclass
class ResultsSettings:
    # Let the AI model write a plain-language summary of a run's results, or suggest what to
    # check when a run fails ("Explain" on the Runs tab). Off: no run data goes to the model.
    explain: bool = True


@dataclass
class Settings:
    server: ServerSettings = field(default_factory=ServerSettings)
    assistant: AssistantSettings = field(default_factory=AssistantSettings)
    master: MasterSettings = field(default_factory=MasterSettings)
    simulator: SimulatorSettings = field(default_factory=SimulatorSettings)
    lab: LabSettings = field(default_factory=LabSettings)
    knowledge: KnowledgeSettings = field(default_factory=KnowledgeSettings)
    results: ResultsSettings = field(default_factory=ResultsSettings)


def load_settings(path: str | Path | None = None) -> Settings:
    explicit = path or os.environ.get("PROTOCOL_TOOL_CONFIG")
    path = Path(explicit or "config.toml")
    if not path.exists():
        if explicit:
            raise FileNotFoundError(f"Config file not found: {path}")
        return Settings()

    with path.open("rb") as f:
        raw = tomllib.load(f)

    defaults = Settings()
    known = {f.name for f in fields(Settings)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"{path}: unknown sections {sorted(unknown)}")
    sections = {}
    for name in known:
        try:
            sections[name] = replace(getattr(defaults, name), **raw.get(name, {}))
        except TypeError as exc:
            raise ValueError(f"{path}: [{name}] {exc}") from exc
    return Settings(**sections)
