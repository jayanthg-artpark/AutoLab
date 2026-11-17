# ELISA on this lab's Flex (template)

How this lab runs plate ELISAs on the Flex, and the mistakes to avoid. The general assay shape is in
templates/sandwich-elisa.md; follow the kit manual for volumes, times and wavelengths. Worked examples, each
simulated in run order and reviewed (not yet run on the robot, 2026-11-17):

| Example | Use it for |
|---|---|
| protocols/lab-elisa/elisa-sandwich-biotin.md | Sandwich ELISA, biotinylated detection antibody + streptavidin-HRP, standard series made on the deck, 8 samples in duplicate |
| protocols/lab-elisa/elisa-competitive.md | Competitive ELISA (signal falls with concentration), ready-made standards, NSB wells |

## The lab's constraints

- No plate washer and no shaker: the Flex 1-channel 1000 µL pipette washes (300 µL in from 12 mm, out from 1 mm
  above the bottom into the waste chute) and mixes by pipette. Before the first wash of a round, take the
  incubation liquid out.
- The lab's rule: a fresh tip for every well liquid is taken out of, and for every standard or sample well. A
  32-well ELISA therefore uses about 300-450 tips, mostly on the washes. Anything dispensed from 12 mm (wash
  buffer, antibody, conjugate, TMB, stop) never touches the liquid: one tip can serve many wells.
- One workflow = one run. Staff load every tube and every tip rack before it starts; the protocols run one after
  another on the same deck, and each continues the tip racks the earlier ones used. The plate, one Falcon rack
  and one Eppendorf rack take 3 of the 8 slots (B1-D2), leaving at most 5 for tip racks (480 tips).
- Each reagent addition (with the washes before it) is its own protocol; incubations are hold steps. Turn the
  lights off (protocol.set_rail_lights(False)) at the end of the TMB protocol.
- Read 450 nm with 570 nm as reference within 30 minutes of the stop solution. Analysis: standard_curve, 4PL,
  reference_measurement "Absorbance 570 nm", x_log true, the zero standard as blank (sandwich) or the NSB wells as
  blank (competitive; there the 0 standard is the maximum signal).

## Check before sending (mistakes the AI model made on 2026-11-17)

Each protocol "passed" on its own; only checking the whole run caught these.

- Tips for the whole run: simulate every protocol in run order. The first designs needed 268-840 tips and ran
  out mid-run. Use the same tip racks, in the same slots and the same order, in every protocol.
- One tube, one liquid for the whole run. A tube an earlier protocol used still holds what is left: don't
  declare it as another reagent later (wash buffer left in A3 was declared as HRP; antibody in A2 as stop).
- Give each wash round its own wash-buffer Falcon with enough for the round (wells x 300 µL x cycles, plus a few
  mL): one Falcon declared full in every round runs dry.
- Leave a reserve in every tube the plate is filled from (e.g. 250 µL for two 100 µL wells), and in on-deck
  dilutions (30 µL + 270 µL rather than 20 + 180 for two 100 µL wells).
- One pipette with two tip sizes in a multi-protocol run: until the master node counts tips per rack, don't name
  a rack in pick_up_tip(rack) in a run of several protocols; give each pipette one tip size per run, picked with
  pick_up_tip().
