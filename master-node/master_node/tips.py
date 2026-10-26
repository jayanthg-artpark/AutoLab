"""Tip racks stay on the Flex between runs; this tracks how far each one is used.

Pipettes take tips in Opentrons' order: A1, B1 ... H1, A2 ... A single-channel
pipette takes the next tip, an 8-channel the next whole column (what is left of
a partly used column is skipped and counted as used), a 96-channel a full rack.
So a rack's state is one number: how many tips, in that order, are gone.

Before a run the master node checks there are enough tips, then appends a small
wrapper to the protocol that starts each pipette at its first unused tip, so the
protocol itself needs no changes. Tips count as used as soon as the run starts,
even if it later fails: overcounting wastes a few tips, undercounting would reuse one.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import asdict, dataclass

from .models import TipUse, utcnow

RACK_SIZE = 96
ORDER = [f"{row}{col}" for col in range(1, 13) for row in "ABCDEFGH"]


class TipProblem(Exception):
    """The racks on the deck can't serve this protocol as they are."""


@dataclass
class Rack:
    slot: str
    load_name: str
    used: int
    updated_at: str

    def view(self) -> dict:
        data = asdict(self)
        data["remaining"] = RACK_SIZE - self.used
        data["next_tip"] = ORDER[self.used] if self.used < RACK_SIZE else None
        return data


def _start(used: int, channels: int) -> int:
    """Index of the first tip this pipette can take from a rack with `used` tips gone."""
    if channels == 1:
        return used
    if channels == 8:
        return -(-used // 8) * 8
    return 0 if used == 0 else RACK_SIZE


class TipTracker:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS tip_racks (
                       slot TEXT PRIMARY KEY, load_name TEXT NOT NULL,
                       used INTEGER NOT NULL, updated_at TEXT NOT NULL)"""
            )

    def racks(self) -> list[Rack]:
        with self._lock:
            rows = self._conn.execute("SELECT slot, load_name, used, updated_at FROM tip_racks ORDER BY slot").fetchall()
        return [Rack(*row) for row in rows]

    def mark_fresh(self, slot: str, load_name: str | None = None) -> Rack | None:
        """Staff put a full rack in `slot` (keeping the recorded tip type unless given)."""
        current = {r.slot: r for r in self.racks()}.get(slot)
        name = load_name or (current.load_name if current else None)
        if not name:
            return None
        self._save({slot: (name, 0)})
        return Rack(slot, name, 0, utcnow().isoformat())

    def remove(self, slot: str) -> bool:
        with self._lock, self._conn:
            return self._conn.execute("DELETE FROM tip_racks WHERE slot = ?", (slot,)).rowcount > 0

    def plan(
        self, uses: list[TipUse], used_now: dict[str, int] | None = None
    ) -> tuple[dict[str, int], dict[str, tuple[str, int]]]:
        """For a protocol's tip use: (tips used per rack now, racks after the run).

        `used_now` overrides the recorded counts, e.g. with what an earlier protocol
        in the same run will leave. Raises TipProblem when the racks on the deck
        can't serve the protocol.
        """
        known = {r.slot: r for r in self.racks()}
        used_now = used_now or {}
        before: dict[str, int] = {}
        after: dict[str, tuple[str, int]] = {}
        for use in uses:
            need = use.pickups
            started = False
            for rack in use.racks:
                record = known.get(rack.slot)
                if record and record.load_name != rack.load_name:
                    raise TipProblem(
                        f"The records say {rack.slot} holds {record.load_name}, but this protocol uses "
                        f"{rack.load_name} there. Swap the rack and click New rack for {rack.slot} on the "
                        "Runs tab, or remove the old record."
                    )
                used = used_now.get(rack.slot, record.used if record else 0)
                before[rack.slot] = used
                start = _start(used, use.channels)
                per = 1 if use.channels == 1 else 8 if use.channels == 8 else RACK_SIZE
                available = (RACK_SIZE - start) // per
                if started and need and used:
                    # The Flex continues into later racks from A1, so they must be full.
                    raise TipProblem(
                        f"The {use.pipette} needs tips from {rack.slot} too, but that rack is partly used. "
                        f"Put a fresh rack in {rack.slot} and click New rack on the Runs tab."
                    )
                if available <= 0 or not need:
                    after.setdefault(rack.slot, (rack.load_name, used))
                    continue
                started = True
                take = min(need, available)
                need -= take
                after[rack.slot] = (rack.load_name, start + take * per)
            if need:
                left = ", ".join(f"{r.slot} {RACK_SIZE - before.get(r.slot, 0)} left" for r in use.racks)
                unit = "tips" if use.channels == 1 else f"{use.channels}-tip pickups"
                raise TipProblem(
                    f"Not enough tips for the {use.pipette}: it needs {use.pickups} {unit} ({left}). "
                    "Put fresh tip racks in and click New rack on the Runs tab."
                )
        return before, after

    def commit(self, after: dict[str, tuple[str, int]]) -> None:
        self._save(after)

    def _save(self, racks: dict[str, tuple[str, int]]) -> None:
        now = utcnow().isoformat()
        with self._lock, self._conn:
            self._conn.executemany(
                """INSERT INTO tip_racks (slot, load_name, used, updated_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT (slot) DO UPDATE SET load_name = excluded.load_name,
                       used = excluded.used, updated_at = excluded.updated_at""",
                [(slot, name, min(used, RACK_SIZE), now) for slot, (name, used) in racks.items()],
            )

    def close(self) -> None:
        self._conn.close()


# Appended to the protocol before it goes to the Flex. It passes everything through
# to the real protocol context, but starts each pipette at its rack's first unused tip.
_WRAPPER = '''

# --- Added by the master node: continue partly used tip racks ------------------------
_LAB_TIPS_USED = __USED__
_LAB_TIP_ORDER = [row + str(col) for col in range(1, 13) for row in "ABCDEFGH"]


class _LabTipProtocol:
    def __init__(self, protocol):
        object.__setattr__(self, "_lab_protocol", protocol)

    def __getattr__(self, name):
        return getattr(self._lab_protocol, name)

    def __setattr__(self, name, value):
        setattr(self._lab_protocol, name, value)

    def load_instrument(self, *args, **kwargs):
        pipette = self._lab_protocol.load_instrument(*args, **kwargs)
        racks = kwargs.get("tip_racks") or (args[2] if len(args) > 2 else None) or []
        for index, rack in enumerate(racks):
            used = _LAB_TIPS_USED.get(str(rack.parent), 0)
            if pipette.channels == 1:
                start = used
            elif pipette.channels == 8:
                start = -(-used // 8) * 8
            else:
                start = 0 if used == 0 else 96
            if start < 96:
                if start or index:
                    pipette.starting_tip = rack.wells_by_name()[_LAB_TIP_ORDER[start]]
                break
        return pipette


def run(protocol):
    return _lab_original_run(_LabTipProtocol(protocol))
'''


def with_tip_state(source: bytes, used: dict[str, int]) -> bytes:
    """The protocol, made to start each pipette at its first unused tip.

    Opentrons allows only one run() per file, so the protocol's own run() is
    renamed and the wrapper becomes run().
    """
    text, renamed = re.subn(r"^def run\(", "def _lab_original_run(", source.decode("utf-8"), count=1, flags=re.MULTILINE)
    if not renamed:
        raise TipProblem("The protocol has no top-level run(protocol) function")
    wrapper = _WRAPPER.replace("__USED__", repr(dict(sorted(used.items()))))
    return (text.rstrip() + "\n" + wrapper).encode()
