"""Master node settings, loaded from a TOML file.

The path comes from $MASTER_NODE_CONFIG, falling back to ./config.toml. Every
setting has a default, so with no file at all the node starts with the default
simulated instruments. The instruments themselves are in the registry file
(instruments.toml, see registry.py). See config.example.toml for the full list.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

# Sections older versions had; the instruments now live in the registry file.
MOVED_TO_REGISTRY = {"thor", "flex", "tecan", "stations"}


@dataclass
class ServerSettings:
    host: str = "0.0.0.0"
    port: int = 8000
    # When set, every API call except /health must send it as X-API-Key.
    api_key: str = ""
    # Origins allowed to call the API from a browser (the protocol tool).
    cors_origins: list[str] = field(
        default_factory=lambda: ["http://localhost:5500", "http://127.0.0.1:5500"]
    )
    database: str = "master_node.db"
    # The instrument registry.
    instruments: str = "instruments.toml"


@dataclass
class SimSettings:
    # Multiplier on simulated device delays: 1.0 is roughly real time, 0 is instant.
    time_scale: float = 1.0


@dataclass
class Settings:
    server: ServerSettings = field(default_factory=ServerSettings)
    sim: SimSettings = field(default_factory=SimSettings)


def load_settings(path: str | Path | None = None) -> Settings:
    explicit = path or os.environ.get("MASTER_NODE_CONFIG")
    path = Path(explicit or "config.toml")
    if not path.exists():
        if explicit:
            raise FileNotFoundError(f"Config file not found: {path}")
        return Settings()

    with path.open("rb") as f:
        raw = tomllib.load(f)

    moved = MOVED_TO_REGISTRY & set(raw)
    if moved:
        raise ValueError(
            f"{path}: [{'], ['.join(sorted(moved))}] now belong in the instrument registry "
            "(instruments.toml). See instruments.example.toml and remove them from this file."
        )
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
