"""Designs simulated from the tips left on the deck, as the run will use them (real simulator)."""

import asyncio
import json

import httpx
from fastapi.testclient import TestClient

from protocol_tool.main import create_app
from protocol_tool.master import MasterClient
from protocol_tool.simulator import DeckTips, simulate

from .conftest import GOOD_PROTOCOL, FakeMaster, FakeModel, continue_tips, tool_use
from .test_api import SEND, read_events

TIPS_200 = "opentrons_flex_96_tiprack_200ul"
WRITE = [tool_use("t1", "write_protocol", title="Dilution series", summary="Two-fold series", plate_slot="D2", code=GOOD_PROTOCOL)]


def make(settings, turns, used=None):
    master = FakeMaster()
    master.tips_used = dict(used or {})
    master.tip_load_names = {"C1": TIPS_200} if used else {}
    model = FakeModel(list(turns))
    app = create_app(settings, model=model, master=MasterClient(settings.master, transport=httpx.MockTransport(master)))
    return TestClient(app), master, model


def design(client) -> tuple[str, dict]:
    sid = client.post("/api/sessions").json()["id"]
    done = read_events(client.post(f"/api/sessions/{sid}/chat", data={"message": "Two-fold dilution"}))[-1]
    return sid, done["session"]["design"]["protocols"][0]["simulation"]


def test_the_design_starts_from_the_tips_left_on_the_deck(settings):
    client, master, model = make(settings, [("", WRITE, "tool_use"), ("Done.", [], "end_turn")], used={"C1": 4})
    with client:
        _, sim = design(client)
        assert sim["ok"] and sim["tip_state"] == {"C1": 4}
        assert sim["tip_racks"] == [{"slot": "C1", "load_name": TIPS_200, "pipette": "p1000_single_flex", "before": 4, "after": 94}]
        told = model.calls[1][-1]["content"][0]["content"]
        assert "Tips, starting from what is left on the deck:" in told
        assert "90 tips from C1 (opentrons_flex_96_tiprack_200ul): 92 of 96 left before, 2 after" in told


def test_a_deck_without_enough_tips_fails_the_simulation(settings):
    client, _, model = make(settings, [("", WRITE, "tool_use"), ("Too few tips.", [], "end_turn")], used={"C1": 40})
    with client:
        _, sim = design(client)
        assert not sim["ok"]
        assert any("ran out of tips" in e and "C1 opentrons_flex_96_tiprack_200ul had 56 of 96 left" in e for e in sim["errors"])
        assert "load another rack of the same type in a free slot" in model.calls[1][-1]["content"][0]["content"]


def test_review_checks_the_tips_again_when_another_run_took_some(settings):
    client, master, _ = make(settings, [("", WRITE, "tool_use"), ("Done.", [], "end_turn")])
    with client:
        sid, sim = design(client)
        assert sim["ok"] and sim["tip_state"] == {}

        master.tips_used, master.tip_load_names = {"C1": 40}, {"C1": TIPS_200}  # another run took 40 tips
        refused = client.post(f"/api/sessions/{sid}/plan", json=SEND)
        assert refused.status_code == 409 and "failed simulation" in refused.json()["detail"]

        master.tips_used = {"C1": 4}  # staff put in a rack with 92 left
        plan = client.post(f"/api/sessions/{sid}/plan", json=SEND)
        assert plan.status_code == 200, plan.text
        assert plan.json()["tips"] == [{"protocol": "Dilution series", "slot": "C1", "load_name": TIPS_200,
                                        "pipette": "p1000_single_flex", "before": 4, "after": 94}]
        # Nothing changed since: the review doesn't simulate again.
        before = client.get(f"/api/sessions/{sid}").json()["design"]["protocols"][0]["simulation"]["ran_at"]
        client.post(f"/api/sessions/{sid}/plan", json=SEND)
        assert client.get(f"/api/sessions/{sid}").json()["design"]["protocols"][0]["simulation"]["ran_at"] == before


def test_a_later_protocol_starts_where_the_earlier_ones_leave_the_racks(settings):
    second = GOOD_PROTOCOL.replace("Two-fold dilution series", "Second series")
    turns = [
        ("", [tool_use("t1", "write_protocol", protocol_id="first", title="First", summary="s", plate_slot="D2", code=GOOD_PROTOCOL),
              tool_use("t2", "write_protocol", protocol_id="second", title="Second", summary="s", plate_slot="D2", code=second),
              tool_use("t3", "set_workflow", steps=[{"type": "liquid_handling", "protocol_id": "first"},
                                                    {"type": "liquid_handling", "protocol_id": "second"}])], "tool_use"),
        ("Done.", [], "end_turn"),
    ]
    client, master, _ = make(settings, turns)
    with client:
        sid, _ = design(client)
        master.continued.clear()
        client.post(f"/api/sessions/{sid}/simulate")
        first, later = master.continued
        assert first["after"] == []
        assert later["after"] == [[{"pipette": "p1000_single_flex", "channels": 1, "pickups": 90,
                                    "racks": [{"slot": "C1", "load_name": TIPS_200}]}]]


def test_a_tip_rack_the_protocol_never_uses_is_flagged():
    code = GOOD_PROTOCOL.replace(
        '    protocol.load_trash_bin("A3")',
        '    protocol.load_trash_bin("A3")\n    protocol.load_labware("opentrons_flex_96_tiprack_200ul", "B3")')
    deck = DeckTips(continue_tips(code, {"C1": 4}), {"C1": 4}, {"C1": TIPS_200})
    sim = asyncio.run(simulate(code, "D2", deck_tips=deck))
    assert "The tip rack in B3 is never used (the racks on the deck before it have enough tips)." in " ".join(sim.warnings)
    json.dumps(sim.model_dump(mode="json"))  # saved with the session


TIPS_50 = "opentrons_flex_96_tiprack_50ul"
TWO_RACKS = f'''
requirements = {{"robotType": "Flex", "apiLevel": "{GOOD_PROTOCOL.split('"apiLevel": "')[1].split('"')[0]}"}}


def run(protocol):
    protocol.load_trash_bin("A3")
    racks = [protocol.load_labware("{TIPS_50}", "C1"), protocol.load_labware("{TIPS_50}", "B1")]
    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", "D2")
    pipette = protocol.load_instrument("flex_8channel_50", "right", tip_racks=racks)
    for _ in range(14):
        pipette.pick_up_tip()
        pipette.aspirate(10, plate["A1"])
        pipette.dispense(10, plate["A2"])
        pipette.drop_tip()
'''


def test_running_on_into_a_rack_an_earlier_protocol_emptied_fails_the_simulation():
    # As in a four-step ELISA: step 1 took all of B1 and two columns of C1. This step starts at C1's
    # third column, but after C1 the Flex continues into B1 from A1: tips that are already gone.
    used = {"B1": 96, "C1": 16}
    deck = DeckTips(continue_tips(TWO_RACKS, used), used, {"B1": TIPS_50, "C1": TIPS_50})
    sim = asyncio.run(simulate(TWO_RACKS, "D2", deck_tips=deck))
    assert not sim.ok
    assert any("Not enough tips on the deck" in e and "runs on into B1" in e and "that rack is empty" in e for e in sim.errors)


def test_a_full_second_rack_is_fine():
    used = {"C1": 16}
    deck = DeckTips(continue_tips(TWO_RACKS, used), used, {"B1": TIPS_50, "C1": TIPS_50})
    sim = asyncio.run(simulate(TWO_RACKS, "D2", deck_tips=deck))
    assert not any("Not enough tips" in e for e in sim.errors), sim.errors
