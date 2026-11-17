"""Protocols the scientist gives as they are (e.g. from Opentrons Protocol Designer).

The assistant never retypes these. The only change allowed is moving labware to other deck slots,
made here by rewriting just the slot names in the protocol's load and move calls: every other
character of the file stays as it was given.
"""

from __future__ import annotations

import ast
import re

SLOT = re.compile(r"^[A-D][1-4]$")
# Calls that put something on the deck, and which argument is its slot: (positional index, keyword).
SLOT_ARGS = {
    "load_labware": (1, "location"),
    "load_module": (1, "location"),
    "load_adapter": (1, "location"),
    "load_trash_bin": (0, "location"),
    "move_labware": (1, "new_location"),
}


class SlotMoveError(ValueError):
    """The slot moves can't be applied to this protocol."""


def _slot_constants(tree: ast.AST) -> list[ast.Constant]:
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in SLOT_ARGS):
            continue
        index, keyword = SLOT_ARGS[node.func.attr]
        arg = next((k.value for k in node.keywords if k.arg == keyword), None)
        if arg is None and len(node.args) > index:
            arg = node.args[index]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and SLOT.match(arg.value.upper()):
            found.append(arg)
    return found


def slots_used(code: str) -> set[str]:
    """The deck slots the protocol names in its load and move calls."""
    return {c.value.upper() for c in _slot_constants(ast.parse(code))}


def move_slots(code: str, moves: dict[str, str]) -> str:
    """The protocol with everything loaded in (or moved to) each `from` slot put in its `to` slot instead.
    Only those slot strings change."""
    moves = {str(a).strip().upper(): str(b).strip().upper() for a, b in moves.items()}
    for a, b in moves.items():
        if not (SLOT.match(a) and SLOT.match(b)):
            raise SlotMoveError(f"Slot moves go between deck slots A1-D4, not {a!r} -> {b!r}")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise SlotMoveError(f"The protocol isn't valid Python: {exc}") from exc
    constants = _slot_constants(tree)
    used = {c.value.upper() for c in constants}
    if missing := [a for a in moves if a not in used]:
        raise SlotMoveError(f"The protocol puts nothing in {', '.join(missing)}; it uses {', '.join(sorted(used))}")
    if taken := [b for a, b in moves.items() if b in used and b not in moves]:
        raise SlotMoveError(f"{', '.join(taken)} already hold labware in this protocol; move that too or pick a free slot")
    # AST offsets are in UTF-8 bytes per line; replace from the end so earlier offsets stay valid.
    lines = code.encode("utf-8").splitlines(keepends=True)
    for c in sorted(constants, key=lambda c: (c.lineno, c.col_offset), reverse=True):
        new = moves.get(c.value.upper())
        if new is None or c.lineno != c.end_lineno:
            continue
        line = lines[c.lineno - 1]
        literal = line[c.col_offset:c.end_col_offset]
        quote = literal[-1:]
        lines[c.lineno - 1] = line[:c.col_offset] + literal[: literal.index(quote) + 1] + new.encode() + quote + line[c.end_col_offset:]
    return b"".join(lines).decode("utf-8")
