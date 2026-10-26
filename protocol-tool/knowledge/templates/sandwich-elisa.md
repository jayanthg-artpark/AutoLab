# Sandwich ELISA on a pre-coated plate (example template)

The general shape of a colorimetric sandwich ELISA with an HRP detection system and
TMB substrate. Every kit differs: where the kit manual gives volumes, incubation
times, wash counts or wavelengths, follow the manual, not this template.

## Plate layout
- Standards: a 7-point two-fold series plus a zero standard, in duplicate.
- Samples: in duplicate, diluted into the standard range as the manual says.
- Blank wells: assay diluent only.

## Workflow
1. liquid_handling: add standards and samples (typically 100 uL per well).
2. hold: incubate (typically 1.5-2 h at room temperature).
3. wash: typically 3-4 cycles of 300 uL wash buffer.
4. liquid_handling: add detection antibody (typically 100 uL per well).
5. hold: incubate (typically 1 h at room temperature).
6. wash: as above.
7. liquid_handling: add streptavidin-HRP (if the kit uses a biotinylated detection antibody).
8. hold: incubate (typically 20-30 min, room temperature, protected from light).
9. wash: as above.
10. liquid_handling: add TMB substrate (typically 100 uL per well).
11. hold: develop (typically 15-30 min, protected from light).
12. liquid_handling: add stop solution (typically 50-100 uL per well).
13. read: absorbance at 450 nm, with 540 or 570 nm as a reference wavelength if the
    manual asks for wavelength correction. Read within 30 min of stopping.

## Notes
- Steps 4-12 each need their own protocol (different reagents), or one protocol per
  reagent addition.
- Reagents sit on the Flex deck for the whole run; staff load them before it starts.
- Analysis: standard_curve, fit 4pl (5pl if the curve is asymmetric), measurement
  "Absorbance 450 nm", reference_measurement "Absorbance 570 nm" when read, blank_wells =
  the zero standard / diluent wells (leave the zero standard out of the 4PL standards),
  samples with their dilution factor, x_log true.
