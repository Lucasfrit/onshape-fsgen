# featuretree

[![Paper](https://img.shields.io/badge/paper-10.5281%2Fzenodo.22848792-blue)](https://doi.org/10.5281/zenodo.22848792)  [![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.22848721.svg)](https://doi.org/10.5281/zenodo.22848721)  [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Emit an editable feature tree — in FreeCAD *and* Onshape — from a small neutral feature-IR, round-trip human edits back by name, and render the same IR to a build123d solid.**

<!-- HERO GIF — record `docs/roundtrip.gif` (storyboard in LAUNCH.md), then uncomment:
<p align="center">
  <img src="docs/roundtrip.gif" width="720"
       alt="code/LLM writes the IR → native editable feature tree opens in FreeCAD → drag a sketch/param by hand → the edit reads back into the code by name">
  <br><em>Author once as code (or have an LLM write it) → a native, editable FreeCAD tree → hand-edit → your change reads back <b>by name</b>.</em>
</p>
-->

**AI and code can generate CAD now — but they hand you a *dead solid*.** A build123d/LLM-generated
part, or a `.step`/`.stl`, opens in FreeCAD as one frozen lump: you can't grab a pad and change its
depth, because the parametric feature tree is gone. `featuretree` is the missing **round-trip** — it
keeps the design as named, ordered *operations* the whole way, so generated CAD stays hand-editable in
a real tool and your edits flow back to the code.

A neutral *file* (STEP/STL) — or a code-baked solid — loses the parametric feature tree: it imports
into FreeCAD as one frozen solid you can't edit by operation. `featuretree` keeps the tree. You author a design once as
a small **feature IR** (named, ordered operations), and an emitter re-authors it in FreeCAD's *own*
feature vocabulary (`PartDesign::Sketch / Pad / Pocket / Fillet`), with each object's `Label` set to
your feature name. Open the `.FCStd` and the operations are right there in the left-panel tree,
editable. Human edits read back **by name**, so they survive rebuilds.

```
   ir.py  (the DSL, your single source of truth)        gen.py  ── emit ──►  <part>.FCStd
   named features · named params · symbolic geometry                         (native tree,
   queries (no kernel edge ids)                          roundtrip.py ◄─ read ─ human edits, by name)
```

This is packaged as a [Claude Code](https://claude.com/claude-code) **skill**, but the Python is
plain and runs standalone — see [Use without Claude Code](#use-without-claude-code).

## Why

The thing that makes a feature tree survive a round-trip is that geometry is referenced
*symbolically* — by feature name and queries — never by unstable kernel edge/face ids. That's the
wall that kills neutral feature-file formats. The IR sidesteps it:

- every feature has a stable, human-meaningful `name` → the target object's `Label`, so a human edit
  is matched back **by name**, not by geometry;
- parameters are named values, not positions in a blob;
- edge/face selection is a **query** re-resolved against live geometry at every build, never a stored
  kernel id.

## Requirements

- **FreeCAD 1.0+ AppImage.** FreeCAD is driven head-less through its own bundled Python (3.11) via
  `freecadcmd` — build123d / OCP can't author a *native* feature tree. The runner auto-locates the
  AppImage at `/opt`, or one-time-extracts it and caches the result. Override with the
  `FREECAD_APPIMAGE` (path to the AppImage) or `FREECAD_CMD` (path to a `freecadcmd` binary)
  environment variables.
- **Python 3** for the host-side scripts (`gen.py` / `roundtrip.py`).
- **For the Onshape backend (optional):** an Onshape API key (access + secret) from
  <https://dev-portal.onshape.com>, and `pip install shapely`. See [Onshape backend](#onshape-backend).

## Install

As a Claude Code skill:

```bash
git clone https://github.com/punkfab/featuretree.git ~/.claude/skills/featuretree
```

Claude Code discovers it automatically; ask it to use the `featuretree` skill when you want a
FreeCAD file that opens with its operations in the tree.

## Quickstart

```bash
S=~/.claude/skills/featuretree
python3 $S/gen.py --sample plate         # built-in: 40x30x10 plate + hole  -> $S/out/plate.FCStd
python3 $S/gen.py --sample poly          # exercises the polygon-profile primitive
python3 $S/gen.py mypart.ir.json out/mypart.FCStd     # emit a spec you authored
python3 $S/roundtrip.py out/mypart.FCStd              # read the tree / params back
python3 $S/roundtrip.py out/mypart.FCStd edits.json   # apply a named edit, re-save, report
```

Authoring a part with the DSL (produce a JSON-able dict, write it, emit it):

```python
import sys; sys.path.insert(0, "/path/to/featuretree")
import ir, json
spec = ir.part("bracket",
    ir.sketch("profile", polys=[[(0,0),(40,0),(40,14),(16,14),(16,30),(0,30)]]),  # outer wire
    ir.pad("body", "profile", length=6),
    ir.sketch("holes", circles=[(8,8,2.6)]),
    ir.pocket("drill", "holes", through=True),
)
json.dump(spec, open("bracket.ir.json", "w"))            # then: python3 gen.py bracket.ir.json
```

## The IR (see [`ir.py`](ir.py))

- `sketch(name, plane="XY"|"XZ", circles=[(cx,cy,r)], rects=[(w,h,cx,cy)], polys=[wire,...], on=None)`
  — `polys`: first wire = outer profile, following wires = holes (one sketch → one pad gives a plate
  with holes); a wire vertex may carry a DXF **bulge** `(x,y,bulge)` for a circular arc.
  `on={"face_of": feat, "side": "top"|"bottom"}` attaches the sketch to a face chosen by **query**
  (circles, rects, *and* polys).
- `pad(name, sketch, length, symmetric=False)` — deterministic +Z growth (so both backends agree).
- `pocket(name, sketch, through=True, length=None)`
- `revolve(name, sketch, angle=360)` — revolve an XZ profile about Z (wheels, bosses, nozzles).
- `prism_cut(name, origin, normal, xdir, depth, polys=[wire,...])` — subtract a 2D profile extruded
  along an **arbitrary axis** at an **arbitrary location**. One primitive for any placed cut: a
  floor / through pocket along the main axis *or* a cross-axis hole ⊥ to it. This is what multi-axis
  STEP recovery emits; both backends build it identically.
- `polar_pocket(name, radius, length, mount_r, z, count, phase)` — a ring of tangent bores.
- `fillet(name, radius, select={"circles": "top_outer"})` — edges chosen by **query**, re-resolved to
  live `EdgeN` every build (never a stored kernel id — the topological-naming sidestep).
- `part(name, *features)` → the spec. `update_from_freecad(spec, params)` flows read-back edits in.

## build123d backend

The same IR also renders straight to a **build123d** solid, in the caller's own Python — no FreeCAD
process. build123d and FreeCAD share the OpenCASCADE kernel, so the IR yields **identical geometry**
either way: the `plate` sample is 11497.3 mm³ from both backends (Δ = 0.0). Author the design once as
IR and get *both* the editable parametric tree (FreeCAD/Onshape) *and* a watertight solid you can
mesh / simulate / interference-check — with no second, hand-maintained model to drift.

```python
import ir, b3d_emit
part, res = b3d_emit.emit(ir.SAMPLES["plate"]())   # part is a build123d Solid
print(res["volume"], "mm^3")                        # 11497.3, same as FreeCAD
```

```bash
python3 b3d_emit.py --sample plate out/plate.stl    # IR -> watertight .stl
```

Coverage mirrors `fc_build.py`: sketches (circles / rects / polygons-with-holes, straight + arc) on
XY / XZ or a part's top/bottom face, pad (± midplane), pocket (through / blind), revolve, `prism_cut`
(placed profile-along-any-axis), polar pockets, and fillet by the **same edge query** the IR stores
(resolved against live geometry, no kernel ids).

## STEP → IR (feature recognition) — round-trip engineering

A STEP/STL from a vendor, a 3D scan, a colleague, or a decade-old archive is a **frozen solid**: the
parametric feature tree is gone, so it imports as one lump you can't edit by *operation*. Change a
pocket's depth or a hole's diameter and you're pushing vertices, not editing the design's intent.

`step_recognize.py` **recovers a feature tree from the dumb B-rep** — and, paired with the by-name
round-trip below, that closes the engineering loop: **import a frozen solid → recover a named,
parametric tree → edit an operation in FreeCAD (or build123d) → re-emit → and it's *proven* to still
be the same part minus your intended change.** The neutral file becomes editable again, without
trusting a black-box recognizer that might quietly be wrong — because every recovery is checked by
reconstruction (below). That is the motivation for the whole tool: neutral files are how CAD data
actually moves between people and programs; this makes them *parametric* again on the way in.

```python
import step_recognize
spec, report = step_recognize.recognize("part.step")    # spec is a featuretree IR
print(report["verified"], report["dvol_pct"], report.get("recovered"))
# True 0.14 {'pockets': 14, 'holes': 10, 'residual_lumps': 2}   # NIST CTC-01
```

```bash
python3 step_recognize.py part.step --stl out.stl           # recover + write a viewable solid
python3 step_recognize.py part.step --fcstd out.FCStd       # recover + write an editable FreeCAD tree
python3 step_recognize.py part.step --emit out.ir.json      # recover + write the IR
python3 step_recognize.py part.step --iou                   # + Boolean-free IoU against the input
python3 step_recognize.py --selftest                        # generate fixtures, assert (quick CI)
python3 -m pytest tests/                                     # full test suite (emit + recognize)
```

**Checking a recovery independently.** `--iou` (or `iou_check.registered_iou(original, recovered)`
from Python) measures how much of the input the recovered tree actually reproduces, as
intersection-over-union, **without any Boolean operations**: it classifies sampled points against
both solids and reports a 95% interval. Two reasons it exists. The verifier's volume test is
one-sided — over-cut and uncut material can cancel — and IoU is not. And IoU computed from an
OpenCASCADE union is untrustworthy on failed recoveries: on the NIST corpus it returned the larger
operand as the "union", mis-scoring four of eight PARTIAL parts, one by 56 points, while passing
every range check. For a PARTIAL recovery the figure is a lower bound (best of 24 axis-aligned
registrations). On NIST CTC-01: `99.81% ± 0.07`.

### Recovery strategies

The core idea: **most machined/printed parts are a 2D profile swept along an axis** — extruded, or
revolved — **plus holes and pockets.** So recognition is *cross-sectional*, in layers, each proven
before the next is trusted:

1. **Dispatch — extrude vs revolve.** Find the axis the solid is a prismatic extrusion along (every
   face planar-⊥, planar-∥, or a cylinder ∥ to it) or a body of revolution about (rotating it leaves
   it unchanged). Orientation-agnostic: any X/Y/Z or face-normal axis is rotated onto Z.
2. **Section-based outline.** The outline + through-holes come from a *cross-section*, sampled at a
   few heights (not a single end face) so a stepped/fragmented end — or a section landing exactly on
   a feature plane — doesn't derail it. The profile keeps **straight edges *and* circular arcs**.
3. **Through vs blind.** An inner wire is emitted as a through-hole only if it's present near **both**
   faces; a blind pocket (floor below the section) is *not* drilled through — over-cutting can't be
   undone — but deferred to recovery. Through-holes may be **any shape** (circle, slot/obround, poly).
4. **Multi-axis recovery.** When the base extrude doesn't verify, the machined interior is still in
   the **residual** (`recovered − original`), which is carved feature by feature:
   - **(A) floor pockets** — every significant intermediate plane ⊥ the axis is a pocket *floor*;
     cut it (keeping islands/bosses standing via outer-minus-inner-wire profiles);
   - **(B) residual carve loop** — whatever's left breaks into separate lumps, and *each lump is
     itself a 2D profile extruded along **its own** axis* — a through-web pocket ∥ the main axis, a
     **cross-axis hole** ⊥ it. Recognize each and subtract it as a `prism_cut` (a placed
     profile-along-an-axis), looping until the residual vanishes.
5. **Verification-driven axis choice.** Several axes can *look* prismatic (a cross-hole makes its own
   axis look clean), so recognize() gathers a candidate per axis and lets **verification** pick the
   winner: first a base that verifies on its own (the simplest clean extrude), else the first whose
   recovery verifies.
6. **Self-verification (the honesty).** The recovered IR is re-emitted through `b3d_emit` and its
   volume + (rotation-tolerant) bounding box compared to the original. Every result is **VERIFIED**
   (Δvol ≈ 0 — provably the same solid within tolerance, now an editable tree) or **PARTIAL** with
   the residual reported. This is what tames the non-uniqueness of feature recognition: *any*
   decomposition that reconstructs the solid is accepted — geometric equivalence, not a guess at the
   designer's exact operations — and anything that doesn't reconstruct is rejected, never faked.

On the NIST CTC-01 test part (multi-level pockets, through-web windows, 12 cross-holes, 8 chamfers)
this takes a 155%-off base extrude to a **verified** 54-feature tree, and that tree re-emits to the
**identical volume in both build123d *and* FreeCAD** — the recovered part is genuinely editable in
either.

### Drawbacks & limits (surfaced, never silently wrong)

- **Equivalence, not intent.** Verification certifies the recovered solid *matches* within tolerance
  — not that the operations are the ones the designer used. A two-level pocket may come back as
  several `prism_cut`s rather than "ledge + bore"; it's the same geometry, editable, but not
  necessarily the same *history*. Recognition is inference and non-unique in general.
- **Edge fillets/chamfers aren't in the vocabulary** — they're edge blends, not swept profiles — so
  they're left as **sub-tolerance residual**. A chamfered part can verify within tolerance while the
  recovered tree has sharp edges (`recovered.residual_lumps` and a warning disclose it).
- **The reported Δvol is a NET, and it can hide compensating errors.** On NIST CTC-01 the
  recovered solid is *smaller* than the original (−0.139%), so the residual is not just uncut
  chamfers — those would make it *larger*. Decomposing it (`paper/figures/error_decomposition.py`)
  gives **0.139% over-cut** vs **0.0004% uncut**, a cancellation factor of **1.01×** today. An
  earlier recovery of the same part decomposed as 0.130% vs 0.029% — a factor of **1.56×**. Nothing
  bounds that term, and it moved by half its own value under a change that was not aimed at it.
  VERIFIED means "reproduces the part's volume and extent to within tolerance", *not* "is the part
  to within tolerance". A two-sided symmetric-difference check would close this gap.
- **Additive bosses can't be recovered** (recovery only *subtracts* from the outline envelope). A
  raised post on a base is flagged PARTIAL, not faked.
- **Splines / ellipses / lofts / sweeps / freeform → PARTIAL.** No faithful sketch-and-pad tree
  exists; the verifier rejects rather than guess.
- **Pathological tangencies can over-cut.** A cross-hole exactly coincident with a pocket floor
  fragments that floor and the recovery may not close fully — but the verifier catches it and reports
  PARTIAL rather than shipping a wrong tree. It degrades to honest, not to silently-incorrect.
- **Cost.** Recovery runs repeated boolean diffs and per-axis attempts — **seconds** per complex
  part, not milliseconds. It only kicks in when the base doesn't verify (clean prismatic parts are
  fast). Disable with `recognize(..., recover=False)` for the base-only best-effort.

### Bridging IN from build123d (the reverse direction)

build123d bakes operations into a final solid, so you can't *extract* its tree. To bring an existing
build123d part in, author its operations as IR using the **same named constants** your build123d
script uses (profile points, thickness, hole positions), so both paths describe one design —
then `b3d_emit` regenerates an equivalent solid to confirm parity. Or export it to STEP and try
`step_recognize` (above) — it verifies whether the inferred tree actually reproduces the part.
Geometry that's a mesh boolean (no clean sketch/pad) can't be a feature tree — export those as
STEP/STL and import as a single solid, and say so.

## Ingesting cadgen / text-to-cad assemblies — the editable round-trip for agent-generated CAD

[text-to-cad](https://github.com/earthtojake/text-to-cad) ("CAD Skills", the dominant agent-writes-
build123d toolchain) emits, by design, a *dead solid*: a labeled STEP assembly plus a `.step.json`
sidecar carrying the typed mates / gear couplings / poses that STEP itself cannot. `cadgen_ingest.py`
is the editable half it declines to build. It reads the STEP (part labels and the `o1.N` occurrence
tree survive the XCAF import) and the sidecar (mates joined to occurrences **by id and cross-checked
by label** — disagreements are reported, never hidden), recovers each part's editable tree with
`step_recognize` in the part's *own* frame, and returns an **assembly IR** ([`assembly_ir.py`](assembly_ir.py)):
occurrences + per-part feature trees + mates + couplings + poses, plain JSON.

```bash
python3 cadgen_ingest.py model.step                                 # sidecar auto-found: model.step.json
python3 cadgen_ingest.py model.step --sidecar k.step.json --emit out.asm.json
python3 cadgen_ingest.py --package tree_dir                         # a materialized tree (assembly.json)
python3 cadgen_ingest.py --selftest
```

On cadgen's own planetary-gear hero model (9 parts, 8 mates, a gear coupling, 2 poses) **every part
recovers as a VERIFIED editable tree** (gears Δ0.0%, pins Δ0.01%), with all mates joined and labels
agreeing. Two things learned the hard way, both encoded: parts are recognized **re-centred in their
own frame** (an off-origin planet gear was PARTIAL at Δ38% in world coordinates and VERIFIED at Δ0.0%
locally) and the occurrence carries the placement back, **self-checked** by re-placing the part; and
cadgen is not consistent about where placement lives (a gear's is in `.location`, its pin's is baked
into the geometry), so the world bounding-box centre — not `.location` — defines the part frame.
`tests/fixtures/cadgen/` holds genuine cadgen output the tests run against.

## Assemblies: loop closures, posing, MuJoCo and 3MF

The assembly IR ([`assembly_ir.py`](assembly_ir.py)) is a design artifact in its own right, not
just an ingest format. Occurrences place parts; `mates` form a forward-kinematic **tree**; and
**`closures`** close loops (four-bar, parallelogram, slider-crank) between two occurrences that the
tree already connects. Closures live in their own list, so anything that walks `mates`, including a
cadgen export, never sees a cycle.

```python
import assembly_ir as A, assembly_kin as K, mjcf_emit as M, threemf_emit as T

A.closure_problems(asm); A.mobility(asm)        # validation; planar loops counted as planar
q, poses = K.solve(asm, {"crank": 90})          # drive some mates, solve the rest (loops stay closed)
xml = M.emit(asm, "out/", sim)                  # MuJoCo: bodies = fastened groups, joints = mates,
                                                #   closures -> <equality>, geometry = the parts' own meshes
T.emit_assembly(asm, "bot.3mf", pose={"crank": 90})   # 3MF components + the IR embedded in the package
T.emit_objects(plate_objects, "plate.3mf")      # loose build items: a print plate
```

- **`assembly_kin.py`** is a numpy pose solver. It works in displacements from the authored pose,
  uses damped least squares on the closure residuals, and continues from the authored pose so a
  linkage stays on its branch. Tested against the closed-form four-bar to 0.001° over a full crank turn.
- **`mjcf_emit.py`** writes no geometry by hand. Sim-only facts (masses of bought parts, collision
  proxies for concave parts, actuators, joint friction) ride in a small `sim` dict, never in the IR.
  `emit_fragment(..., prefix=)` composes several instances into one scene.
- **`threemf_emit.py`** (lib3mf): 3MF is the manufacturing *output* (meshes, materials, hierarchy),
  never the design source. `read_assembly` round-trips a package for checking.

Tests: `tests/test_assembly_loops.py`, `tests/test_threemf_emit.py`.

## How it runs

| file | runs under | role |
|------|-----------|------|
| `ir.py` | any Python 3 | the DSL / IR — single source of truth, plain JSON-able dicts |
| `assembly_ir.py` | any Python 3 | the ASSEMBLY IR — occurrences + parts + typed mates / couplings / poses (additive to `ir.py`) |
| `cadgen_ingest.py` | host Python 3 | ingest a cadgen / text-to-cad STEP + `.step.json` sidecar (or tree package) → assembly IR, each part a verified editable tree |
| `b3d_emit.py` | host Python 3 | render an IR spec → a build123d Solid (+ `.stl`) in-process, no FreeCAD |
| `step_recognize.py` | host Python 3 | recover an IR from a STEP B-rep (2.5D-prismatic), self-verified by re-emit |
| `gen.py` | host Python 3 | emit an IR spec → `.FCStd` (+ `.stl`); shells out to FreeCAD |
| `roundtrip.py` | host Python 3 | read the tree / params back; optionally apply named edits |
| `runner.py` | host Python 3 | locate / extract `freecadcmd`, run a script under it |
| `fc_build.py` | FreeCAD's Python 3.11 | build the native PartDesign tree |
| `fc_read.py` | FreeCAD's Python 3.11 | read labels / params back out |
| `fc_common.py` | FreeCAD's Python 3.11 | shared FreeCAD-side helpers |
| `onshape_client.py` | host Python 3 | Onshape REST client (HMAC) — create a doc, run FeatureScript |
| `onshape_emit.py` | host Python 3 | emit an IR spec → a native Onshape Part Studio (REST, query-string references) |
| `script_emit.py` | host Python 3 | emit an IR spec → a Fusion or SolidWorks build script (`targets/`) |
| `targets/*.py` | inside Fusion / SolidWorks | the runtime each generated script carries |
| `cad_verify.py` | host Python 3 | check a target's STEP, `.sldprt` or `.f3d` against the IR (IoU + tree) |

Data passes to the FreeCAD-side scripts via **env vars**, never argv — `freecadcmd` treats extra path
arguments as documents to open.

## Onshape backend

The same IR drives **Onshape** as a native Part Studio: Sketch, Extrude, Fillet, Revolve and Plane
features, each named after its IR feature, posted through the REST API (v6 feature JSON). Every
geometric reference is a **FeatureScript query string** that Onshape re-evaluates on each
regeneration, never a stored entity id:

- a profile is `qContainsPoint(qSketchRegion(<sketch>), <interior point>)` for each material region;
- a face-attached sketch plane is the planar Z-normal face farthest along ±Z;
- a fillet's edges are the circular edges containing points found by the same rule the reference uses.

```bash
python3 onshape_emit.py part.ir.json            # new Part Studio in the verification document
python3 onshape_emit.py part.ir.json --verify   # + export STEP and run the IoU gate
python3 onshape_emit.py part.ir.json --trace    # + volume after every feature vs build123d
python3 onshape_emit.py --read <eid> part.ir.json   # edited dimensions back, by feature name
```

**Auth:** the Onshape key pair, from `ONSHAPE_ACCESS_KEY` / `ONSHAPE_SECRET_KEY` or onpy's
`~/.onpy/config.json` (onpy itself is no longer needed).

### Onshape scope / gotchas

- **Working:** sketches (circles, rects, polygons with arcs) on XY, XZ or a top/bottom face; pad
  (blind, symmetric, taper); pocket (through, blind, taper); fillet; revolve; prism_cut on
  axis-aligned planes. On coverage parts every feature's volume matches build123d to three decimals,
  with one unexplained 4.175 mm³ difference on a pocket that crosses a concave arc.
- **Not yet:** polar_pocket and prism_cut on oblique planes (they need rotated construction planes).
- **Feature JSON must go to `/api/v6/...`**; the unversioned API expects an older envelope format.
- **The free plan has a daily API quota** (about 1,500 calls). A large part costs a few hundred calls.
  The client stops, rather than sleeping for a day, when Onshape asks for a wait over ten minutes.
- **Free Onshape accounts can only create public documents.**
- **Sketches arrive unconstrained**; the IR is the source of truth.

## Fusion and SolidWorks backends (untested inside either program so far)

Neither program can be driven headless from Linux, so `script_emit.py` writes a self-contained script
that runs *inside* it: the IR as data, the build123d reference volume after every feature, and a
small runtime in that program's own API. The runtime re-authors each feature natively (named
sketches, extrudes, cuts with draft, revolves, fillets) and resolves the IR's face / edge queries
against the live body, the same way the FreeCAD backend does.

```bash
python3 script_emit.py fusion part.ir.json out/            # -> out/part_fusion/ (a Fusion script folder)
python3 script_emit.py solidworks part.ir.json out/        # -> out/part_solidworks.py (pywin32, Windows)
```

- **Fusion:** Utilities → Add-Ins → Scripts → **+**, pick the folder, Run. Builds a new design
  with a named timeline. Every length, depth, radius and angle is a **user parameter**
  (`ft_<feature>_<key>`). After editing them, the emitted `featuretree_fusion_read` script writes
  them out, and `script_emit.py apply part.ir.json part.fusion.params.json` carries them back into
  the IR by feature name.
- **SolidWorks:** with SolidWorks open, `python part_solidworks.py` (pip install pywin32). It builds
  a new part and saves `.SLDPRT` + `.step`. `--dump` reads the active part's `D1@<feature>`
  dimensions back for `apply`. The IR's +Z is SolidWorks' model +Z, so Z-up parts sit on their back
  in SolidWorks' Y-up views. The geometry is unaffected.

**Verification.** Each script writes `<part>.<target>.report.json`, which lists every feature with
its volume against the reference, so the first feature that disagrees is named. Then:

```bash
python3 cad_verify.py part.fusion.step part.ir.json --report part.fusion.report.json
python3 cad_verify.py part.SLDPRT part.ir.json      # native file, read with cadmpeg
python3 cad_verify.py part.f3d part.ir.json
```

A native `.sldprt` / `.f3d` is read with [cadmpeg](https://github.com/cadmpeg/cadmpeg) (Apache-2.0,
put it on `PATH` or set `CADMPEG`). That checks two things: the geometry cadmpeg decodes from the
saved file goes through the same Boolean-free IoU gate, and the file's stored feature history must
contain each IR feature by name, of the right kind (extrude / cut / revolve / fillet), with the
right blind length or radius. The second check is the editability claim itself.

**Add `--onshape` for the geometry.** It imports the file with Onshape's commercial translators
(`onshape_translate.py`; uses the Onshape key pair above; free plans make the upload **public**).
Onshape's STEP then decides the geometry verdict and cadmpeg's is only reported. We measured both
decoders on NIST's eleven native SolidWorks 2018 test parts against NIST's own STEP of each part:

| decoder | parts matching NIST's STEP | notes |
|---|---|---|
| Onshape (commercial translators) | **11 / 11** | IoU 99.93–100%, volume within 0.17%, same frame |
| cadmpeg 0.6.0 | 1 / 11 | only the single-revolve FTC-11; the rest 0–81% IoU, invalid solids |

cadmpeg's feature decoding is still useful: it recovered each designer's named FeatureManager
tree (`Boss-Extrude2`, holes, fillets, drafts, patterns) with 81 of 85 extrude depths. So the
tree check uses cadmpeg, and the geometry check uses Onshape when it is available. The per-part
numbers, and a comparison of each designer's tree with the tree featuretree recovers, are in
[`paper/figures/native_nist.md`](paper/figures/native_nist.md).

```bash
python3 cad_verify.py part.SLDPRT part.ir.json --onshape   # tree via cadmpeg, geometry via Onshape
python3 cad_verify.py part.SLDPRT --decoders-only          # no IR: do cadmpeg and Onshape agree?
```

**What is tested here and what isn't.** The tests replay every script's geometry (loops, arc
mid-points, nesting, placed-cut frames, the polar-pocket ring) with build123d, independently of
`b3d_emit`, and it matches the reference volume after every feature, including on a recovered NIST
part. The vendor API calls themselves have not run yet. Three conventions are unconfirmed:

- Fusion's taper sign (`TAPER_SIGN`);
- SolidWorks' behaviour with `AddToDB` sketching (`ADD_TO_DB`);
- the direction of a partial-angle revolve.

A wrong convention shows up as a volume mismatch on the first feature that uses it.

## Use without Claude Code

Nothing here depends on Claude Code at runtime. Clone anywhere, point `sys.path` at the directory (or
run `gen.py` / `roundtrip.py` directly), and make sure the FreeCAD AppImage is locatable per
[Requirements](#requirements).

## Scope / honesty

- **Working:** XY / XZ sketches (rect / circle / polygon + profile-with-holes), face-attached sketches
  (circle / rect / poly), Pad, Pocket (through / blind), Revolve, `prism_cut` (a profile extruded
  along an arbitrary axis at an arbitrary location — the primitive multi-axis recovery emits), query
  fillets, polar pocket patterns; parameter round-trip (lengths, radii) by name. FreeCAD *and*
  build123d emit the same geometry from the same IR (verified equal on the recovered NIST tree).
- **STEP → IR recognition:** extrude / revolve in any orientation, arcs, any-shape through-holes, and
  **multi-axis recovery** (floor / through-web pockets + cross-axis holes) — self-verified by re-emit.
  See [STEP → IR](#step--ir-feature-recognition--round-trip-engineering).
- **Onshape backend:** native Sketch / Extrude / Fillet / Revolve / Plane features with every
  reference stored as a FeatureScript query; polar_pocket and oblique prism_cut not yet; sketches
  arrive unconstrained. See [Onshape backend](#onshape-backend).
- **cadgen / text-to-cad ingest:** STEP + sidecar (or tree package) → assembly IR with a verified
  editable tree per part, mates / couplings / poses preserved, placements self-checked. The
  per-part trees emit through `gen.py` today; emitting the *assembly itself* as a native FreeCAD /
  Onshape assembly (a placed link per occurrence, mates as joints) is the next step, not yet built.
- **Deferred:** non-XY/XZ unattached planes, richer edge selectors (by-radius / position / count),
  edge fillet/chamfer *recognition*, other backends (Fusion API / SolidWorks macro).
- A SolidWorks `.SLDPRT` can't be written on Linux — that backend would emit a macro.

## Paper

The method, the NIST evaluation, and an honest account of what it *doesn't* do are written up in:

> **featuretree: Verified Recovery of Editable CAD Feature Trees from STEP, for Open CAD Systems.**
> Dan Newcome, 2026. Preprint, v2. [`10.5281/zenodo.22848792`](https://doi.org/10.5281/zenodo.22848792)
> · [PDF](https://doi.org/10.5281/zenodo.22848792) · [source](paper/main.tex)

The DOI above is the **version-independent concept DOI** and always resolves to the newest version
(currently v2, `10.5281/zenodo.23024833`; v1 was `10.5281/zenodo.22848793`). v2 reports
3 VERIFIED / 8 PARTIAL / 0 REFUSED on the NIST corpus and corrects two numbers from v1 — see the
record's changelog.

The paper makes **no novelty claim** about feature recognition — the technique dates to the 1990s and
is shipped in several commercial kernels. What it contributes is an *open, verified* implementation,
an evaluation on the NIST MBE corpus that reports an explicit per-part **verdict** (with a refusal
class the recogniser will use when geometry falls outside its vocabulary), and a negative result
about volume-based acceptance. The prior-art scan behind that framing is in
[`paper/prior-art/FINDINGS.md`](paper/prior-art/FINDINGS.md).

## Citing

If featuretree is useful in your research or product, please cite it (GitHub also offers
"Cite this repository" from [`CITATION.cff`](CITATION.cff)):

```bibtex
@misc{featuretree-paper-2026,
  author    = {Newcome, Dan},
  title     = {featuretree: Verified Recovery of Editable {CAD} Feature Trees
               from {STEP}, for Open {CAD} Systems},
  year      = {2026},
  publisher = {Zenodo},
  doi       = {10.5281/zenodo.22848792},
  note      = {Preprint}
}
```

To cite the **software** rather than the paper, use the version-independent concept DOI
[`10.5281/zenodo.22848721`](https://doi.org/10.5281/zenodo.22848721), which always resolves to the
latest release:

```bibtex
@software{featuretree-software-2026,
  author    = {Newcome, Dan},
  title     = {featuretree},
  year      = {2026},
  publisher = {Zenodo},
  doi       = {10.5281/zenodo.22848721},
  url       = {https://github.com/punkfab/featuretree}
}
```

## License

[MIT](LICENSE)
