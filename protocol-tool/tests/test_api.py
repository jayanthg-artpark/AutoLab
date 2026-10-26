"""HTTP API with a scripted model and a fake master node."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from protocol_tool.main import create_app
from protocol_tool.master import MasterClient

from .conftest import GOOD_PROTOCOL, FakeMaster, FakeModel, tool_use


def make_client(settings, turns=()):
    fake_master = FakeMaster()
    master = MasterClient(settings.master, transport=httpx.MockTransport(fake_master))
    app = create_app(settings, model=FakeModel(list(turns)), master=master)
    return TestClient(app), fake_master


def read_events(response) -> list[dict]:
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


SEND = {"name": "Dilution run", "scientist": "Test Scientist", "storage_slot": "R1-S3", "barcode": "PL-1"}


def test_design_review_approve_and_send(settings):
    turns = [
        ("", [tool_use("t1", "write_protocol", title="Dilution series", summary="Two-fold series", plate_slot="D2", code=GOOD_PROTOCOL),
              tool_use("t2", "set_reader_settings", mode="absorbance", wavelengths_nm=[600, 450], wells=["A1:H12"])], "tool_use"),
        ("Ready to send.", [], "end_turn"),
    ]
    client, master = make_client(settings, turns)
    with client:
        session = client.post("/api/sessions").json()
        sid = session["id"]
        assert session["design"]["protocols"] == [] and session["design"]["workflow"] == []
        assert "messages" not in session

        # Nothing to review or send yet.
        assert client.post(f"/api/sessions/{sid}/plan", json=SEND).status_code == 409
        assert client.post(f"/api/sessions/{sid}/send", json=SEND).status_code == 409

        response = client.post(
            f"/api/sessions/{sid}/chat",
            data={"message": "Two-fold dilution, read at 600 and 450 nm"},
            files=[("files", ("plan.txt", b"Use 100 uL per well", "text/plain"))],
        )
        events = read_events(response)
        assert events[0]["entry"]["attachments"] == ["plan.txt"]
        done = events[-1]
        assert done["type"] == "done"
        protocol = done["session"]["design"]["protocols"][0]
        assert protocol["id"] == "main" and protocol["simulation"]["ok"] is True and protocol["simulation_current"] is True
        assert [s["type"] for s in done["session"]["design"]["workflow"]] == ["liquid_handling", "read"]
        assert done["session"]["send_problems"] == []

        # The scientist adjusts the read by hand; invalid edits are rejected.
        bad = client.patch(f"/api/sessions/{sid}/design", json={"reader": {"mode": "fluorescence", "wells": ["A1"]}})
        assert bad.status_code == 422
        ok = client.patch(f"/api/sessions/{sid}/design", json={"reader": {"mode": "absorbance", "wavelengths_nm": [600], "wells": ["A1", "B1"]}})
        read = ok.json()["design"]["workflow"][1]
        assert read["settings"]["wells"] == ["A1", "B1"]

        assert client.post(f"/api/sessions/{sid}/simulate").json()["design"]["protocols"][0]["simulation"]["ok"] is True

        # Sending needs an approved plan.
        unapproved = client.post(f"/api/sessions/{sid}/send", json=SEND)
        assert unapproved.status_code == 409 and "approve" in unapproved.json()["detail"]
        plan = client.post(f"/api/sessions/{sid}/plan", json=SEND)
        assert plan.status_code == 200, plan.text
        assert [s["device"] for s in plan.json()["steps"]] == ["master", "thor", "flex", "thor", "tecan"]
        assert plan.json()["instruments"]["tecan"]["name"] == "Tecan Infinite M200 Pro"
        assert master.submitted == []  # reviewing sends nothing

        # A change after review voids the approval.
        changed = client.post(f"/api/sessions/{sid}/send", json={**SEND, "barcode": "PL-2", "plan_hash": plan.json()["plan_hash"]})
        assert changed.status_code == 409 and "review it again" in changed.json()["detail"]

        sent = client.post(f"/api/sessions/{sid}/send", json={**SEND, "plan_hash": plan.json()["plan_hash"]})
        assert sent.status_code == 201, sent.text
        assert sent.json()["run"]["id"] == "run-1"

        payload = master.submitted[0]
        assert payload["plate"] == {"barcode": "PL-1", "storage_slot": "R1-S3", "labware": "corning_96_wellplate_360ul_flat"}
        liquid, read = payload["steps"]
        assert liquid["type"] == "liquid_handling" and liquid["deck_slot"] == "D2"
        assert liquid["protocol_filename"] == "dilution_series.py"
        assert liquid["tips"] == [{
            "pipette": "p1000_single_flex", "channels": 1, "pickups": 90,
            "racks": [{"slot": "C1", "load_name": "opentrons_flex_96_tiprack_200ul"}],
        }]
        assert read["settings"]["wells"] == ["A1", "B1"] and read["settings"]["wavelengths_nm"] == [600]
        assert payload["notes"] == "Two-fold series"

        # Later edits to the design don't change what the run was sent with...
        client.patch(f"/api/sessions/{sid}/design", json={"reader": {"mode": "absorbance", "wavelengths_nm": [450], "wells": ["C1"]}})
        assert client.get("/api/runs/run-1/design").json() == {
            "available": True, "source": "snapshot", "session_id": sid, "title": "Dilution series"}
        # ...and reopening the run gives a fresh copy of the design as it was sent, ready to send again.
        reopened = client.post("/api/runs/run-1/reopen")
        assert reopened.status_code == 201
        copy = reopened.json()
        assert copy["session"]["id"] != sid and copy["source"] == "snapshot"
        assert copy["send"] == {**SEND, "notes": "", "labware": ""}
        assert copy["session"]["design"]["workflow"][1]["settings"]["wells"] == ["A1", "B1"]
        assert copy["session"]["submitted_runs"] == [] and copy["session"]["send_problems"] == []
        assert copy["session"]["transcript"][-1]["text"].startswith("Reopened the design of run run-1, as it was sent.")
        again = client.post(f"/api/sessions/{copy['session']['id']}/plan", json=SEND)
        assert again.status_code == 200 and again.json()["plan_hash"] == plan.json()["plan_hash"]
        assert client.get(f"/api/sessions/{sid}").json()["design"]["workflow"][1]["settings"]["wells"] == ["C1"]  # original untouched

        code = client.get(f"/api/sessions/{sid}/protocol.py")
        assert code.text == GOOD_PROTOCOL
        assert client.get(f"/api/sessions/{sid}/protocol.py?protocol=other").status_code == 404


def test_plan_shows_what_the_master_node_rejects(settings):
    turns = [
        ("", [tool_use("t1", "write_protocol", title="t", summary="s", plate_slot="D2", code=GOOD_PROTOCOL),
              tool_use("t2", "set_reader_settings", mode="luminescence", wells=["A1"])], "tool_use"),
        ("Done.", [], "end_turn"),
    ]
    client, master = make_client(settings, turns)
    with client:
        sid = client.post("/api/sessions").json()["id"]
        client.post(f"/api/sessions/{sid}/chat", data={"message": "go"})
        response = client.post(f"/api/sessions/{sid}/plan", json=SEND)
        assert response.status_code == 422
        assert "luminescence is not available" in response.json()["detail"]


def test_send_requires_a_current_passing_simulation(settings):
    turns = [("", [tool_use("t1", "write_protocol", title="t", summary="s", plate_slot="D2", code="def run(protocol):\n    protocol.home(\n")], "tool_use"),
             ("It fails.", [], "end_turn")]
    client, master = make_client(settings, turns)
    with client:
        sid = client.post("/api/sessions").json()["id"]
        client.post(f"/api/sessions/{sid}/chat", data={"message": "go"})
        response = client.post(f"/api/sessions/{sid}/send", json={**SEND, "plan_hash": "x"})
        assert response.status_code == 409
        assert "failed simulation" in response.json()["detail"]
        assert master.submitted == []


def test_designs_checked_under_older_rules_are_checked_again_before_review(settings):
    import asyncio

    from protocol_tool.models import ProtocolDraft, Session
    from protocol_tool.simulator import DeckRules, simulate
    from protocol_tool.store import SessionStore

    old_code = GOOD_PROTOCOL.replace('"2.29"', '"2.20"')
    store = SessionStore(settings.server.database)
    session = Session.model_validate({"design": {
        "protocols": [{"id": "main", "code": old_code, "version": 1, "plate_slot": "D2"}],
        "workflow": [{"type": "liquid_handling", "protocol_id": "main"}],
    }})
    # Passed when API levels and labware weren't checked yet.
    session.design.protocols[0].simulation = asyncio.run(simulate(old_code, "D2", rules=DeckRules()))
    assert session.design.send_problems() == []
    store.save(session)
    store.close()

    client, master = make_client(settings)
    with client:
        response = client.post(f"/api/sessions/{session.id}/plan", json=SEND)
        assert response.status_code == 409 and "failed simulation" in response.json()["detail"]
        errors = client.get(f"/api/sessions/{session.id}").json()["design"]["protocols"][0]["simulation"]["errors"]
        assert any("API level 2.29 (this protocol uses 2.20)" in e for e in errors)
        assert master.planned == []


def test_runs_sent_before_snapshots_reopen_their_current_design(settings):
    from protocol_tool.models import Session
    from protocol_tool.store import SessionStore

    store = SessionStore(settings.server.database)
    old = Session.model_validate({"design": {"title": "Old design"}, "submitted_runs": ["run-old"]})
    store.save(old)
    store.close()
    client, _ = make_client(settings)
    with client:
        assert client.get("/api/runs/run-old/design").json()["source"] == "current"
        copy = client.post("/api/runs/run-old/reopen").json()
        assert copy["session"]["design"]["title"] == "Old design" and copy["send"] == {}
        assert "as it is now" in copy["session"]["transcript"][-1]["text"]
        assert client.get("/api/runs/someone-elses/design").json() == {"available": False}
        assert client.post("/api/runs/someone-elses/reopen").status_code == 404


def test_runs_are_relayed_from_the_master_node(settings):
    client, _ = make_client(settings)
    with client:
        assert client.get("/api/runs").json() == [{"id": "run-1", "status": "running"}]
        csv = client.get("/api/runs/run-1/results.csv")
        assert csv.headers["content-disposition"] == 'attachment; filename="run-1.csv"'
        assert client.get("/api/runs/missing").status_code == 404
        assert client.get("/api/health").json()["master"] == {"status": "ok"}
        assert client.get("/api/tips").json()["racks"][0]["remaining"] == 56
        assert [i["id"] for i in client.get("/api/instruments").json()["instruments"]] == ["thor", "flex", "tecan"]
        assert client.post("/api/transport/reset").json()["at_home"] is True


def test_explain_summarises_a_finished_run(settings):
    client, _ = make_client(settings, [("OD600 ranged up to 1.2; A2 had no reading.", [], "end_turn")])
    with client:
        assert client.get("/api/runs/run-1/explain").json() == {"enabled": True, "note": None}
        note = client.post("/api/runs/run-1/explain").json()["note"]
        assert note["text"] == "OD600 ranged up to 1.2; A2 had no reading."
        assert client.get("/api/runs/run-1/explain").json()["note"]["text"] == note["text"]


def test_analysis_of_a_finished_run(settings):
    client, _ = make_client(settings)
    with client:
        analysis = client.get("/api/runs/run-1/analysis").json()["analyses"][0]
        assert analysis["series"][0]["fit"]["equation"] == "y = 1·x"
        u, empty = analysis["samples"]
        assert u["value"] == pytest.approx(1.2) and empty["flag"] == "not read"
        csv = client.get("/api/runs/run-1/analysis.csv")
        assert csv.headers["content-disposition"] == 'attachment; filename="run-1-analysis.csv"'
        assert ",sample,U," in csv.text


def test_explain_can_be_turned_off(settings):
    settings.results.explain = False
    client, _ = make_client(settings)
    with client:
        assert client.post("/api/runs/run-1/explain").status_code == 403


def test_knowledge_documents_are_listed(settings, tmp_path):
    (tmp_path / "kb" / "templates").mkdir(parents=True)
    (tmp_path / "kb" / "templates" / "od600.md").write_text("# OD600 growth curve\n\nRead at 600 nm.")
    settings.knowledge.folder = str(tmp_path / "kb")
    client, _ = make_client(settings)
    with client:
        docs = client.get("/api/knowledge").json()["documents"]
        assert [(d["id"], d["title"]) for d in docs] == [("templates/od600.md", "OD600 growth curve")]


def test_rejects_unsupported_uploads(settings):
    client, _ = make_client(settings)
    with client:
        sid = client.post("/api/sessions").json()["id"]
        response = client.post(f"/api/sessions/{sid}/chat", data={"message": "hi"},
                               files=[("files", ("tool.exe", b"MZ", "application/octet-stream"))])
        assert response.status_code == 415


def test_access_password_protects_everything(settings):
    settings.server.access_password = "lab-secret"
    client, _ = make_client(settings)
    with client:
        for path in ("/", "/js/main.js", "/api/health", "/api/runs"):
            denied = client.get(path)
            assert denied.status_code == 401, path
            assert denied.headers["www-authenticate"].startswith("Basic")
        assert client.get("/api/health", auth=("anyone", "wrong")).status_code == 401
        assert client.get("/", auth=("anyone", "lab-secret")).status_code == 200
        assert client.get("/api/health", auth=("scientist", "lab-secret")).status_code == 200


def test_slow_replies_send_keep_alives(settings, monkeypatch):
    import asyncio

    from protocol_tool import main

    monkeypatch.setattr(main, "KEEPALIVE_S", 0.05)

    class SlowModel(FakeModel):
        async def stream(self, system, tools, messages):
            await asyncio.sleep(0.3)
            async for event in super().stream(system, tools, messages):
                yield event

    master = MasterClient(settings.master, transport=httpx.MockTransport(FakeMaster()))
    with TestClient(create_app(settings, model=SlowModel([("Done.", [], "end_turn")]), master=master)) as client:
        sid = client.post("/api/sessions").json()["id"]
        response = client.post(f"/api/sessions/{sid}/chat", data={"message": "hi"})
        assert ": keep-alive" in response.text
        assert read_events(response)[-1]["type"] == "done"
