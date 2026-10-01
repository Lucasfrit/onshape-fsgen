# Example prompts

Descriptions that have produced working parts, with what they cost. Try them as they are, or change the
dimensions. The resulting Part Studios are in the [public Onshape document](https://cad.onshape.com/documents/1fb090da32717a692a4130a3/w/dd197b61b6df7a8ada3240df/e/1bf2dc907f8a5ed598df9b97)
(open it with any Onshape account and copy it to edit).

Calls are for a native push with the default settings (variables in a Variable Studio, 1 volume check), from
`fsgen studio check`; the estimate matched the real count when tested (pillow block: 14 measured). Paste mode
always costs 0 calls.

| part | description | features | calls |
|---|---|---|---|
| Pillow block | Pillow block for a 12 mm shaft: base plate 70 x 30 x 8 mm with two 6.6 mm mounting holes 52 mm apart; a 30 mm wide, 32 mm tall upright centered on the base, with a 12 mm bore along Y whose center is 22 mm above the bottom; 2 mm fillets on the two top edges of the upright. | 9 | 14 |
| Fan adapter | Fan adapter: a 100 x 60 x 3 mm base plate with a row of 6 vent slots (4 mm wide, 40 mm long, 12 mm apart, rounded ends) on the left half, and on the right half a solid loft rising from a 40 x 40 mm square on the plate top to a 30 mm diameter circle 25 mm above the plate. Four 3.4 mm mounting holes, 5 mm from each corner. | 14 | 19 |
| NEMA 17 mount | NEMA 17 stepper motor mount: an L-bracket, 5 mm thick, 50 mm wide. The vertical plate holds the motor: 22.5 mm center bore for the boss and four M3 clearance holes (3.4 mm) on the 31 mm square pattern, centered on the plate, with the plate tall enough that the lower screws clear the base. The base plate is 45 mm deep with two 5.5 mm x 12 mm slots for M5 screws running front-to-back. Add 3 mm fillet on the inside corner. | 11 | 16 |
| Spacer | Spacer: a 20 mm diameter, 10 mm tall round spacer with a 5.5 mm through hole and 0.5 mm chamfers on both outer edges. | — | 0 (paste) |
| Washer | Washer: 16 mm outer diameter, 8.4 mm hole, 1.6 mm thick. | — | 0 (paste) |

Notes:

- The fan adapter request contradicts itself: six 4 mm slots 12 mm apart need 64 mm, but the left half is
  50 mm. The model used an 8 mm pitch and said why; `#slotPitch` is a variable, so you can change it.
- Features don't include the Variable Studio. With `FSGEN_VARIABLES=tree` every variable is a feature
  (1 call each) instead: the fan adapter then has 26 features.
- The enclosure and flange in the document were not generated: they are hand-written reference scripts
  ([examples/native](native/)) that serve as the AI's examples and as tests. 14 and 11 calls.
- For a part based on a real product (dimensions looked up on the web), see the
  [screwdriver](native/generated/neo_screwdriver.fs), designed in a chat with `/make-part`: 27 features,
  31 calls, or 0 as a paste.
