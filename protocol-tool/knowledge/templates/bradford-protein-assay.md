# Bradford protein assay on this lab's Flex (template)

How this lab runs Bradford (Coomassie) assays on the Flex, and the mistakes to avoid. Five worked example
protocols, each simulated and reviewed, are in protocols/lab-bradford/: start from the closest one.

| Example | Use it for |
|---|---|
| protocols/lab-bradford/plate-serial-dilution.md | Standards only, made by two-fold serial dilution on the plate (10 to 0.3125 µg/well, 50 µL + 200 µL reagent) |
| protocols/lab-bradford/tube-standards-10ul.md | Standards only, made in Eppendorfs from a 2 mg/mL stock (125-2000 µg/mL, 10 µL + 200 µL) |
| protocols/lab-bradford/standards-and-samples.md | Up to 8 samples with ready-made standards (10 µL + 200 µL) |
| protocols/lab-bradford/samples-diluted-on-deck.md | Up to 8 concentrated samples diluted 1:5 on the deck first |
| protocols/lab-bradford/micro-assay-150ul.md | Dilute samples, 1-25 µg/mL (150 µL + 150 µL reagent) |

These are simulated and reviewed but have not yet run on the robot (2026-11-17). Experiment records in
experiments/ show what happened on real runs.

## Formats

- Standard microplate: 10 µL standard or sample + 200 µL reagent, BSA 125-2000 µg/mL. Quadratic fit (the curve
  bends at the top of this range).
- Micro: 150 µL + 150 µL reagent, BSA 1-25 µg/mL. Linear fit.
- Standards by serial dilution on the plate: 50 µL per well + 200 µL reagent, x in µg per well. Linear fit.
- Read absorbance at 595 nm after 10 minutes at room temperature; the colour is stable for about an hour.

## Hardware and handling in this lab

- Flex 1-channel 1000 µL pipette with Flex 1000 µL filter tips (more than 50 µL) and Flex 50 µL filter tips
  (50 µL and less). Load both racks and pick from the one that fits: pipette.pick_up_tip(tips_50) or
  pipette.pick_up_tip(tips_1000). Never name a tip spot (tips["A1"]): it is empty after the first pick-up.
- Liquids in Falcon tubes (Opentrons 10-tube rack: 15 mL tubes in A1, B1, C1, A2, B2, C2; 50 mL tubes in A3, B3,
  A4, B4) and 1.5 mL Eppendorfs (24-tube rack). No reservoirs. Water in a 15 mL tube, Bradford reagent in a 50 mL
  tube; aspirate both at tube.meniscus(z=-2, target="end") with their volumes declared.
- A fresh tip for every well that gets a standard or a sample. On 2026-11-13 one tip filling a standard's three
  wells (2-10 µL) gave columns 2 and 3 about 0.1 OD more than column 1 and pulled R² down to 0.86.
- 1-10 µL into the plate: 1 mm above the bottom with a 2 µL push-out (lab rule). More than 50 µL: from 12 mm.
- The Tecan cannot shake the plate. After the reagent, mix each well gently with its own tip: 3 x 100 µL (no more
  than about half the well's liquid) at 2 mm above the bottom, aspirate and dispense at 50 µL/s, then put the flow
  rates back. Bubbles spoil absorbance readings.

## Check before simulating (mistakes the AI model made on 2026-11-17)

The simulator passed all of these; only a review caught them.

- Dilution arithmetic: check every tube with C1·V1 = C2·V2. "300 µL of 2 mg/mL + 300 µL water" is 1000 µg/mL, not
  100.
- Every standard made in a tube needs both the BSA and the water to its final volume. Declare tubes the robot
  fills as empty (0 µL): declaring the final volume hides a missing water step.
- Serial dilution on the plate: the first well needs twice the transfer volume (100 µL for 50 µL transfers), or
  it ends up empty and every standard shifts one well. Mix each step at least 5 times.
- Analysis: when standards and samples use the same volume, x is the standards' concentration (µg/mL) and the
  samples' dilution is 1; only an on-deck dilution of the sample (e.g. 1:5) is a dilution factor. Include the
  0 µg/mL wells as a standard at x = 0 and as blank_wells.
- Read only the wells the protocol fills.
- The reagent in a 50 mL position, not a 15 mL one.
