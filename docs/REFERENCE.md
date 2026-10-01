# fsgen reference

Detailed reference moved from the README: API budget, rules, feature catalog workflow, LLM backend, all
commands, the FS-Lite language, and Onshape behaviours measured while building fsgen.

## Onshape API budget

The EDU plan allows **2500 counted calls per year**. Onshape counts every 2xx/3xx response (each redirect hop
too); 4xx/5xx, browser use and public App Store apps are free ([Onshape API limits](https://onshape-public.github.io/docs/auth/limits/)).

- Every call is logged in `.fsgen_api_ledger.jsonl`; each command ends with `Onshape API calls this run: N`.
- `fsgen usage` shows used/remaining vs. the allocation; record Onshape's official count with
  `fsgen usage --set-official N` (e.g. from the FeatureScript MCP server's `get_api_usage`).
- `ONSHAPE_RUN_BUDGET` (default 60) or `--budget N` stops a run before it overspends.
- `tests/test_api_cost.py` runs the real client against a fake Onshape (`fsgen/costmodel.py`) and fails if a
  workflow gets more expensive. Use the fake for development, not the real API.

| operation | calls |
|---|---|
| new Part Studio | 1 |
| each feature (variable, sketch, extrude, …) | 1 |
| re-push unchanged script | 0 |
| re-push after editing a dimension/variable | 1 per changed feature + 1 status check |
| feature fails in Onshape | +1 (diagnosis); the fix updates it in place (1) |
| metrics (parts, volume, area) / Onshape render | 1 / 1 |
| export via API: STL / STEP | 1 / ~4 (or free from the Onshape UI) |

`generate` is **local-first**: the script is checked, built, repaired and reviewed with the local builder
(`fsgen/native/local.py`, matches Onshape's volume on all 7 reference parts), then pushed to Onshape once and
confirmed with one massproperties call. Variables go into one Variable Studio (3 calls for any number;
`FSGEN_VARIABLES=tree` puts them in the feature tree instead, 1 call each). Measured: the pillow block
(9 features, 5 variables) cost 14 calls in total. Exports are opt-in (`--export step,stl`); local STEP/STL
are always written.

## General rules (`rules.md`)

`rules.md` holds the modelling rules, one instruction per bullet (e.g. *no Hole feature unless asked; holes are
sketch + extrude REMOVE*). It's sent with every request and checked in the review step. Edit it freely.

## Which features are available, and how new ones get added

`fsgen features` shows the catalog; `fsgen features <type>` shows one feature with its settable parameters.

| status | meaning |
|---|---|
| **verified** | used in a reference example in `examples/native/` that builds cleanly in Onshape; documented in the prompt with notes (`fsgen/native/catalog.json`) |
| **learned** | built successfully in Onshape during a `generate`/`studio push` run; the working call is saved in the catalog and shown to the model as an example from then on |
| **available** | present in Onshape's feature specs (all 98 standard features). The checker validates parameter ids, enum values and units offline, so the model can try it; success promotes it to *learned*, and failures are saved as notes |

Currently verified: Variable, Sketch, Extrude, Revolve, Fillet, Chamfer, Plane, Circular pattern, Mirror, Shell.

**Learning on the way (automatic):** the model may use any available feature. If Onshape builds it, it becomes
*learned*. If it fails, the Onshape error goes back to the model for a repair and is stored as a note.

**Promoting to verified (manual, when a feature matters):**
1. `fsgen features <type>` to see its parameters (and learned example/failures).
2. Write or copy a small script in `examples/native/` that uses it the way you want.
3. `fsgen studio push examples/native/<file>.fs --fresh --export` and check the part and tree in Onshape.
4. Add an entry with status `verified` and notes to `fsgen/native/catalog.json`; if it's a core feature, add it to
   the spec in `fsgen/native/prompts.py` (or to `FEW_SHOT`).

Parameter types the translator can set: enum, quantity (expressions with `#vars`), boolean, string, query,
feature list and arrays (e.g. loft profiles). Lookup tables (Hole standards), reference tables and part-studio
references stay at their Onshape defaults. `fsgen features --refresh` re-downloads the specs after Onshape updates.

## LLM backend

`fsgen generate` picks the LLM in this order:

1. `FSGEN_LLM_COMMAND` / `--backend command --llm-command "…"`: any LLM with a command-line tool (prompt on
   stdin, answer on stdout), e.g. `ollama run qwen2.5-coder:32b`, `gemini -p`, `llm -m <model>`.
2. `ANTHROPIC_API_KEY` (or an `ant auth login` profile): the Anthropic SDK with `claude-opus-5-5` (streaming,
   adaptive thinking, prompt caching, server-side refusal fallback); the review sees the preview image.
3. Otherwise the logged-in `claude` CLI (`claude -p`, no API key needed); it may use web search
   (`--no-web` to turn off) and reads the preview image with its Read tool.

In a chat, any coding assistant can be the designer instead: see `AGENTS.md`.

## Commands

| command | what it does |
|---|---|
| `fsgen generate "<request>" [--name N] [--paste] [--reviews 2] [--no-web]` | **native**: prompt → script → Onshape feature tree → repair/review → `out/native/<run>/` (script, STEP/STL, Onshape renders, transcripts) |
| `/make-part <description>` (in a Claude Code chat) | the chat designs `parts/<name>.fs` with the commands below; you steer, then paste or push |
| `fsgen studio paste script.fs` | custom feature to paste into a Feature Studio (0 calls; clipboard + optional Dropbox copy) |
| `fsgen recognize part.step` | STEP → editable Part Studio script via featuretree recognition (0 calls) |
| `fsgen usage [--set-official N]` | API calls used/left |
| `fsgen spec` | print the instructions/spec the designer follows (rules, catalog, examples) |
| `fsgen studio check script.fs` | trace + build locally (0 calls): features, geometry report, errors |
| `fsgen studio build script.fs` | local build + STEP/STL/PNG in `out/<name>/` (0 calls) |
| `fsgen studio push script.fs [--name N] [--fresh] [--export]` | build/update the Part Studio incrementally (only from the first changed feature) |
| `fsgen features [type] [--all] [--refresh]` | feature catalog / one feature's settable parameters |
| `fsgen generate --custom "<request>" [--cloud]` | older single-custom-feature mode with local OCCT build/repair/review |
| `fsgen run part.fs [-p name=value] [--png]` | local build + STEP/STL (+ 4-view PNG) |
| `fsgen watch part.fs` | rebuild on save, live three.js viewer at http://127.0.0.1:8765 |
| `fsgen push part.fs [-p ...]` | upload to Onshape, regenerate, diff against local, export (`--formats step,stl,x_t`) |
| `fsgen eval part.fs` | Onshape error messages with source line numbers |
| `fsgen compare examples/compat/*.fs` | local engine vs Onshape on volume / area / body count / bbox |
| `pytest` | language tests + compat regression against Onshape-measured golden values |

The Onshape side uses one document (`fsgen-workspace`, id stored in `.fsgen_workspace.json`) with a Feature
Studio `Gen`, a Part Studio holding a single instance of the generated feature, and an empty `Eval` Part Studio.

## What FS-Lite supports

See `fsgen/prompts.py` (the spec the LLM gets) for the full list. In short:

- **Language:** var/const, functions and lambdas, if/for/for-in/while, arrays and maps with value semantics,
  unit-checked arithmetic (`millimeter`, `degree`, …), vectors, `~` string concatenation, `throw`/`try`.
- **Parameters:** `isLength` / `isInteger` / `isAngle` / `isReal` with bound specs, and boolean/string
  parameters. Their defaults drive local runs, and they're sent as feature parameters on push.
- **Sketch:** `newSketchOnPlane`, `skLineSegment`, `skArc`, `skCircle`, `skEllipse`, `skRectangle`,
  `skPolyline`, `skRegularPolygon`, `skSolve`, regions with `filterInnerLoops`.
- **Ops:** `opExtrude` (BLIND / THROUGH_ALL, start bound), `opRevolve`, `opBoolean`, `opFillet`,
  `opChamfer` (equal offsets), `opShell`, `opTransform`, `opPattern`, `opDeleteBodies`, `fCuboid`,
  `fCylinder`, `fCone`.
- **Queries:** `qCreatedBy` with provenance tracked through booleans and fillets via OCCT history,
  `qSketchRegion`, `qUnion`/`qSubtraction`/`qIntersection`, `qOwnedByBody`, `qGeometry`, `qParallelEdges`,
  `qCoincidesWithPlane`, `qContainsPoint`, `qClosestTo`, `qFarthestAlong`, `qLargest`/`qSmallest`,
  `qAdjacent`, `evVolume` / `evArea` / `evBox3d`, and more.
- **Not supported (yet):** sweep, helix, draft, UP_TO bounds, sketch constraints, `extrude()`-style
  std features, enums/type declarations, other Part Studios, and non-planar `newSketch`.

## Onshape behaviours measured while building this (all reproduced locally)

| topic | finding |
|---|---|
| `plane(o, n)` default x | x = normalize(cross(A, n)): A = X if \|n.y\| is largest, Z if \|n.x\|, Y if \|n.z\| (ties y > x > z) |
| `XZ_PLANE` | normal +Y, **x = +Z** (sketch u → world Z, v → world X) |
| `opBoolean` UNION | `targets` are **ignored** unless `"targetsAndToolsNeedGrouping" : true`; only tools merge |
| `opBoolean` INTERSECTION | tools only; passing `targets` → `BOOLEAN_BAD_INPUT` |
| `opRevolve` `angleBack` | sweep runs from **+angleBack** to angleForward (positive sense, wraps): (90°, 30°) gives a 60° wedge |
| `opShell` | positive thickness grows **outward** with sharp corners |
| `fSphere` | `center` must be a vertex Query, not a point |
| Feature Studio compile errors | not exposed by the REST API (empty `featurespecs` only). `evalFeatureScript` returns messages + lines, so `fsgen` rebuilds the feature body as a lambda for diagnostics |
| Variable feature names | the UI names them with the template `###name = #value` and fills a hidden `value` parameter; a literal name like `#width` shows as "?" in the tree |
| omitted feature parameters | not always defaulted by the API: `cPlane` without `cplaneType` fails (`REGEN_ERROR`). fsgen sends complete parameter sets |
| feature errors | featureState has no message; `getFeatureError(context, makeId(fid))` via evalFeatureScript gives e.g. `FILLET_SELECT_EDGES` + faulty parameters |
| default planes | Top: x→+X, y→+Y (normal +Z); Front: x→+X, y→+Z (normal −Y); Right: x→+Y, y→+Z (normal +X) |
| `qParallelEdges` | signature is `(edges, directionVector)` |
| operation ids | all ops under one parent id must be contiguous ("Parent Id … used at two non-contiguous points") — enforced locally |
| in-document custom features | always use the latest Feature Studio code: a stale feature instance keeps generating geometry |
| `/boundingboxes` | not tight on curved geometry (use volume/area for comparisons) |
| API quotas | per endpoint and daily (EDU account): `GET …/features` was exhausted for ~24 h after a bulk scan; eval had ~30 calls left. Local first, cloud on demand |

Compat suite status: all 11 files in `examples/compat/` match Onshape (body count, volume and area to
1e-4 relative, bbox enclosed), including two real-world Feature Studios.

> Note on `examples/compat/user_project_box.fs` (a real-world Feature Studio): its `joinBosses` union passes the
> box as `targets` without `targetsAndToolsNeedGrouping`, so in Onshape the four bosses stay **separate
> bodies** (5 parts) and the screw holes only cut the box. The local engine reproduces this and prints a warning.

