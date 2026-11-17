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
    pipette.dispense(10, plate["DEST"], push_out=2)
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
    pipette.aspirate(50, rack["A1"].meniscus(z=-2, target="end"))
    pipette.dispense(50, DEST)
    pipette.drop_tip()
''' % LATEST


def reach(tips: int, volume: int, dest: str = 'plate["H1"]', pipette: str = "flex_1channel_1000", layout: str = ""):
    code = (REACH.replace("TIPS", str(tips)).replace("VOLUME", str(volume)).replace("DEST", dest)
            .replace("PIPETTE", pipette).replace("LAYOUT", layout))
    return asyncio.run(simulate(code, "D1"))


def test_tips_reach_only_their_length_into_a_tube():
    assert reach(50, 40000).ok  # a full 50 mL tube: the surface is near the top
    # 5 mL: the surface is about 98 mm down. A 50 uL tip reaches 48 mm and the nozzle follows it 33 mm into the tube.
    low = reach(50, 5000)
    assert any(e.startswith("The flex_1channel_1000 aspirates ") and "50 µL tips reach only 48 mm (its nozzle follows "
               "them at most 33 mm into this opening), so the pipette itself would hit the tube or well" in e
               for e in low.errors), low.errors
    # 1000 uL tips reach 86 mm and, with the nozzle following, the bottom of a 50 mL tube: Opentrons' own library
    # protocols take them to 1 mm above the bottom of 15 mL tubes.
    assert reach(1000, 20000).ok, reach(1000, 20000).errors
    assert reach(1000, 3000).ok, reach(1000, 3000).errors
    assert reach(1000, 40000, dest='rack["B1"]').ok  # dispensing at the default 1 mm above the bottom

    # A 50 uL tip dispensing at the default height (1 mm above the bottom) puts the pipette itself into the tube.
    into_tube = reach(50, 40000, dest='rack["B1"]')
    assert any("dispenses" in e and "tube.top(-5)" in e for e in into_tube.errors), into_tube.errors
    assert reach(50, 40000, dest='rack["B1"].top(-5)').ok

    # An 8-channel using one nozzle: the nozzles without tips hang over the tube rack at the pipette's height.
    single = '    pipette.configure_nozzle_layout(style=SINGLE, start="H1", tip_racks=[tips])'
    assert reach(50, 40000, pipette="flex_8channel_50", layout=single).ok
    deep = reach(50, 5000, pipette="flex_8channel_50", layout=single)
    assert any("the pipette and its nozzles without tips would hit the labware" in e for e in deep.errors), deep.errors
    assert "a tip reaches only its own length below the pipette (50 µL tips 47.8 mm" in DeckRules().describe()


PIPETTING = '''requirements = {"robotType": "Flex", "apiLevel": "%s"}
def run(protocol):
    protocol.load_waste_chute()
    tips = protocol.load_labware("opentrons_flex_96_tiprack_200ul", "C3")
    res = protocol.load_labware("nest_12_reservoir_15ml", "C2")
    plate = protocol.load_labware("nest_96_wellplate_200ul_flat", "D1")
    p = protocol.load_instrument("flex_1channel_1000", "left", tip_racks=[tips])
    res["A1"].load_liquid(protocol.define_liquid("Diluent"), 10000)
    res["A2"].load_liquid(protocol.define_liquid("Standard"), 10000)
    res["A3"].load_liquid(protocol.define_liquid("Reagent"), 10000)
BODY
''' % LATEST

# Triplicates of 10 uL diluent with one tip into empty wells, then 10 uL standard into each with a fresh tip
# and a mix, then 100 uL reagent from above (above the rim: one tip is fine).
GOOD_BODY = """
    p.pick_up_tip()
    for w in ("A1", "A2", "A3"):
        p.aspirate(10, res["A1"])
        p.dispense(10, plate[w].bottom(z=1), push_out=2)
    p.drop_tip()
    for w in ("A1", "A2", "A3"):
        p.pick_up_tip()
        p.aspirate(10, res["A2"])
        p.dispense(10, plate[w].bottom(z=1), push_out=2)
        p.mix(3, 15, plate[w].bottom(z=1))
        p.drop_tip()
    p.pick_up_tip()
    for w in ("A1", "A2", "A3"):
        p.aspirate(100, res["A3"])
        p.dispense(100, plate[w].bottom(z=12))
    p.drop_tip()
"""


def pipetting(body: str):
    return asyncio.run(simulate(PIPETTING.replace("BODY", body), "D1"))


def test_the_labs_pipetting_rules():
    good = pipetting(GOOD_BODY)
    assert good.ok, good.errors
    assert "Mix 3× 15 µL in A1 of NEST 96 Well Plate 200 µL Flat" in good.steps
    assert any(s.startswith("Dispense 10 µL into A1") and s.endswith("(push-out 2 µL)") for s in good.steps)

    # Push-out taken as extra liquid: 12 uL aspirated, 10 + 2 dispensed.
    extra = pipetting('    p.pick_up_tip()\n    p.aspirate(12, res["A1"])\n'
                      '    p.dispense(10, plate["A1"].bottom(z=1))\n    p.dispense(2, plate["A1"].bottom(z=1))\n    p.drop_tip()')
    assert any("never aspirate extra for it" in e and "push-out 0 µL" in e for e in extra.errors), extra.errors
    assert pipetting('    p.pick_up_tip()\n    p.aspirate(10, res["A1"])\n    p.dispense(10, plate["A1"], push_out=2)\n'
                     '    p.drop_tip()').ok  # Opentrons dispenses 1 mm above the bottom by default
    low = pipetting('    p.pick_up_tip()\n    p.aspirate(10, res["A1"])\n    p.dispense(10, plate["A1"].bottom(z=5), push_out=2)\n'
                    '    p.drop_tip()')
    assert any("A1 (10 µL at 5.0 mm)" in e for e in low.errors), low.errors
    big = pipetting('    p.pick_up_tip()\n    p.aspirate(100, res["A3"])\n    p.dispense(100, plate["A1"].bottom(z=1))\n    p.drop_tip()')
    assert any("from above, 12 mm above the bottom" in e for e in big.errors), big.errors

    # One tip into wells that already hold liquid carries the first well's liquid on.
    carried = GOOD_BODY.replace('''    for w in ("A1", "A2", "A3"):
        p.pick_up_tip()
        p.aspirate(10, res["A2"])
        p.dispense(10, plate[w].bottom(z=1), push_out=2)
        p.mix(3, 15, plate[w].bottom(z=1))
        p.drop_tip()''', '''    p.pick_up_tip()
    for w in ("A1", "A2", "A3"):
        p.aspirate(10, res["A2"])
        p.dispense(10, plate[w].bottom(z=1), push_out=2)
    p.drop_tip()''')
    sim = pipetting(carried)
    # (back to the standard in the reservoir, after going into the diluent in A1)
    assert any("A1 of NEST 96 Well Plate 200 µL Flat into A2 of NEST 12 Well Reservoir" in e
               and "take a fresh tip" in e for e in sim.errors), sim.errors
    # Dispensing one source's liquid into each of three wells that hold liquid, without going back to the source.
    split = pipetting('    p.pick_up_tip()\n    p.aspirate(30, res["A1"])\n'
                      '    for w in ("A1", "A2", "A3"):\n        p.dispense(10, plate[w].bottom(z=1), push_out=0 if w != "A3" else 2)\n'
                      '    p.drop_tip()\n    p.pick_up_tip()\n    p.aspirate(30, res["A2"])\n'
                      '    for w in ("A1", "A2", "A3"):\n        p.dispense(10, plate[w].bottom(z=1), push_out=2)\n    p.drop_tip()')
    assert any("A1 of NEST 96 Well Plate 200 µL Flat into A2 of NEST 96 Well Plate 200 µL Flat" in e for e in split.errors), split.errors

    # Below the lab's 1-channel 1000 uL pipette's 2 uL minimum (checked on the robot) the drop stays on the tip.
    tiny = pipetting('    p.pick_up_tip()\n    p.aspirate(1, res["A2"])\n    p.dispense(1, plate["B1"].bottom(z=1), push_out=2)\n'
                     '    p.drop_tip()')
    assert any("The flex_1channel_1000 pipettes 2-1000 µL" in e and "1 µL (A2, B1)" in e and "dilute the stock" in e
               for e in tiny.errors), tiny.errors
    assert pipetting('    p.pick_up_tip()\n    p.aspirate(2, res["A2"])\n    p.dispense(2, plate["B1"].bottom(z=1), push_out=2)\n'
                     '    p.drop_tip()').ok
    assert "there is no higher minimum" in DeckRules().describe()

    # Under 100 uL of reagent the tip is covered: no push-out is right there (air would bubble the reagent).
    under = ('    p.pick_up_tip()\n    p.aspirate(100, res["A3"])\n    p.dispense(100, plate["C1"].bottom(z=12))\n    p.drop_tip()\n'
             '    p.pick_up_tip()\n    p.aspirate(8, res["A2"])\n    p.dispense(8, plate["C1"].bottom(z=1), push_out=0)\n    p.drop_tip()')
    assert pipetting(under).ok
    dry = pipetting('    p.pick_up_tip()\n    p.aspirate(8, res["A2"])\n    p.dispense(8, plate["C1"].bottom(z=1), push_out=0)\n'
                    '    p.drop_tip()')
    assert any("C1 (8 µL, push-out 0 µL)" in e and "push_out=0 is right" in e for e in dry.errors), dry.errors

    # Small volumes go in small tips: 10 uL from a 1000 uL tip fails, a mix with that tip doesn't.
    big_tips = PIPETTING.replace("opentrons_flex_96_tiprack_200ul", "opentrons_flex_96_tiprack_1000ul")
    sim = asyncio.run(simulate(big_tips.replace("BODY", GOOD_BODY), "D1"))
    assert any("moves less than 50 µL with a 1000 µL tip (10 µL into A1" in e and "pick up 50 µL tips" in e
               for e in sim.errors), sim.errors
    assert not any("15 µL" in e for e in sim.errors), sim.errors
    assert "use the smallest tip that holds the volume" in DeckRules().describe()

    wet = pipetting(GOOD_BODY.replace("    p.drop_tip()\n", "    p.drop_tip()\n    p.transfer(20, res['A1'], plate['B1'], pre_wet=True)\n", 1))
    assert any("pre-wets tips" in e for e in wet.errors), wet.errors
    assert "Never pre-wet tips" in DeckRules().describe()


def test_a_50_ul_pipette_goes_down_to_1_ul_in_low_volume_mode():
    code = PIPETTING.replace("opentrons_flex_96_tiprack_200ul", "opentrons_flex_96_tiprack_50ul").replace(
        'protocol.load_instrument("flex_1channel_1000"', 'protocol.load_instrument("flex_1channel_50"')
    body = '    p.pick_up_tip()\n    p.aspirate(2, res["A2"])\n    p.dispense(2, plate["B1"].bottom(z=1), push_out=2)\n    p.drop_tip()'
    default = asyncio.run(simulate(code.replace("BODY", body), "D1"))
    assert any("The flex_1channel_50 pipettes 5-50 µL" in e and "configure_for_volume" in e for e in default.errors), default.errors
    low = asyncio.run(simulate(code.replace("BODY", "    p.configure_for_volume(2)\n" + body), "D1"))
    assert low.ok, low.errors


def test_a_tip_spot_is_not_picked_up_twice():
    # The simulator allows naming the same spot every time; on the robot it is empty the second time.
    same = pipetting("""
    for w in ("A1", "A2"):
        p.pick_up_tip(tips["A1"])
        p.aspirate(100, res["A3"])
        p.dispense(100, plate[w].bottom(z=12))
        p.drop_tip()
""")
    assert any("picks up tips again from a spot it already emptied: A1 of" in e for e in same.errors), same.errors
    # A tip put back (return_tip) may be picked up again.
    returned = pipetting("""
    p.pick_up_tip(tips["A1"])
    p.return_tip()
    p.pick_up_tip(tips["A1"])
    p.aspirate(100, res["A3"])
    p.dispense(100, plate["A1"].bottom(z=12))
    p.drop_tip()
""")
    assert returned.ok, returned.errors


def test_small_volumes_may_be_pushed_out_with_2_to_7_ul():
    # A 2 uL drop stays on a 50 uL tip with 2 uL of push-out and comes off with 5 uL (tested on the lab's Flex).
    def push(n):
        return pipetting(f'    p.pick_up_tip()\n    p.aspirate(2, res["A2"])\n'
                         f'    p.dispense(2, plate["B1"].bottom(z=1), push_out={n})\n    p.drop_tip()')
    assert push(2).ok and push(5).ok and push(7).ok
    for bad in (1, 10):
        sim = push(bad)
        assert any(f"B1 (2 µL, push-out {bad} µL)" in e and "5 µL with 50 µL tips" in e for e in sim.errors), sim.errors
    assert "comes off with 5 µL" in DeckRules().describe()


SMALL_50 = '''
from opentrons import protocol_api
requirements = {"robotType": "Flex", "apiLevel": "%s"}

def run(protocol):
    protocol.load_waste_chute()
    tips = protocol.load_labware("opentrons_flex_96_filtertiprack_50ul", "C2")
    tubes = protocol.load_labware("opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap", "B2")
    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", "C1")
    p = protocol.load_instrument("flex_1channel_1000", "left", tip_racks=[tips])
    tubes["A2"].load_liquid(protocol.define_liquid("BSA"), 100)
BODY
''' % LATEST


def test_small_volumes_with_50_ul_tips_need_the_tested_settings():
    def run(body):
        return asyncio.run(simulate(SMALL_50.replace("BODY", body), "C1"))

    # What worked on the lab's Flex: 20 uL/s, a 1 s pause after aspirating, a 5 uL push-out.
    good = run('''    p.pick_up_tip()
    for w in ("B1", "B2"):
        p.flow_rate.aspirate = 20
        p.flow_rate.dispense = 20
        p.aspirate(2, tubes["A2"].bottom(z=1))
        protocol.delay(seconds=1)
        p.dispense(2, plate[w].bottom(z=1), push_out=5)
    p.drop_tip()''')
    assert good.ok, good.errors
    # What left the drop on the tip: default speed, no pause, 2 uL of push-out.
    bad = run('''    p.pick_up_tip()
    p.aspirate(2, tubes["A2"].bottom(z=1))
    p.dispense(2, plate["B1"].bottom(z=1), push_out=2)
    p.drop_tip()''')
    error = next(e for e in bad.errors if e.startswith("Less than 10 µL with 50 µL tips on a 1000 µL pipette"))
    assert "too fast: 2 µL from A2 at 478 µL/s" in error and "no pause after aspirating: 2 µL from A2" in error
    assert "push-out under 5 µL: 2 µL into B1 (push-out 2 µL)" in error
    # 10 uL worked with the defaults, and a mix is not a transfer.
    assert run('''    p.pick_up_tip()
    p.aspirate(10, tubes["A2"].bottom(z=1))
    p.dispense(10, plate["B1"].bottom(z=1), push_out=2)
    p.mix(3, 8, plate["B1"].bottom(z=1))
    p.drop_tip()''').ok
