"""Runs the real Opentrons simulator (about 1.5 s per protocol)."""

import asyncio

from protocol_tool.config import LabSettings
from protocol_tool.simulator import DeckRules, latest_api_level, simulate

from .conftest import BROKEN_PROTOCOL, GOOD_PROTOCOL

LAB = DeckRules.from_settings(LabSettings())  # the lab's defaults: latest API level, Flex labware list


def test_the_lab_uses_the_newest_api_level():
    assert LAB.api_level == latest_api_level() == "2.29"
    assert f'"apiLevel": "{LAB.api_level}"' in GOOD_PROTOCOL
    assert asyncio.run(simulate(GOOD_PROTOCOL, "D2", rules=LAB)).ok


def test_older_api_levels_fail():
    sim = asyncio.run(simulate(GOOD_PROTOCOL.replace('"2.29"', '"2.20"'), "D2", rules=LAB))
    assert not sim.ok
    assert any("Use the lab's Opentrons API level 2.29 (this protocol uses 2.20)" in e for e in sim.errors)


def test_ot2_tip_racks_and_unlisted_labware_fail():
    code = GOOD_PROTOCOL.replace("opentrons_flex_96_tiprack_200ul", "opentrons_96_tiprack_300ul") \
                        .replace("nest_12_reservoir_15ml", "usascientific_12_reservoir_22ml")
    sim = asyncio.run(simulate(code, "D2", rules=LAB))
    assert not sim.ok
    assert any(e.startswith("opentrons_96_tiprack_300ul is an OT-2 tip rack. Use Flex tips") for e in sim.errors)
    assert any(e.startswith("usascientific_12_reservoir_22ml is not on the lab's Flex labware list") for e in sim.errors)


def test_ot2_tip_racks_fail_even_without_a_labware_list():
    code = GOOD_PROTOCOL.replace("opentrons_flex_96_tiprack_200ul", "opentrons_96_tiprack_300ul")
    sim = asyncio.run(simulate(code, "D2"))
    assert not sim.ok and any("OT-2 tip rack" in e for e in sim.errors)


def test_a1_and_a2_stay_empty():
    # A reservoir in A2 and the tip rack in A1.
    code = GOOD_PROTOCOL.replace('"C2"', '"A2"').replace('"C1"', '"A1"')
    sim = asyncio.run(simulate(code, "D2", rules=LAB))
    assert not sim.ok
    assert any(e.startswith("A2 must stay empty") and "NEST 12 Well Reservoir" in e for e in sim.errors)
    assert any(e.startswith("A1 must stay empty") for e in sim.errors)


def test_no_thermocycler_or_moves_into_a1_a2():
    thermocycler = GOOD_PROTOCOL.replace(
        'protocol.load_trash_bin("A3")', 'protocol.load_trash_bin("A3")\n    protocol.load_module("thermocyclerModuleV2")')
    sim = asyncio.run(simulate(thermocycler, "D2", rules=LAB))
    assert not sim.ok and any("Thermocycler covers A1" in e for e in sim.errors)

    moved = GOOD_PROTOCOL.replace(
        '    volume = protocol.params.volume_ul',
        '    protocol.move_labware(reservoir, "A1", use_gripper=True)\n    volume = protocol.params.volume_ul')
    sim = asyncio.run(simulate(moved, "D2", rules=LAB))
    assert not sim.ok and any("moves labware into A1" in e for e in sim.errors)


def test_only_the_pipettes_on_the_robot_and_no_gripper():
    wrong = GOOD_PROTOCOL.replace('"flex_1channel_1000", "left"', '"flex_8channel_1000", "right"')
    sim = asyncio.run(simulate(wrong, "D2", rules=LAB))
    assert not sim.ok
    assert any("The right mount holds flex_8channel_50, not flex_8channel_1000" in e for e in sim.errors)

    gripper = GOOD_PROTOCOL.replace(
        '    volume = protocol.params.volume_ul',
        '    protocol.move_labware(reservoir, "B2", use_gripper=True)\n    volume = protocol.params.volume_ul')
    sim = asyncio.run(simulate(gripper, "D2", rules=LAB))
    assert not sim.ok and any("has no gripper" in e for e in sim.errors)


def test_the_assistant_is_told_the_rules():
    assert 'protocol.load_instrument("flex_8channel_50", "right")' in LAB.describe()
    assert "There is no gripper" in LAB.describe()
    assert "A1 and A2 must stay empty for the whole run" in LAB.describe()
    assert "Lab staff can set up other labware in B1, B2, B3, C1, C2, C3, D1, D2" in LAB.describe()
    text = LAB.describe()
    assert "Never OT-2 labware" in text and "Labware you may load" in text and "opentrons_flex_*" in text


def test_good_protocol_reports_its_deck():
    sim = asyncio.run(simulate(GOOD_PROTOCOL, "D2"))

    assert sim.ok, sim.errors
    assert sim.warnings == []
    slots = sim.deck.slots
    assert slots["A3"].trash
    assert slots["C1"].labware[0].category == "tipRack"
    plate = sim.deck.plate_at("D2")
    assert plate.load_name == "corning_96_wellplate_360ul_flat"
    assert (plate.rows, plate.cols) == (8, 12)
    assert [p.display_name for p in sim.deck.pipettes] == ["Flex 1-Channel 1000 µL"]
    assert sim.stats["tips"] > 0
    assert sim.steps[0].startswith("Pick up tip from A1 of")


def test_broken_protocol_reports_the_error_line_and_partial_deck():
    sim = asyncio.run(simulate(BROKEN_PROTOCOL, "D2"))

    assert not sim.ok
    assert "no_such_reservoir" in sim.errors[0]
    assert sim.errors[0].startswith("[line ")
    assert "C1" in sim.deck.slots  # labware loaded before the error still shows


def test_empty_thor_slot_fails():
    sim = asyncio.run(simulate(GOOD_PROTOCOL, "B2"))
    assert not sim.ok
    assert any("Nothing is loaded in B2" in e for e in sim.errors)


def test_thor_cannot_reach_a1_or_a2():
    code = GOOD_PROTOCOL.replace('"D2"', '"A1"')
    sim = asyncio.run(simulate(code, "A1"))
    assert not sim.ok
    assert any("Thor cannot place the sample plate in A1" in e for e in sim.errors)


def test_fixture_slots_must_stay_free():
    # Labware in the waste chute slot, and a trash bin moved away from A3.
    code = GOOD_PROTOCOL.replace('"C2"', '"D3"').replace('load_trash_bin("A3")', 'load_trash_bin("C3")')
    sim = asyncio.run(simulate(code, "D2"))
    assert not sim.ok
    assert any(e.startswith("D3 holds the waste chute") for e in sim.errors)
    assert any('use protocol.load_trash_bin("A3"), not C3' in e for e in sim.errors)


def test_the_deck_shows_both_fixtures_when_loaded():
    code = GOOD_PROTOCOL.replace('protocol.load_trash_bin("A3")', 'protocol.load_trash_bin("A3")\n    protocol.load_waste_chute()')
    sim = asyncio.run(simulate(code, "D2"))
    assert sim.ok, sim.errors
    assert sim.deck.slots["A3"].trash and sim.deck.slots["D3"].waste_chute


def test_syntax_error_is_reported_not_raised():
    sim = asyncio.run(simulate("def run(protocol)\n    pass\n", "D2"))
    assert not sim.ok
    assert sim.errors


def test_tip_use_per_pipette():
    sim = asyncio.run(simulate(GOOD_PROTOCOL, "D2"))
    assert sim.tips == [{
        "pipette": "p1000_single_flex", "channels": 1, "pickups": 90,
        "racks": [{"slot": "C1", "load_name": "opentrons_flex_96_tiprack_200ul"}],
    }]


def test_a_tip_rack_shared_by_two_pipettes_fails():
    code = GOOD_PROTOCOL.replace(
        '    volume = protocol.params.volume_ul',
        '    multi = protocol.load_instrument("flex_8channel_1000", "right", tip_racks=[tips])\n'
        '    multi.pick_up_tip()\n    multi.drop_tip()\n'
        '    volume = protocol.params.volume_ul',
    )
    sim = asyncio.run(simulate(code, "D2"))
    assert not sim.ok
    assert any("used by two pipettes" in e for e in sim.errors)
