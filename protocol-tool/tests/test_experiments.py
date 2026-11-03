"""The experiment notebook: records of finished runs, the scientist's rating, and how they rank."""

import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from protocol_tool.experiments import experiment_record, rating_of, record_id
from protocol_tool.knowledge import Knowledge
from protocol_tool.main import create_app
from protocol_tool.master import MasterClient

from .conftest import FINISHED_RUN, GOOD_PROTOCOL, FakeMaster, FakeModel, tool_use
from .test_api import SEND, make_client
from .test_assistant import run_turn

DESIGN_TURNS = [
    ("", [tool_use("t1", "record_requirements", title="Dilution series", assay="OD600 dilution series", samples="One culture"),
          tool_use("t2", "write_protocol", title="Dilution series", summary="Two-fold series", plate_slot="D2", code=GOOD_PROTOCOL),
          tool_use("t3", "set_reader_settings", mode="absorbance", wavelengths_nm=[600], wells=["A1:A4"])], "tool_use"),
    ("Ready to send.", [], "end_turn"),
]


def record_text(settings, run_id: str) -> str:
    return (Path(settings.knowledge.folder) / record_id(run_id)).read_text(encoding="utf-8")


def send_run(client) -> None:
    sid = client.post("/api/sessions").json()["id"]
    client.post(f"/api/sessions/{sid}/chat", data={"message": "Two-fold dilution, read OD600"})
    plan = client.post(f"/api/sessions/{sid}/plan", json=SEND).json()
    assert client.post(f"/api/sessions/{sid}/send", json={**SEND, "plan_hash": plan["plan_hash"]}).status_code == 201


def test_rating_a_run_writes_its_experiment_record(settings):
    client, _ = make_client(settings, DESIGN_TURNS)
    with client:
        send_run(client)
        assert client.get("/api/runs/run-1/feedback").json() == {"enabled": True, "feedback": None, "record": None}
        assert client.put("/api/runs/run-1/feedback", json={"rating": "great"}).status_code == 422

        saved = client.put("/api/runs/run-1/feedback", json={"rating": "partly", "notes": " Start the curve higher. "}).json()
        assert saved["feedback"]["rating"] == "partly" and saved["feedback"]["notes"] == "Start the curve higher."
        assert saved["record"] == "experiments/run-1.md"
        assert client.get("/api/runs/run-1/feedback").json()["feedback"]["rating"] == "partly"

        text = record_text(settings, "run-1")
        assert text.startswith("# Lab experiment: Dilution run\nRating: partly worked\n")
        assert "Assay: OD600 dilution series" in text and "Start the curve higher." in text
        assert "R² 1" in text and "sample U: 1.2" in text  # the computed analysis
        assert GOOD_PROTOCOL.strip() in text  # the protocol exactly as sent

        # It's in the knowledge the assistant searches straight away, with its rating.
        docs = {d["id"]: d for d in client.get("/api/knowledge").json()["documents"]}
        assert docs["experiments/run-1.md"]["kind"] == "Lab experiment" and docs["experiments/run-1.md"]["rating"] == "partly"


def test_the_summary_goes_into_an_existing_record(settings):
    client, _ = make_client(settings, [*DESIGN_TURNS, ("The standards fit a straight line.", [], "end_turn")])
    with client:
        send_run(client)
        client.put("/api/runs/run-1/feedback", json={"rating": "worked"})
        client.post("/api/runs/run-1/explain")
        text = record_text(settings, "run-1")
        assert "## AI summary of the run\nThe standards fit a straight line." in text


def test_feedback_waits_for_the_run_to_finish(settings, monkeypatch):
    monkeypatch.setitem(FINISHED_RUN, "status", "running")
    client, _ = make_client(settings)
    with client:
        assert client.put("/api/runs/run-1/feedback", json={"rating": "worked"}).status_code == 409


def test_the_notebook_can_be_turned_off(settings):
    settings.knowledge.record_experiments = False
    client, _ = make_client(settings)
    with client:
        assert client.get("/api/runs/run-1/feedback").json()["enabled"] is False
        assert client.put("/api/runs/run-1/feedback", json={"rating": "worked"}).status_code == 403


def test_finished_runs_are_recorded_without_anyone_rating_them(settings):
    class FinishedMaster(FakeMaster):
        def __call__(self, request):
            if request.url.path == "/runs" and request.method == "GET":
                return httpx.Response(200, json=[{"id": "run-1", "status": "succeeded"}, {"id": "run-2", "status": "running"}])
            return super().__call__(request)

    master = MasterClient(settings.master, transport=httpx.MockTransport(FinishedMaster()))
    with TestClient(create_app(settings, model=FakeModel([]), master=master)) as client:
        for _ in range(100):
            if client.get("/api/runs/run-1/feedback").json()["record"]:
                break
            time.sleep(0.05)
        assert client.get("/api/runs/run-1/feedback").json()["record"] == "experiments/run-1.md"
        assert client.get("/api/runs/run-2/feedback").json()["record"] is None
    text = record_text(settings, "run-1")
    assert "Rating: not reviewed" in text and "Nobody has rated this run yet" in text


def test_records_rank_by_rating(tmp_path):
    run = {**FINISHED_RUN, "protocol": {**FINISHED_RUN["protocol"], "name": "Bradford assay"}}
    root = tmp_path / "knowledge"
    (root / "experiments").mkdir(parents=True)
    for run_id, rating in (("r-none", ""), ("r-failed", "failed"), ("r-worked", "worked"), ("r-partly", "partly")):
        feedback = {"rating": rating, "notes": ""} if rating else None
        (root / record_id(run_id)).write_text(experiment_record({**run, "id": run_id}, None, feedback=feedback), encoding="utf-8")
    (root / "templates").mkdir()
    (root / "templates" / "bradford.md").write_text("# Bradford assay template\n\nBradford assay at 595 nm.")

    kb = Knowledge(root)
    ranked = [p.doc for p, _ in kb.search("Bradford assay", folder="experiments", per_doc=1)]
    assert ranked == ["experiments/r-worked.md", "experiments/r-partly.md", "experiments/r-failed.md", "experiments/r-none.md"]
    assert [d.rating for d in kb.documents() if d.id == "experiments/r-none.md"] == [""]
    assert "4 lab experiments under experiments/ (1 worked, 1 partly worked, 1 failed, 1 not reviewed" in kb.describe()
    assert rating_of("# Bradford template\nRating: worked") == "worked" and rating_of("# Notes\nno rating") == ""


def test_the_assistant_sees_the_rating_of_past_experiments(settings, tmp_path):
    root = tmp_path / "knowledge"
    (root / "experiments").mkdir(parents=True)
    (root / record_id("run-9")).write_text(
        experiment_record({**FINISHED_RUN, "id": "run-9", "status": "failed", "error": "Read plate: reader lid open"}, None,
                          feedback={"rating": "failed", "notes": "Close the lid first."}), encoding="utf-8")
    model = FakeModel([
        ("", [tool_use("t1", "search_knowledge", query="dilution run", folder="experiments"),
              tool_use("t2", "search_knowledge", query="ELISA", folder="experiments"),
              tool_use("t3", "search_knowledge", query="x", folder="nowhere")], "tool_use"),
        ("A similar run failed because the reader lid was open.", [], "end_turn"),
    ])
    run_turn(settings, model, knowledge=Knowledge(root))
    found, none, bad = model.calls[1][-1]["content"]
    assert 'doc="experiments/run-9.md"' in found["content"] and 'rating="failed"' in found["content"]
    assert "Close the lid first." in found["content"]
    assert none["content"] == "No past experiments in this lab match 'ELISA'."
    assert bad["is_error"] and "folder must be one of" in bad["content"]
    assert "1 lab experiments under experiments/ (1 failed" in model.calls[0][0]["content"][0]["text"]
