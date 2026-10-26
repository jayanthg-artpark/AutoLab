"""Example Flex protocol for the protocol tool.

The protocol keeps the sample plate in D2, where Thor places it before the run and carries it
to the Tecan afterwards, so the protocol must not move the plate itself.

Deck: tips C1, reservoir C2, trash A3, sample plate D2 (placed by Thor).
"""

from opentrons import protocol_api

metadata = {"protocolName": "Two-fold dilution series", "author": "Lab automation"}
requirements = {"robotType": "Flex", "apiLevel": "2.29"}


def add_parameters(parameters):
    parameters.add_float(
        variable_name="volume_ul",
        display_name="Volume per well",
        description="Diluent added to every well before the series.",
        default=100.0,
        minimum=20.0,
        maximum=180.0,
        unit="µL",
    )


def run(protocol: protocol_api.ProtocolContext):
    tips = protocol.load_labware("opentrons_flex_96_tiprack_200ul", "C1")
    reservoir = protocol.load_labware("nest_12_reservoir_15ml", "C2")
    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", "D2")
    protocol.load_trash_bin("A3")
    pipette = protocol.load_instrument("flex_1channel_1000", "left", tip_racks=[tips])

    volume = protocol.params.volume_ul
    diluent, stock = reservoir["A1"], reservoir["A2"]

    # Diluent into columns 2-12, stock into column 1.
    pipette.transfer(volume, diluent, [w for col in plate.columns()[1:] for w in col])
    pipette.transfer(volume * 2, stock, plate.columns()[0])

    # Two-fold serial dilution across each row, mixing after each step.
    for row in plate.rows():
        pipette.transfer(volume, row[:11], row[1:], mix_after=(3, volume / 2), new_tip="always")
