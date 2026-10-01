# General modeling rules

These rules are sent to the model with every request and checked in the review step. Edit freely:
one rule per bullet, written as an instruction.

## Features
- Do not use the Hole feature unless the request explicitly asks for it. Make holes with a sketch
  (circles) and an extrude with `NewBodyOperationType.REMOVE`, THROUGH_ALL or BLIND, with `booleanScope`
  set to the part.
- Prefer the simplest standard feature that captures the intent: sketch + extrude/revolve first, then
  patterns/mirror, then fillets/chamfers. Avoid features that the request does not need.
- Repeated geometry: draw it once and use a feature pattern (`linearPattern`, `circularPattern`, `mirror`)
  instead of copying sketches or features.
- Fillets and chamfers go at the end of the tree.

## Variables
- Every key dimension from the request becomes a variable with a descriptive camelCase name
  (`plateWidth`, `holeDiameter`), defined at the top of the tree.
- Derived positions and sizes are expressions of variables (`#plateWidth / 2 - #holeInset`), not
  hard-coded numbers, so changing a variable updates the model.

## Sketches and features
- One purpose per sketch; name sketches and features after what they make ("Base plate sketch",
  "Base plate", "Mounting holes").
- Units are millimeters (and degrees).
- Z is up; the part sits on the Top plane. Symmetric parts are centered on the origin.
- Unless asked otherwise the result is exactly one part.

## API budget (each feature in the tree costs one Onshape API call; 2500 calls/year)
- Keep the tree lean: use the fewest features that keep the design editable.
- Make variables only for dimensions someone is likely to change (typically 4-8). Derive the rest
  with expressions instead of extra variables.
- Holes or cuts that share a direction and depth go in one sketch and one extrude, unless a pattern
  of a single feature is clearer.

## 3D printing (when the part is meant to be printed)
- Fits between printed parts: 0.2–0.3 mm clearance per side for sliding fits, 0.1 mm for press fits; holes for
  screws/pins about 0.2–0.4 mm larger than nominal. Make clearances a variable.
- Give each part a flat face to print on and lay printed parts out side by side on the Top plane, in their print
  orientation (not assembled), unless asked otherwise.
- Avoid overhangs flatter than 45° and unsupported flat ceilings; use 45° chamfers instead of fillets on edges
  that face the print bed.
- Minimum wall thickness 1.2 mm (≈ 3 perimeters); minimum feature size about 0.8 mm.
- Printed threads: pitch ≥ 1.5 mm with radial clearance ≥ 0.2 mm; start/end threads with a chamfer.
- Snap fits need something that can flex: a slot through a pin, a thin cantilever arm, or a split ring. Two
  solid walls cannot snap together.
