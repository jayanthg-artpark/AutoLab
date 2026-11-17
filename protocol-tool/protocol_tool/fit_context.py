"""Shortening a conversation so it fits a model's context window.

The saved conversation (Session.messages) is never changed: only the copy sent to the
model is shortened, and only when it is too long. What goes first is what the model needs
least, so a design keeps working on a model with a small window (e.g. 64k tokens):

1. the design state sent with every scientist message (only the latest one is current);
2. the code of protocol versions that a later write_protocol replaced;
3. files attached before the latest message (they stay attached to the design, so
   use_given_protocol still works; only their text leaves the model's view);
4. the long tool results before the latest message (clipped);
5. the oldest exchanges, a whole scientist turn at a time.

The latest scientist message and everything after it are always sent as they are.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from .model_status import ModelError

CLIPPED_RESULT_CHARS = 600
DROPPED_NOTE = ("(The start of this conversation was left out to fit the AI model's context window. "
                "The design state below is current.)")


class ContextTooLong(ModelError):
    """The server refused the request as longer than the model's context window."""

    def __init__(self, message: str, limit: int, used: int | None = None):
        super().__init__(message)
        self.limit = limit  # the model's context window, in tokens
        self.used = used  # how many tokens the refused request had, if the server said


def fit(messages: list[dict[str, Any]], fits: Callable[[list[dict[str, Any]]], bool]) -> list[dict[str, Any]] | None:
    """`messages`, shortened until `fits` accepts them; None if even the latest turn alone is too long."""
    if fits(messages):
        return messages
    msgs = copy.deepcopy(messages)
    turns = [i for i, m in enumerate(msgs) if _is_scientist_turn(m)]
    last = turns[-1] if turns else len(msgs)
    for step in (_drop_old_design_states, _drop_replaced_code, _drop_old_attachments, _clip_old_results):
        step(msgs, last)
        if fits(msgs):
            return msgs
    for start in turns[1:]:
        kept = msgs[start:]
        first = dict(kept[0])
        first["content"] = [{"type": "text", "text": DROPPED_NOTE}, *_blocks(first)]
        kept = [first, *kept[1:]]
        if fits(kept):
            return kept
    return None


def _blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message["content"]
    return [{"type": "text", "text": content}] if isinstance(content, str) else list(content)


def _is_scientist_turn(message: dict[str, Any]) -> bool:
    """A message the scientist sent (not the tool results the loop sends back)."""
    blocks = _blocks(message)
    return message["role"] == "user" and bool(blocks) and all(b.get("type") != "tool_result" for b in blocks)


def _drop_old_design_states(msgs: list[dict[str, Any]], last: int) -> None:
    for m in msgs[:last]:
        if m["role"] == "user" and isinstance(m["content"], list):
            m["content"] = [b for b in m["content"]
                            if not (b.get("type") == "text" and b.get("text", "").startswith("<design_state>"))]


def _drop_replaced_code(msgs: list[dict[str, Any]], last: int) -> None:
    latest: dict[str, dict[str, Any]] = {}
    for m in msgs:
        if m["role"] == "assistant" and isinstance(m["content"], list):
            for b in m["content"]:
                if b.get("type") == "tool_use" and b.get("name") == "write_protocol" and isinstance(b.get("input"), dict):
                    pid = str(b["input"].get("protocol_id") or "main").strip().lower()
                    if pid in latest:
                        latest[pid]["input"]["code"] = "# (left out: a later write_protocol replaced this version)"
                    latest[pid] = b


def _drop_old_attachments(msgs: list[dict[str, Any]], last: int) -> None:
    for m in msgs[:last]:
        if m["role"] == "user" and isinstance(m["content"], list):
            m["content"] = [_stub(b) if b.get("type") in ("document", "image") else b for b in m["content"]]


def _stub(block: dict[str, Any]) -> dict[str, Any]:
    name = f"'{block['title']}'" if block.get("title") else ("an image" if block.get("type") == "image" else "a file")
    return {"type": "text", "text": (
        f"(The scientist attached {name} here. Its contents were left out to fit the AI model's context window; "
        "the file is still attached to the design. If you need details from it, ask the scientist.)")}


def _clip_old_results(msgs: list[dict[str, Any]], last: int) -> None:
    for m in msgs[:last]:
        if m["role"] != "user" or not isinstance(m["content"], list):
            continue
        for b in m["content"]:
            if b.get("type") != "tool_result":
                continue
            text = b.get("content", "")
            if isinstance(text, list):
                text = "\n".join(p.get("text", "") for p in text if isinstance(p, dict))
            if len(text) > CLIPPED_RESULT_CHARS:
                b["content"] = text[:CLIPPED_RESULT_CHARS] + " …(the rest was left out)"
