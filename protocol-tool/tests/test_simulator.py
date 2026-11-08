"""Runs the real Opentrons simulator (about 1.5 s per protocol)."""

import asyncio

from protocol_tool.config import LabSettings
from protocol_tool.simulator import DeckRules, latest_api_level, simulate

from .conftest import BROKEN_PROTOCOL, GOOD_PROTOCOL, LATEST

LAB = DeckRules.from_settings(LabSettings())  # the lab's defaults: latest API level, any Opentrons labware


def test_the_lab_uses_the_newest_api_level():
    assert LAB.api_level == latest_api_level() == LATEST
    assert f'"apiLevel": "{LAB.api_level}"' in GOOD_PROTOCOL
    assert asyncio.run(simulate(GOOD_PROTOCOL, "D2", rules=LAB)).ok


def test_older_api_levels_fail():
    sim = asyncio.run(simulate(GOOD_PROTOCOL.replace(f'"{LATEST}"', '"2.20"'), "D2", rules=LAB))
    assert not sim.ok
    assert any(f"Use the lab's Opentrons API level {LATEST} (this protocol uses 2.20)" in e for e in sim.errors)


def test_other_tip_racks_and_unlisted_labware_fail():
    code = GOOD_PROTOCOL.replace("opentrons_flex_96_tiprack_200ul", "opentrons_96_tiprack_300ul") \
                        .replace("nest_12_reservoir_15ml", "usascientific_12_reservoir_22ml")
    listed = DeckRules(labware=("opentrons_flex_*", "corning_96_wellplate_360ul_flat", "nest_12_reservoir_15ml"))
    sim = asyncio.run(simulate(code, "D2", rules=listed))
    assert not sim.ok
    assert any(e.startswith("opentrons_96_tiprack_300ul is not a Flex tip rack. Use Flex tips") for e in sim.errors)
    assert any(e.startswith("usascientific_12_reservoir_22ml is not on the lab's Flex labware list") for e in sim.errors)


def test_any_opentrons_labware_by_default():
    code = GOOD_PROTOCOL.replace("nest_12_reservoir_15ml", "usascientific_12_reservoir_22ml")
    sim = asyncio.run(simulate(code, "D2", rules=LAB))
    assert not any("labware list" in e for e in sim.errors), sim.errors


def test_other_tip_racks_fail_even_without_a_labware_list():
    code = GOOD_PROTOCOL.replace("opentrons_flex_96_tiprack_200ul", "opentrons_96_tiprack_300ul")
    sim = asyncio.run(simulate(code, "D2"))
    assert not sim.ok and any("is not a Flex tip rack" in e for e in sim.errors)


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
    assert "OT-2" not in text and "never numbered slots" in text and "any standard Opentrons labware" in text and "reference/opentrons-labware/" in text
    listed = DeckRules(labware=("opentrons_flex_*",)).describe()
    assert "Labware you may load (anything else fails the check): opentrons_flex_*" in listed


def test_good_protocol_reports_its_deck():
    sim = asyncio.run(simulate(GOOD_PROTOCOL, "D2"))

    assert sim.ok, sim.errors
    # Its only shortcoming: it drops tips in the trash bin, not the lab's waste chute.
    assert [w.split(";")[0] for w in sim.warnings] == ["Tips are dropped in the trash bin in A3"]
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


def test_used_tips_go_in_the_waste_chute():
    rules = DeckRules()
    assert "Used tips go in the waste chute (D3, fixed)" in rules.describe()
    assert "load_waste_chute()" in rules.describe()

    binned = asyncio.run(simulate(GOOD_PROTOCOL, "D2", rules=rules))  # its tips go to the trash bin in A3
    assert any("Tips are dropped in the trash bin in A3; this lab drops tips in the waste chute (D3)" in w
               for w in binned.warnings)

    chute = GOOD_PROTOCOL.replace('protocol.load_trash_bin("A3")', "protocol.load_waste_chute()")
    sim = asyncio.run(simulate(chute, "D2", rules=rules))
    assert sim.ok, sim.errors
    assert not any("trash bin" in w for w in sim.warnings)
    assert sim.deck.slots["D3"].waste_chute and "A3" not in sim.deck.slots


FALCON = '''requirements = {"robotType": "Flex", "apiLevel": "%s"}
def run(protocol):
    protocol.load_waste_chute()
    tips = protocol.load_labware("opentrons_flex_96_tiprack_50ul", "B2")
    rack = protocol.load_labware("opentrons_6_tuberack_falcon_50ml_conical", "C2")
    plate = protocol.load_labware("nest_96_wellplate_200ul_flat", "D1")
    pipette = protocol.load_instrument("flex_1channel_1000", "left", tip_racks=[tips])
    tube = rack["A1"]
    pipette.pick_up_tip()
SOURCE
    pipette.dispense(30, plate["A1"])
    pipette.drop_tip()
''' % LATEST


def falcon(source: str):
    return asyncio.run(simulate(FALCON.replace("SOURCE", source), "D1"))


def test_tubes_are_aspirated_from_just_below_the_surface():
    for fixed in ("    pipette.aspirate(30, tube)", "    pipette.aspirate(30, tube.bottom(40))",
                  "    pipette.transfer(30, tube, plate['A2'], new_tip='never')"):
        sim = falcon(fixed)
        assert not sim.ok
        assert any(e.startswith("The protocol aspirates from A1 of Opentrons 6 Tube Rack with Falcon 50 mL Conical "
                                "(a 113 mm deep tube) at a fixed height") for e in sim.errors), (fixed, sim.errors)

    known = falcon('    tube.load_liquid(protocol.define_liquid("Buffer"), 40000)\n'
                   '    pipette.aspirate(30, tube.meniscus(z=-2, target="end"))')
    assert known.ok, known.errors
    # A measured level isn't known before the run, so the tip can't be shown to stay within reach.
    sensed = falcon('    pipette.measure_liquid_height(tube)\n    pipette.aspirate(30, tube.meniscus(z=-2, target="end"))')
    assert any("The level after measure_liquid_height isn't known before the run" in e for e in sensed.errors), sensed.errors
    assert not falcon('    pipette.aspirate(30, tube.meniscus(z=-2, target="end"))').ok  # no volume, no measurement
    assert "measure_liquid_height" in DeckRules().describe()


def test_the_deck_shows_where_liquid_goes():
    sim = falcon('    tube.load_liquid(protocol.define_liquid("Buffer"), 40000)\n'
                 '    pipette.aspirate(30, tube.meniscus(z=-2, target="end"))')
    rack, plate = sim.deck.slots["C2"].labware[-1], sim.deck.plate_at("D1")
    buffer = sim.deck.liquids[0].id
    assert rack.aspirated == {"A1": 30} and plate.dispensed == {"A1": 30}
    assert plate.received == {"A1": [buffer]}  # the tip carries the tube's liquid into the plate

    multi = GOOD_PROTOCOL.replace('"flex_1channel_1000", "left"', '"flex_8channel_50", "right"') \
                         .replace("opentrons_flex_96_tiprack_200ul", "opentrons_flex_96_tiprack_50ul")
    multi = multi[:multi.index("    volume = protocol.params.volume_ul")] + \
        '    pipette.transfer(20, reservoir["A1"], plate["A2"])\n'
    sim = asyncio.run(simulate(multi, "D2"))
    assert sim.ok, sim.errors
    assert sorted(sim.deck.plate_at("D2").dispensed) == [f"{r}2" for r in "ABCDEFGH"]  # one column, 8 channels
    assert sim.deck.slots["C2"].labware[-1].aspirated == {"A1": 160}  # all 8 channels take from the one trough


def test_a_deck_without_a_waste_chute_keeps_the_trash_bin():
    rules = DeckRules(waste_chute_slot="")
    assert 'load it with `protocol.load_trash_bin("A3")`' in rules.describe()
    assert not any("waste chute" in w for w in asyncio.run(simulate(GOOD_PROTOCOL, "D2", rules=rules)).warnings)


HEAD = '''requirements = {"robotType": "Flex", "apiLevel": "%s"}
from opentrons.protocol_api import SINGLE, PARTIAL_COLUMN
def run(protocol):
    protocol.load_waste_chute()
    tips = protocol.load_labware("opentrons_flex_96_tiprack_50ul", "B2")
    source = protocol.load_labware("LABWARE", "C2")
    plate = protocol.load_labware("nest_96_wellplate_200ul_flat", "D1")
    pipette = protocol.load_instrument("flex_8channel_50", "left", tip_racks=[tips])
LAYOUT
    pipette.pick_up_tip()
    source["WELL"].load_liquid(protocol.define_liquid("Buffer"), 1000)
    pipette.aspirate(10, source["WELL"].meniscus(z=-2, target="end"))
    pipette.dispense(10, plate["DEST"])
    pipette.drop_tip()
''' % LATEST


def eight_channel(labware: str, well: str = "A1", layout: str = ""):
    dest = "H1" if layout else "A1"  # a partial layout's primary nozzle is H1, with the others behind it
    code = HEAD.replace("LABWARE", labware).replace("WELL", well).replace("LAYOUT", layout).replace("DEST", dest)
    return asyncio.run(simulate(code, "D1"))


def test_a_multichannel_pipette_only_goes_into_plates_and_reservoirs():
    for tubes, front in (("opentrons_6_tuberack_falcon_50ml_conical", "B1"),
                         ("opentrons_15_tuberack_falcon_15ml_conical", "C1"),
                         ("opentrons_24_tuberack_nest_1.5ml_snapcap", "D1")):
        sim = eight_channel(tubes)
        assert not sim.ok
        assert any(e.startswith("The flex_8channel_50 goes into A1 of ") and "only one fits there" in e
                   for e in sim.errors), (tubes, sim.errors)
        # Four nozzles still don't fit a tube.
        partial = eight_channel(tubes, front,
                                '    pipette.configure_nozzle_layout(style=PARTIAL_COLUMN, start="H1", end="E1", tip_racks=[tips])')
        assert [e for e in partial.errors if "only one fits there" in e and "Tube Rack" in e], (tubes, partial.errors)

    single = eight_channel("opentrons_6_tuberack_falcon_50ml_conical",
                           layout='    pipette.configure_nozzle_layout(style=SINGLE, start="H1", tip_racks=[tips])')
    assert not any("only one fits there" in e for e in single.errors), single.errors

    for fits in ("nest_12_reservoir_15ml", "nest_1_reservoir_195ml", "nest_96_wellplate_2ml_deep",
                 "corning_384_wellplate_112ul_flat", "opentrons_96_wellplate_200ul_pcr_full_skirt"):
        sim = eight_channel(fits)
        assert sim.ok, (fits, sim.errors)
    # A column that starts below row A runs off the plate.
    assert any("only one fits there" in e for e in eight_channel("nest_96_wellplate_2ml_deep", "B1").errors)
    assert "Multi-channel pipettes (8- and 96-channel) only go into" in DeckRules().describe()


REACH = '''requirements = {"robotType": "Flex", "apiLevel": "%s"}
from opentrons.protocol_api import SINGLE
def run(protocol):
    protocol.load_waste_chute()
    tips = protocol.load_labware("opentrons_flex_96_tiprack_TIPSul", "C3")  # nothing behind it: a partial layout's other nozzles hang there
    rack = protocol.load_labware("opentrons_6_tuberack_falcon_50ml_conical", "C2")
    plate = protocol.load_labware("nest_96_wellplate_200ul_flat", "D1")
    pipette = protocol.load_instrument("PIPETTE", "left", tip_racks=[tips])
LAYOUT
    rack["A1"].load_liquid(protocol.define_liquid("Buffer"), VOLUME)
    pipette.pick_up_tip()
    pipette.aspirate(30, rack["A1"].meniscus(z=-2, target="end"))
    pipette.dispense(30, DEST)
    pipette.drop_tip()
''' % LATEST


def reach(tips: int, volume: int, dest: str = 'plate["H1"]', pipette: str = "flex_1channel_1000", layout: str = ""):
    code = (REACH.replace("TIPS", str(tips)).replace("VOLUME", str(volume)).replace("DEST", dest)
            .replace("PIPETTE", pipette).replace("LAYOUT", layout))
    return asyncio.run(simulate(code, "D1"))


def test_tips_reach_only_their_length_into_a_tube():
    assert reach(50, 40000).ok  # a full 50 mL tube: the surface is near the top
    low = reach(50, 5000)  # 5 mL: the surface is about 98 mm down, a 50 uL tip reaches 48 mm
    assert any(e.startswith("The flex_1channel_1000 aspirates ") and "50 µL tips reach only 48 mm, so the pipette itself "
               "would hit the tube or well" in e for e in low.errors), low.errors
    assert reach(1000, 20000).ok, reach(1000, 20000).errors  # 1000 uL tips reach 86 mm
    assert not reach(1000, 3000).ok  # but not the bottom of a 50 mL tube

    # Dispensing into a tube at the default height (1 mm above the bottom) puts the pipette into it.
    into_tube = reach(1000, 40000, dest='rack["B1"]')
    assert any("dispenses" in e and "tube.top(-5)" in e for e in into_tube.errors), into_tube.errors
    assert reach(1000, 40000, dest='rack["B1"].top(-5)').ok

    # An 8-channel using one nozzle: the nozzles without tips hang over the tube rack at the pipette's height.
    single = '    pipette.configure_nozzle_layout(style=SINGLE, start="H1", tip_racks=[tips])'
    assert reach(50, 40000, pipette="flex_8channel_50", layout=single).ok
    deep = reach(50, 5000, pipette="flex_8channel_50", layout=single)
    assert any("the pipette and its nozzles without tips would hit the labware" in e for e in deep.errors), deep.errors
    assert "a tip reaches only its own length below the pipette (50 µL tips 47.8 mm" in DeckRules().describe()
