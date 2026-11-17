"""Protocols the scientist gives to run as they are."""

import asyncio

import pytest

from protocol_tool.assistant import Assistant, Attachment
from protocol_tool.given import SlotMoveError, move_slots, slots_used
from protocol_tool.models import Session
from protocol_tool.store import SessionStore

from .conftest import LATEST, FakeModel, tool_use

# As Protocol Designer writes them: keyword slots, its own tip racks in A1 and A2 (which this lab keeps
# empty), an older API level, and 100 uL dispensed 1 mm above the bottom (the lab's rule says 12 mm).
GIVEN = '''metadata = {"protocolName": "Given µL protocol"}
requirements = {"robotType": "Flex", "apiLevel": "2.27"}

def run(protocol) -> None:
    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", location="C1")
    res = protocol.load_labware("nest_12_reservoir_15ml", 'B2')
    tips = protocol.load_labware(
        "opentrons_flex_96_tiprack_1000ul",
        location="A1",
    )
    pipette = protocol.load_instrument("flex_1channel_1000", "left", tip_racks=[tips])
    protocol.load_waste_chute()
    pipette.pick_up_tip()
    pipette.aspirate(100, res["A1"])
    pipette.dispense(100, plate["A1"].bottom(z=1))  # "A1" here is a well, not a slot
    pipette.drop_tip()
'''


def test_only_the_slots_change():
    moved = move_slots(GIVEN, {"a1": "C3", "B2": "D2"})
    assert moved == GIVEN.replace('location="A1"', 'location="C3"').replace("'B2'", "'D2'")
    assert slots_used(moved) == {"C1", "C3", "D2"}
    with pytest.raises(SlotMoveError, match="puts nothing in D1"):
        move_slots(GIVEN, {"D1": "C3"})
    with pytest.raises(SlotMoveError, match="C1 already hold labware"):
        move_slots(GIVEN, {"A1": "C1"})


def test_the_assistant_runs_a_given_protocol_as_it_is(settings):
    model = FakeModel([
        ("", [tool_use("t1", "use_given_protocol", protocol_id="main", file="curve.py", title="Curve",
                       summary="As given.", plate_slot="C1", slot_moves={"A1": "C3"})], "tool_use"),
        ("Done.", [], "end_turn"),
    ])
    store = SessionStore(settings.server.database)
    session = Session()
    store.save(session)
    assistant = Assistant(settings, store, model)

    async def go():
        return [e async for e in assistant.chat(session, "Run this", [Attachment("curve.py", "text/x-python", GIVEN.encode())])]

    asyncio.run(go())
    protocol = session.design.protocol("main")
    assert protocol.code == GIVEN.replace('location="A1"', 'location="C3"')
    assert (protocol.given, protocol.slot_moves) == ("curve.py", {"A1": "C3"})
    sim = protocol.simulation
    assert sim.ok, sim.errors  # the lab's own rules are only warnings for it
    assert any("API level" in w for w in sim.warnings) and any("12 mm above the bottom" in w for w in sim.warnings)
    result = model.calls[1][-1]["content"][0]["content"]
    assert "Used curve.py as given, with labware moved A1 -> C3" in str(result)

    # Unmoved, A1 must stay empty for Thor: that still fails, so the move is needed.
    unmoved = asyncio.run(assistant._simulate_with_full_racks(session.design, protocol.model_copy(update={"code": GIVEN})))
    assert not unmoved.ok and any("A1 must stay empty" in e for e in unmoved.errors)
    assert f'"apiLevel": "{LATEST}"' not in protocol.code
