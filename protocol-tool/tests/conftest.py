from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from protocol_tool.assistant import ModelTurn
from protocol_tool.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"
GOOD_PROTOCOL = (FIXTURES / "dilution_series.py").read_text(encoding="utf-8")
BROKEN_PROTOCOL = GOOD_PROTOCOL.replace('"nest_12_reservoir_15ml"', '"no_such_reservoir"')


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings()
    s.server.database = str(tmp_path / "sessions.db")
    s.master.url = "http://master.test"
    s.knowledge.folder = str(tmp_path / "knowledge")
    return s


def tool_use(tool_id: str, name: str, **args: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": args}


class FakeModel:
    """Plays back scripted model turns and records what it was sent."""

    def __init__(self, turns: list[tuple[str, list[dict[str, Any]], str]]):
        # Each turn: (streamed text, extra content blocks, stop_reason)
        self.turns = list(turns)
        self.calls: list[list[dict[str, Any]]] = []

    async def stream(self, system, tools, messages):
        self.calls.append(copy.deepcopy(messages))
        text, blocks, stop_reason = self.turns.pop(0)
        content = []
        if text:
            words = text.split(" ")
            for i, word in enumerate(words):
                yield {"type": "text", "text": word if i == 0 else " " + word}
            content.append({"type": "text", "text": text})
        for block in blocks:
            if block["type"] == "tool_use":
                yield {"type": "tool_start", "name": block["name"]}
        content += blocks
        yield ModelTurn(content=content, stop_reason=stop_reason)


INSTRUMENTS = [
    {"id": "thor", "kind": "transport", "kind_label": "Transport", "name": "Jetson Thor", "driver": "manual", "plate_slots": [], "limits": {}},
    {"id": "flex", "kind": "liquid_handler", "kind_label": "Liquid handler", "name": "Opentrons Flex", "driver": "sim",
     "plate_slots": ["B1", "D2"], "limits": {}},
    {"id": "tecan", "kind": "plate_reader", "kind_label": "Plate reader", "name": "Tecan Infinite M200 Pro", "driver": "tecan_infinite",
     "plate_slots": ["carrier"], "limits": {"modes": ["absorbance"]}},
]
KIND = {"liquid_handling": "flex", "read": "tecan"}

FINISHED_RUN = {
    "id": "run-1", "status": "succeeded", "error": "", "has_results": True, "outputs": {},
    "protocol": {"name": "Dilution run", "scientist": "Test Scientist", "analysis": [{
        "title": "Standard curve", "kind": "standard_curve", "measurement": "Absorbance 600 nm", "fit": "linear",
        "standards": [{"name": "S1", "wells": ["A3"], "x": 1}, {"name": "S2", "wells": ["A4"], "x": 2}],
        "samples": [{"name": "U", "wells": ["A1"]}, {"name": "Empty", "wells": ["A2"]}],
    }]},
    "steps": [{"label": "Read plate", "device": "tecan", "status": "done", "detail": "Stored 2 values"}],
    "events": [{"level": "info", "message": "Run succeeded"}],
}


class FakeMaster:
    """httpx transport standing in for the master node. Plans steps onto flex/tecan; only absorbance reads."""

    def __init__(self):
        self.submitted: list[dict[str, Any]] = []
        self.planned: list[dict[str, Any]] = []

    @staticmethod
    def plan(body: dict[str, Any]) -> httpx.Response:
        steps = [{"key": "decode_plan", "label": "Decode + plan", "device": "master", "params": {}}]
        for n, step in enumerate(body["steps"], start=1):
            if step["type"] not in KIND:
                return httpx.Response(422, json={"detail": f"Step {n} ({step['type']}) needs an instrument the registry doesn't have"})
            if step["type"] == "read" and step["settings"]["mode"] != "absorbance":
                return httpx.Response(422, json={"detail": f"Step {n} (read): {step['settings']['mode']} is not available (only absorbance)"})
            device = step.get("instrument") or KIND[step["type"]]
            steps.append({"key": "transfer", "label": f"Move plate to {device}", "device": "thor", "params": {}})
            steps.append({"key": "operation", "label": "Run protocol" if step["type"] == "liquid_handling" else "Read plate",
                          "device": device, "params": {"summary": step["type"]}})
        return httpx.Response(200, json={"steps": steps, "warnings": []})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/plan":
            body = json.loads(request.content)
            self.planned.append(body)
            return self.plan(body)
        if request.method == "POST" and path == "/runs":
            body = json.loads(request.content)
            self.submitted.append(body)
            return httpx.Response(201, json={"id": "run-1", "status": "queued", "protocol": {"name": body["name"]}})
        if path == "/runs":
            return httpx.Response(200, json=[{"id": "run-1", "status": "running"}])
        if path == "/instruments":
            return httpx.Response(200, json={"instruments": INSTRUMENTS})
        if request.method == "POST" and path == "/transport/reset":
            return httpx.Response(200, json={"at_home": True, "holding_plate": False, "detail": "home, empty-handed"})
        if path == "/runs/run-1":
            return httpx.Response(200, json=FINISHED_RUN)
        if path == "/runs/run-1/results":
            return httpx.Response(200, json={"measurements": [{"label": "Absorbance 600 nm", "unit": "OD",
                                                               "values": {"A1": 1.2, "A2": None, "A3": 1.0, "A4": 2.0}}]})
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/tips":
            return httpx.Response(200, json={"racks": [{"slot": "C1", "load_name": "opentrons_flex_96_tiprack_200ul",
                                                        "used": 40, "remaining": 56, "next_tip": "A6"}]})
        if path == "/runs/run-1/results.csv":
            return httpx.Response(200, text="well,value\nA1,0.5\n", headers={"content-type": "text/csv", "content-disposition": 'attachment; filename="run-1.csv"'})
        return httpx.Response(404, json={"detail": "not found"})
