# Absorbance dilution series (example template)

A serial dilution of a coloured or turbid sample across a 96-well plate, read by
absorbance. Use for dye standard curves, OD600 of cultures, or checking pipetting.

## Plate layout
- Columns 1-11: two-fold dilution series, highest concentration in column 1.
- Column 12: diluent only (blank).
- Rows A-H: replicates (at least 3 rows per sample).

## Workflow
1. liquid_handling: fill columns 2-12 with diluent (100 uL), add 200 uL sample to
   column 1, then transfer 100 uL from column to column 1 -> 11, mixing 3 times after
   each transfer; discard 100 uL from column 11 so every well holds 100 uL.
2. read: absorbance. Dyes at their absorbance maximum (e.g. 595 nm for Coomassie,
   630 nm for bromophenol blue); cultures at 600 nm. Read all wells used.

## Analysis
- dilution_series on the absorbance read: standards = each column (all replicate rows),
  x = relative concentration (1, 0.5, 0.25, ...), blank_wells = column 12, fit linear,
  x_log true. As a standard curve (known stock concentration), use standard_curve.

## Notes
- Keep the series within the reader's linear range (roughly OD 0.05-2.0 for 100 uL
  in a flat-bottom plate); dilute the stock first if column 1 is above it.
- Subtract the mean of the blanks before plotting.
