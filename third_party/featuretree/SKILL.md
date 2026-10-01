---
name: featuretree
description: >-
  Emit an EDITABLE FreeCAD feature tree (.FCStd with native Sketch/Pad/Pocket/Fillet objects)
  from a small neutral feature-IR, so a CAD design opens in FreeCAD with its operations in the
  left-panel tree — and round-trips human edits back by feature name. Use alongside build123d
  when a part should be inspectable/editable in FreeCAD's parametric tree, not handed off as a
  dead STEP/STL solid (which imports as one frozen lump). Supports rectangle, circle, AND
  arbitrary polygon / profile-with-holes sketches, pads, pockets (through/blind), and
  query-selected fillets. The SAME IR also emits to a native Onshape Part Studio (REST) —
  sketches, pad, pocket, fillet, revolve and axis-aligned placed cuts — so one design drives FreeCAD and Onshape. Requires the FreeCAD AppImage
  (auto-located at /opt, override with FREECAD_APPIMAGE / FREECAD_CMD); the Onshape backend needs
  an Onshape API key + `pip install shapely`.
---

# featuretree — operation-preserving CAD round-trip

A neutral *file* (STEP/STL) loses the parametric feature tree — it imports into FreeCAD as one
frozen solid you can't edit by operation. This skill keeps the tree: you author the design once
as a small **feature IR** (named, ordered operations), and an emitter re-authors it in FreeCAD's
**own** feature vocabulary (`PartDesign::Sketch/Pad/Pocket/Fillet`), each object's `Label` = your
feature name. Open the `.FCStd` and the operations are on the left, editable. Human edits read
back **by name**, so they survive rebuilds.

```
   ir.py  (the DSL, your single source of truth)        gen.py  ── emit ──►  <part>.FCStd
   named features · named params · symbolic geometry                         (native tree,
   queries (no kernel edge ids)                          roundtrip.py ◄─ read ─ human edits, by name)
```

## When to use
- You're doing CAD (often authoring geometry in build123d for sims/STLs) and *also* want a
  FreeCAD file the user can open and edit operation-by-operation.
- The user asks for FreeCAD / `.FCStd` files, "see the tree", "edit in FreeCAD", or round-tripping.
- NOT for a one-off STL/STEP where no downstream editing is needed — just export from build123d.

## Quickstart

```bash
S=~/.claude/skills/featuretree
python3 $S/gen.py --sample plate         # built-in: 40x30x10 plate + hole  -> $S/out/plate.FCStd
python3 $S/gen.py --sample poly          # exercises the polygon-profile primitive
python3 $S/gen.py mypart.ir.json out/mypart.FCStd     # emit a spec you authored
python3 $S/roundtrip.py out/mypart.FCStd              # read the tree/params back
python3 $S/roundtrip.py out/mypart.FCStd edits.json   # apply a named edit, re-save, report
python3 $S/b3d_emit.py --sample plate out/plate.stl   # SAME IR -> build123d solid (no FreeCAD)
```

Authoring a part (Python, using the DSL — produce a JSON-able dict, write it, emit it):

```python
import sys; sys.path.insert(0, "/home/<you>/.claude/skills/featuretree")
import ir, json
spec = ir.part("bracket",
    ir.sketch("profile", polys=[[(0,0),(40,0),(40,14),(16,14),(16,30),(0,30)]]),  # outer wire
    ir.pad("body", "profile", length=6),
    ir.sketch("holes", circles=[(8,8,2.6)]),
    ir.pocket("drill", "holes", through=True),
)
json.dump(spec, open("bracket.ir.json","w"))            # then: python3 gen.py bracket.ir.json
```

## The IR (see `ir.py`)
- `sketch(name, plane="XY", circles=[(cx,cy,r)], rects=[(w,h,cx,cy)], polys=[wire,...], on=None)`
  — `polys`: first wire = outer profile, following wires = holes (one sketch → one pad gives a
  plate with holes). `on={"face_of": feat, "side": "top"|"bottom"}` attaches the sketch to a face
  chosen by **query** (circles only, v0).
- `pad(name, sketch, length, symmetric=False)` · `pocket(name, sketch, through=True, length=None)`
- `fillet(name, radius, select={"circles":"top_outer"})` — edges chosen by **query**, re-resolved
  to live `EdgeN` every build (never a stored kernel id — the topological-naming sidestep).
- `part(name, *features)` → the spec. `update_from_freecad(spec, params)` flows read-back edits in.

## build123d backend
The same IR also renders to a **build123d Solid** in-process (`b3d_emit.emit(spec) -> (part, res)`),
no FreeCAD. Same OCCT kernel, so identical geometry — the `plate` sample is 11497.3 mm³ from both.
One IR → the editable tree AND a watertight solid to mesh/sim/interference-check; no second model.

## STEP → IR (feature recognition)
`step_recognize.recognize(step_path) -> (spec, report)` INFERS an IR from a dumb STEP B-rep for the
2.5D-prismatic class (planar base → pad, concave Z-cylinders → through/blind pockets). Self-verified:
the IR is re-emitted via `b3d_emit` and volume/bbox compared to the STEP — `report["verified"]` is
True (Δ≈0) or it's flagged PARTIAL (fall back to a single solid). Out of scope: fillets, bosses,
revolves/lofts/sweeps/freeform (rejected, never faked). `python3 step_recognize.py f.step --emit x.ir.json`.

## Bridging IN from build123d (reverse)
build123d bakes operations into a final solid, so you can't extract its "tree". Instead, author
the part's operations as IR using the **same named constants** the build123d script uses (profile
points, thickness, hole positions), so both paths describe one design. Extracted/complex 2D
profiles go in as `polys`. Geometry that's a mesh boolean (no clean sketch/pad) can't be a feature
tree — export those as STEP/STL and import as a single solid, and say so.

## How it runs
FreeCAD is driven head-less through its **own** Python via `freecadcmd` (`runner.py` finds or
one-time-extracts the AppImage; reuses an already-extracted cache). Data passes via env vars, never
argv (freecadcmd opens path args as documents). `gen.py`/`roundtrip.py` run on the project
interpreter and shell out; `fc_build.py`/`fc_read.py`/`fc_common.py` run inside FreeCAD.

## Scope / honesty
- Working: XY sketches (rect/circle/polygon + profile-with-holes), face-attached circle sketches,
  Pad, Pocket (through/blind), query fillets; parameter round-trip (lengths, radii) by name.
- Deferred: non-XY unattached planes, polygon/rect face-attached sketches, richer edge selectors
  (by-radius/position/count), other backends (Onshape FeatureScript / Fusion / SolidWorks macro).
- A SolidWorks `.SLDPRT` can't be written on Linux — that backend would emit a macro.
