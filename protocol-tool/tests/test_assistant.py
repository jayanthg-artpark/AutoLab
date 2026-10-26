"""The chat loop, with scripted model turns instead of the Claude API."""

import asyncio

import httpx

from protocol_tool.assistant import Assistant
from protocol_tool.knowledge import Knowledge
from protocol_tool.models import Session
from protocol_tool.store import SessionStore

from .conftest import BROKEN_PROTOCOL, GOOD_PROTOCOL, INSTRUMENTS, FakeMaster, FakeModel, tool_use


def run_turn(settings, model, session=None, text="Make a dilution series and read OD600", **lab):
    store = SessionStore(settings.server.database)
    session = session or Session()
    store.save(session)
    assistant = Assistant(settings, store, model, **lab)

    async def go():
        return [event async for event in assistant.chat(session, text, [])]

    return asyncio.run(go()), session


def check_history(messages):
    """Every tool_use is answered by a tool_result in the next message."""
    for i, message in enumerate(messages):
        if message["role"] != "assistant":
            continue
        ids = {b["id"] for b in message["content"] if b.get("type") == "tool_use"}
        if ids:
            answered = {b["tool_use_id"] for b in messages[i + 1]["content"] if b.get("type") == "tool_result"}
            assert ids == answered


async def instruments():
    return INSTRUMENTS


async def preview_plan(submission):
    response = FakeMaster.plan(submission)
    return ("ok", response.json()) if response.status_code == 200 else ("rejected", response.json()["detail"])


def test_claude_fixes_a_failing_protocol_and_sets_the_reader(settings):
    model = FakeModel([
        ("Drafting a dilution series.", [tool_use("t1", "write_protocol", title="Dilution", summary="v1", plate_slot="D2", code=BROKEN_PROTOCOL)], "tool_use"),
        ("", [
            tool_use("t2", "write_protocol", title="Dilution series", summary="Two-fold series", plate_slot="D2", code=GOOD_PROTOCOL),
            tool_use("t3", "set_reader_settings", mode="absorbance", wavelengths_nm=[600], wells=["A1:H11"]),
        ], "tool_use"),
        ("Done: the protocol passes simulation.", [], "end_turn"),
    ])
    events, session = run_turn(settings, model)

    # What Claude was told after each tool round.
    first_results = model.calls[1][-1]["content"]
    assert "Simulation FAILED" in first_results[0]["content"]
    assert "no_such_reservoir" in first_results[0]["content"]
    second_results = model.calls[2][-1]["content"]
    assert "Simulation PASSED" in second_results[0]["content"]
    assert second_results[1]["content"].startswith("Reader settings saved: absorbance at 600 nm; 88 wells")

    design = session.design
    protocol = design.protocol("main")
    assert protocol.version == 2 and protocol.title == "Dilution series"
    assert protocol.simulation.ok and protocol.simulation_current
    read = design.read_steps()[0]
    assert read.settings.wells[-1] == "H11" and len(read.settings.wells) == 88

    kinds = [e["type"] for e in events]
    assert kinds[0] == "entry" and kinds[-1] == "done"
    assert kinds.count("tool_done") == 3
    reply = events[-1]["entry"]
    assert reply["text"] == "Drafting a dilution series.\n\nDone: the protocol passes simulation."
    assert [t["ok"] for t in reply["tools"]] == [False, True, True]
    # Text and tool activity keep the order they happened in.
    assert [p["type"] for p in reply["parts"]] == ["text", "tool", "tool", "tool", "text"]
    assert reply["parts"][-1]["text"] == "Done: the protocol passes simulation."
    check_history(session.messages)

    # The scientist's message carries the design state block.
    first_user = model.calls[0][0]["content"]
    assert first_user[0]["text"].startswith("<design_state>")
    assert first_user[-1]["text"] == "Make a dilution series and read OD600"


def test_requirements_then_a_workflow_checked_by_the_master_node(settings):
    model = FakeModel([
        ("", [tool_use("t1", "record_requirements", title="OD600 series", assay="Dye dilution", readout="Absorbance 600 nm",
                       assumptions=["100 uL per well"], open_questions=["Which dye?"])], "tool_use"),
        ("", [tool_use("t2", "write_protocol", protocol_id="dilute", title="Dilute", summary="s", plate_slot="D2", code=GOOD_PROTOCOL)], "tool_use"),
        ("", [tool_use("t3", "set_workflow", steps=[
            {"type": "liquid_handling", "protocol_id": "dilute", "label": "Make the series"},
            {"type": "read", "settings": {"mode": "fluorescence", "excitation_nm": 485, "emission_nm": 535, "wells": ["A1"]}},
        ])], "tool_use"),
        ("", [tool_use("t4", "set_workflow", steps=[
            {"type": "liquid_handling", "protocol_id": "dilute", "label": "Make the series"},
            {"type": "read", "settings": {"mode": "absorbance", "wavelengths_nm": [600], "wells": ["A1:H12"]}},
        ])], "tool_use"),
        ("Planned.", [], "end_turn"),
    ])
    events, session = run_turn(settings, model, instruments=instruments, preview_plan=preview_plan)

    design = session.design
    assert design.title == "OD600 series"
    assert design.requirements.open_questions == ["Which dye?"]
    assert [p.id for p in design.protocols] == ["dilute"]

    rejected = model.calls[3][-1]["content"][0]["content"]
    assert "REJECTED" in rejected and "fluorescence is not available" in rejected
    planned = model.calls[4][-1]["content"][0]["content"]
    assert "The master node planned it as:" in planned
    assert "- Read plate (Tecan Infinite M200 Pro)" in planned
    assert [s.type for s in design.workflow] == ["liquid_handling", "read"]
    assert design.workflow[0].label == "Make the series"
    assert [t["ok"] for t in events[-1]["entry"]["tools"]] == [True, True, False, True]

    # The model is told which instruments exist and their limits.
    state = model.calls[0][0]["content"][0]["text"]
    assert "- tecan: Tecan Infinite M200 Pro (plate reader); plate positions carrier; limits {\"modes\": [\"absorbance\"]}" in state
    assert "- flex: Opentrons Flex (liquid handler, simulated)" in state
    check_history(session.messages)


def test_workflow_must_use_protocols_that_exist(settings):
    model = FakeModel([
        ("", [tool_use("t1", "set_workflow", steps=[{"type": "liquid_handling", "protocol_id": "nope"}])], "tool_use"),
        ("", [tool_use("t2", "set_workflow", steps=[{"type": "wash", "cycles": 99}])], "tool_use"),
        ("", [tool_use("t3", "set_workflow", steps=[{"type": "hold", "seconds": 60}, {"type": "read"}])], "tool_use"),
        ("OK.", [], "end_turn"),
    ])
    _, session = run_turn(settings, model)
    no_settings = model.calls[3][-1]["content"][0]
    assert no_settings["is_error"] and "Step 2 (read) needs its settings" in no_settings["content"]
    first = model.calls[1][-1]["content"][0]
    assert first["is_error"] and "protocol 'nope', which doesn't exist" in first["content"]
    second = model.calls[2][-1]["content"][0]
    assert second["is_error"] and "cycles" in second["content"]
    assert session.design.workflow == []


def test_analysis_must_match_what_the_workflow_reads(settings):
    standards = [{"name": f"S{c}", "wells": [f"A{c}", f"B{c}"], "x": 2000 / 2 ** (c - 1)} for c in range(1, 8)]
    bradford = {"title": "BSA standard curve", "kind": "standard_curve", "measurement": "Absorbance 595 nm", "fit": "linear",
                "x_label": "BSA", "x_unit": "ug/mL", "blank_wells": ["A8", "B8"], "standards": standards,
                "samples": [{"name": "Lysate 1", "wells": ["C1", "D1"], "dilution": 5}]}
    model = FakeModel([
        ("", [tool_use("t1", "set_analysis", analyses=[bradford])], "tool_use"),
        ("", [tool_use("t2", "set_reader_settings", mode="absorbance", wavelengths_nm=[600], wells=["A1:D8"])], "tool_use"),
        ("", [tool_use("t3", "set_analysis", analyses=[bradford])], "tool_use"),
        ("", [tool_use("t4", "set_analysis", analyses=[{**bradford, "measurement": "Absorbance 600 nm",
                                                        "samples": [{"name": "Lysate 1", "wells": ["E1"]}]}])], "tool_use"),
        ("", [tool_use("t5", "set_analysis", analyses=[{**bradford, "measurement": "Absorbance 600 nm"}])], "tool_use"),
        ("Planned.", [], "end_turn"),
    ])
    events, session = run_turn(settings, model)
    results = [model.calls[i][-1]["content"][0] for i in (1, 3, 4, 5)]
    assert results[0]["is_error"] and "no read or qPCR step yet" in results[0]["content"]
    assert results[1]["is_error"] and "no step produces 'Absorbance 595 nm'" in results[1]["content"]
    assert "step 1: Absorbance 600 nm" in results[1]["content"]
    assert results[2]["is_error"] and "wells E1 are not read in step 1" in results[2]["content"]
    assert not results[3].get("is_error") and results[3]["content"].startswith("Analysis saved: BSA standard curve (standard curve, linear fit; 7 standards, 1 samples)")
    assert session.design.analyses[0].samples[0].dilution == 5

    # The next message tells the model what it planned.
    follow_up = FakeModel([("OK.", [], "end_turn")])
    run_turn(settings, follow_up, session=session, text="thanks")
    assert "Result analysis:\n- BSA standard curve (standard curve, linear fit; 7 standards, 1 samples):" in follow_up.calls[0][-1]["content"][0]["text"]


def test_knowledge_search_and_read(settings, tmp_path):
    manuals = tmp_path / "knowledge" / "manuals"
    manuals.mkdir(parents=True)
    (manuals / "il6-kit.md").write_text(
        "# Human IL-6 ELISA kit\n\nIncubate samples 2 hours at room temperature.\n\nRead at 450 nm with 570 nm correction."
    )
    model = FakeModel([
        ("", [tool_use("t1", "search_knowledge", query="IL-6 ELISA read wavelength")], "tool_use"),
        ("", [tool_use("t2", "read_knowledge", doc="manuals/il6-kit.md"), tool_use("t3", "read_knowledge", doc="manuals/none.md")], "tool_use"),
        ("Found it.", [], "end_turn"),
    ])
    run_turn(settings, model, knowledge=Knowledge(settings.knowledge.folder))
    found = model.calls[1][-1]["content"][0]["content"]
    assert 'doc="manuals/il6-kit.md"' in found and "450 nm" in found
    read, missing = model.calls[2][-1]["content"]
    assert "Incubate samples 2 hours" in read["content"]
    assert missing["is_error"] and "manuals/il6-kit.md" in missing["content"]
    assert "Knowledge documents: manuals/il6-kit.md (Kit manual: Human IL-6 ELISA kit)" in model.calls[0][0]["content"][0]["text"]


def test_plate_slot_must_be_one_thor_can_reach(settings):
    model = FakeModel([
        ("", [tool_use("t1", "write_protocol", title="x", summary="x", plate_slot="A1", code=GOOD_PROTOCOL)], "tool_use"),
        ("", [tool_use("t2", "write_protocol", title="x", summary="x", plate_slot="D2", code=GOOD_PROTOCOL.replace('"D2"', '"B2"'))], "tool_use"),
        ("Fixed.", [], "end_turn"),
    ])
    events, session = run_turn(settings, model)
    first = model.calls[1][-1]["content"][0]
    assert first["is_error"] is True and "B1, B2, B3, C1, C2, C3, D1, D2" in first["content"]
    second = model.calls[2][-1]["content"][0]["content"]
    assert "Simulation FAILED" in second and "Nothing is loaded in D2" in second
    protocol = session.design.protocol("main")
    assert protocol.plate_slot == "D2" and not protocol.simulation.ok


def test_invalid_reader_settings_go_back_to_claude_as_errors(settings):
    model = FakeModel([
        ("", [tool_use("t1", "set_reader_settings", mode="fluorescence", excitation_nm=535, emission_nm=485, wells=["A1"])], "tool_use"),
        ("Sorry, fixing that.", [], "end_turn"),
    ])
    events, session = run_turn(settings, model)
    result = model.calls[1][-1]["content"][0]
    assert result["is_error"] is True
    assert "longer than excitation" in result["content"]
    assert session.design.workflow == []  # unchanged


def test_truncated_tool_input_is_not_run(settings):
    model = FakeModel([
        ("", [tool_use("t1", "write_protocol", title="x", summary="x", code="def run(")], "max_tokens"),
        ("Let me try again.", [], "end_turn"),
    ])
    events, session = run_turn(settings, model)
    assert session.design.protocols == []
    assert model.calls[1][-1]["content"][0]["is_error"] is True
    check_history(session.messages)


def test_refusal_is_reported_and_history_stays_valid(settings):
    model = FakeModel([("", [tool_use("t1", "write_protocol", title="x", summary="x", code="")], "refusal")])
    events, session = run_turn(settings, model)
    assert events[-2] == {"type": "error", "message": "Claude declined to continue with this request."}
    check_history(session.messages)


def test_scientist_edits_reach_claude_on_the_next_turn(settings):
    model = FakeModel([
        ("", [tool_use("t1", "set_reader_settings", mode="absorbance", wavelengths_nm=[600], wells=["A1"])], "tool_use"),
        ("OK.", [], "end_turn"),
        ("Noted.", [], "end_turn"),
    ])
    _, session = run_turn(settings, model)
    session.design.read_steps()[0].settings.wavelengths_nm = [450]
    run_turn(settings, model, session=session, text="I changed the wavelength")
    assert '"wavelengths_nm": [450]' in model.calls[2][-1]["content"][0]["text"]


def test_the_assistant_is_told_which_tip_racks_are_on_the_deck(settings):
    async def racks():
        return [{"slot": "C1", "load_name": "opentrons_flex_96_tiprack_200ul", "remaining": 56}]

    model = FakeModel([("OK.", [], "end_turn")])
    run_turn(settings, model, tip_racks=racks)
    state = model.calls[0][0]["content"][0]["text"]
    assert "Tip racks on the Flex now: C1 opentrons_flex_96_tiprack_200ul (56 of 96 tips left)" in state
    assert "Lab instruments (registry):\nunknown (the master node can't be reached)" in state


def test_sessions_saved_by_the_older_version_still_load():
    old = {
        "id": "abc", "design": {"title": "Old", "summary": "s", "protocol_code": GOOD_PROTOCOL, "version": 3, "plate_slot": "D2",
                                "reader": {"mode": "absorbance", "wavelengths_nm": [600], "wells": ["A1"]}},
        "simulation": None, "transcript": [], "messages": [],
    }
    session = Session.model_validate(old)
    protocol = session.design.protocol("main")
    assert (protocol.code, protocol.version, protocol.plate_slot) == (GOOD_PROTOCOL, 3, "D2")
    assert [s.type for s in session.design.workflow] == ["liquid_handling", "read"]
    assert session.design.read_steps()[0].settings.wells == ["A1"]
    assert session.design.send_problems() == [
        "Protocol 'Old' v3 has changed since its last simulation. Run the simulation."
    ]
