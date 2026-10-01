# featuretree — launch kit

Copy-paste-ready posts + the demo storyboard. Framing: **the editable round-trip for code- and
AI-generated CAD.** Everything here mirrors the README's honest scope — don't overstate in comments.

Repo: <https://github.com/punkfab/featuretree>

---

## 0. The hero GIF (record this first — it *is* the pitch)

CAD is visual; nothing converts like watching the tree stay live. ~20–30 s, no audio needed, save as
`docs/roundtrip.gif`, then uncomment the hero block at the top of the README.

**Storyboard (the plate sample — reliable, fast):**
1. **Terminal** — `python3 gen.py --sample plate` → `out/plate.FCStd`. *(2s; caption: "code writes the part")*
2. **FreeCAD** — open `plate.FCStd`; the left panel shows the **named tree**: `profile` (Sketch) →
   `body` (Pad) → `holes` (Sketch) → `drill` (Pocket). Hover so the names are legible. *(5s)*
3. **Edit by hand** — double-click `body`, change its length (e.g. 10 → 16 mm); the solid updates
   live. *(5s)*
4. **Round-trip** — save, back to terminal: `python3 roundtrip.py out/plate.FCStd` → the readout shows
   the edited length **read back by name**. *(5s; caption: "your hand-edit is back in the code")*
5. **One source, everywhere** *(optional, 3s)* — `python3 b3d_emit.py --sample plate out/plate.stl`
   (same IR → watertight mesh) to make the "editable tree *and* a solid, no drift" point.

Caption for the loop: *"Author once as code (or have an LLM write it) → native editable FreeCAD tree →
hand-edit → the change reads back by name."*

> For the X thread, a second cut of the **STEP→editable** direction lands hard: import a frozen `.step`
> lump → `step_recognize.py part.step --fcstd out.FCStd` → open it and the recovered pockets/holes are
> now a real editable tree.

---

## 1. Show HN

**Title:** `Show HN: Featuretree – editable FreeCAD/Onshape feature trees from code (and AI CAD)`

**URL:** `https://github.com/punkfab/featuretree`

**First comment (post immediately after submitting):**

> Code-CAD (build123d, CadQuery, OpenSCAD) and now LLM CAD generators are great at producing geometry,
> but the output is a baked solid — open it in a GUI CAD tool and there's no feature tree to edit. Same
> with any STEP/STL: it imports as one frozen lump. If you want to nudge a pad depth or a hole by hand,
> you can't, because the parametric history is gone.
>
> featuretree is my attempt at the missing round-trip. You author a design once as a small neutral
> feature-IR — named, ordered operations (sketch / pad / pocket / revolve / fillet), with edges chosen
> by *query* rather than by unstable kernel edge-ids. An emitter re-authors it in FreeCAD's own
> PartDesign vocabulary with each object's Label set to your feature name, so it opens as a real
> editable tree. Edits you make in FreeCAD read back **by name**, so they survive rebuilds. The same IR
> also drives Onshape (via onpy) and renders to a build123d solid — identical geometry (shared
> OpenCASCADE kernel), so you get the editable tree *and* a watertight mesh from one source, with no
> second model to drift.
>
> There's also a STEP→IR direction: it recovers a feature tree from a dumb B-rep (2.5D-prismatic, plus
> multi-axis — floor/through pockets and cross-axis holes), and every recovery is **self-verified** by
> re-emitting and comparing volume + bounding box to the original — so it's either VERIFIED (provably
> the same solid, now editable) or honestly PARTIAL, never a silently-wrong guess. On the NIST CTC-01
> part it takes a 155%-off base extrude to a verified 51-feature tree.
>
> Honest limits: edge fillets/chamfers aren't recovered (left as sub-tolerance residual); additive
> bosses / splines / lofts come back PARTIAL rather than faked; the native-tree emit needs a FreeCAD
> 1.0 AppImage (driven headless via freecadcmd). MIT.
>
> I'd love feedback from FreeCAD / CadQuery / build123d folks especially — what breaks on your parts?

*Tips: post Tue–Thu ~8–10am ET; be present in the thread all day (HN weights author replies); lead the
title with the concrete thing, not adjectives.*

---

## 2. r/FreeCAD  (and r/cad)

**Title:** `Generate a native, editable PartDesign tree from code — and round-trip your hand-edits back`

**Body:**

> I kept hitting the wall where code-generated or imported CAD lands in FreeCAD as one frozen solid —
> no tree, nothing to edit by operation. So I built **featuretree**: you describe a part once as a
> small neutral "feature IR" (named, ordered ops), and it emits a real **PartDesign tree** —
> Sketch / Pad / Pocket / Revolve / Fillet — with each object's **Label set to your feature name**.
> Open the `.FCStd` and the operations are right there in the tree, editable. The nice part: edits you
> make in FreeCAD read back out **by name**, so they survive a regenerate and flow back to the source.
>
> Two things this crowd might care about specifically:
> - **Edge selection is by query** (re-resolved to a live `EdgeN` on every build), not a stored kernel
>   id — the topological-naming sidestep, so a fillet/pocket doesn't silently grab the wrong edge after
>   a rebuild.
> - **STEP import → editable tree:** it recovers a parametric tree from a frozen B-rep (multi-axis:
>   floor/through pockets + cross-axis holes) and **self-verifies** by re-emitting and checking
>   volume/bbox — so an imported lump becomes editable again, and it tells you honestly when it can only
>   *partially* recover (chamfers, bosses, splines) instead of shipping a wrong tree.
>
> The same IR also drives Onshape and renders a build123d solid from one source. Runs headless via
> `freecadcmd` (FreeCAD 1.0 AppImage). MIT — feedback and broken test parts very welcome:
> <https://github.com/punkfab/featuretree>

*Post the hero GIF inline (Reddit autoplays it). r/FreeCAD is the surest audience — lead here.*

---

## 3. X / Twitter thread  (ride the AI-CAD wave)

1/ AI can write your CAD now. But it hands you a **dead solid** — open it in a real CAD tool and
there's no feature tree to edit. You can't grab a pad and change its depth. 🧵 *(attach the hero GIF)*

2/ featuretree is the missing **round-trip**: author a part once as a small feature-IR (or have an LLM
write it) → it opens as a *native, editable* FreeCAD tree → you hand-edit a sketch → the change reads
back into the code **by name**. Same IR also drives Onshape + build123d.

3/ It even goes backwards: a frozen `.step` → recovers an editable feature tree (pockets, cross-axis
holes), and **self-verifies** by rebuilding and checking the volume matches. Verified or honestly
partial — never a silently-wrong guess. *(attach the STEP→editable cut)*

4/ Open source, MIT. The editability layer the AI-CAD wave is missing. github.com/punkfab/featuretree

*Reply into Zoo / Adam / "vibe manufacturing" threads with tweet 1 + the GIF — that's the distribution.*

---

## 4. build123d / CadQuery communities (Discord + GitHub Discussions)

> If you've wanted to code a part in build123d but then *nudge it by hand* in FreeCAD without losing
> parametricity — featuretree emits the same design as a native editable FreeCAD/Onshape tree *and* a
> build123d solid from one neutral IR (identical geometry, shared OCC kernel), and round-trips your
> FreeCAD edits back by name. MIT: github.com/punkfab/featuretree — curious whether it survives your parts.

---

## Posting checklist
- [ ] record `docs/roundtrip.gif` (+ optional STEP→editable cut), uncomment the README hero block
- [ ] sanity-check the repo lands clean for a first-time cloner (Quickstart runs; README top reads well)
- [ ] r/FreeCAD + FreeCAD forum first (surest), then Show HN (Tue–Thu am), same day
- [ ] X tweet 1 + GIF as a reply into a live AI-CAD thread
- [ ] be present in every thread for the first 24h — replies are the distribution
