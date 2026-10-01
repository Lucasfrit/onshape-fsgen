# Demo: STEP → editable tree (the head-to-head cut)

The comparable to what Autodesk announced as AutoTimeline (AU 2026, 2026-09-15) —
with the one thing their published material doesn't describe: **a verdict**.

Target length **~35 s**, no audio needed. Save as `docs/step-to-editable.gif`.

The pitch in one line: *everyone can guess a feature tree; this one tells you
whether the guess is actually your part.*

---

## Setup

```bash
cd ~/sandbox/punkfab/featuretree
rm -rf out/demo && mkdir -p out/demo
```

Use a large terminal font. The payoff is text, so it has to be readable.

## Storyboard

**1. The frozen lump (5s).** Open `tests/step/nist_ctc_01_asme1_rd.stp` in
FreeCAD. Left panel shows a single body — no tree. Click it; nothing to edit.
*Caption: "a STEP import is one frozen solid — no pad to change, no hole to
resize."*

**2. Recover (10s).** Terminal:

```bash
python3 step_recognize.py tests/step/nist_ctc_01_asme1_rd.stp \
  --fcstd out/demo/nist_recovered.FCStd
```

Let the real output scroll — it *is* the demo:

```
  tree: outline -> body -> blind_sk0 -> blind0 -> ... -> cut20
  0 through hole(s), 14 blind
  volume: STEP 14642823.6  vs re-emitted IR 14627973.5  (Δ0.1%, bbox Δ0.0mm)
  ! 20 face(s) not captured by a single extrude (chamfers / cross-holes / freeform)
  ! multi-axis recovery: +11 pocket(s), +10 cross-hole(s); 2 residual lump(s) left uncut
  => VERIFIED
```

**3. Hold on the verdict (5s).** Freeze on the last three lines. This is the
whole argument: the warnings are *disclosed*, and `VERIFIED` means the recovered
tree provably rebuilds the input (Δ0.1% volume, exact bbox) — not "here's my best
guess." *Caption: "it re-emits the tree and checks it against your part."*

**4. It's genuinely editable (10s).** Open `out/demo/nist_recovered.FCStd`. The
left panel now shows **51 named features** — `outline → body → blind0 … →
pocket0, pocket1 → cut2 … cut20`. Expand the tree, double-click a pocket, change
its depth, watch the solid rebuild. *Caption: "51 features. Editable."*

**5. The honest tail (5s, optional but recommended).** Scroll back to the
`2 residual lump(s) left uncut` warning and say what it is: the part's 8 chamfers,
which the vocabulary can't express. *Caption: "and it tells you what it couldn't
do."*

> This last beat is the differentiator, not a weakness. Lead with it.

## Talking points

- **Verification, not confidence.** The gate is re-emit-and-compare, so the
  verdict is about reconstruction, not a score.
- **PARTIAL is a feature.** Additive bosses, splines and lofts are rejected
  rather than faked. Degrades to honest, never to silently-incorrect.
- **Portable.** The same recovered IR drives FreeCAD, Onshape and build123d;
  it isn't locked in one application.
- **Fast enough.** 7.7 s for a 51-feature recovery on this part, single core.

## Do NOT claim in the demo

- Any recovery rate over a corpus — evidence is benchmark parts, not a study.
- That recovered names carry design intent — they're generated (`cut2`,
  `pocket0`). Stable, but not meaningful.
- That the 9-part cadgen planetary result reproduces from this repo — the
  fixture geometry blobs aren't distributed. See `paper/figures/README.md`.
