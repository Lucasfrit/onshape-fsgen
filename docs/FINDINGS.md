# Findings log

Running log of what we learned while building fsgen (newest sections at the bottom). Each entry says
how it was established. API-call counts refer to the Onshape EDU allocation (2500 calls/year).

## 2026-10-01 — Onshape API basics

- **Auth**: API keys work with HTTP basic auth (`access:secret`); no HMAC signing needed. (tested)
- **Feature Studio compile errors are not exposed by the REST API**: a broken Feature Studio just returns
  empty `featurespecs`. `POST /partstudios/.../featurescript` (evalFeatureScript) does return parse and
  runtime errors with line numbers, so fsgen re-wraps code as a lambda for diagnostics. (tested)
- **Feature errors**: `featureState` only says OK/INFO/WARNING/ERROR; `getFeatureError(context, makeId(fid))`
  via evalFeatureScript gives the reason (e.g. `FILLET_SELECT_EDGES`, faulty parameter). (tested)
- **Exports redirect** to an external storage host; following redirects with credentials would leak the API
  key to that host — fsgen only sends credentials to cad.onshape.com. (code review)
- **Rate limits are per endpoint and daily**: `GET …/features` was locked ~23 h (Retry-After ≈ 84000 s) after an
  unthrottled scan of all documents; the eval endpoint showed ~30 remaining. Lesson: never bulk-scan. (observed)

## Onshape modelling semantics (all measured in Onshape, reproduced by the local engine)

| topic | finding |
|---|---|
| `plane(o, n)` default x | x = normalize(cross(A, n)), A = X if \|n.y\| largest, Z if \|n.x\|, Y if \|n.z\| (ties y > x > z) |
| `XZ_PLANE` | normal +Y, x = +Z |
| default planes (Part Studio) | Top: x +X, y +Y, n +Z · Front: x +X, y +Z, n −Y · Right: x +Y, y +Z, n +X |
| `opBoolean` UNION | ignores `targets` unless `targetsAndToolsNeedGrouping: true` |
| `opBoolean` INTERSECTION | tools only; `targets` → `BOOLEAN_BAD_INPUT` |
| `opRevolve` `angleBack` | sweep runs from +angleBack to angleForward (positive sense, wraps) |
| `opShell` | positive thickness grows outward with sharp corners; the standard Shell *feature* hollows inward |
| `fSphere` | center must be a vertex query |
| operation ids | sub-ids of one parent must be contiguous ("Parent Id … used at two non-contiguous points") |
| in-document custom features | always use the latest Feature Studio code (stale instances keep regenerating) |
| `/boundingboxes` | not tight on curved parts — compare volume/area instead |
| `qParallelEdges` | signature `(edges, directionVector)` |
| omitted feature parameters | not always defaulted via the API (`cPlane` needs `cplaneType`) → send complete sets |
| Variable features | name must be the UI template `###name = #value` + hidden `value` param, else the tree shows "?" |
| Variable Studio | create (1) + setVariables (1) + reference (1); same-document reference must omit `referenceDocumentId` (else HTTP 500) |
| REMOVE extrude pointing away | finishes with INFO, cuts nothing |

User finding: in the user's "Universal Project Box" Feature Studio, `opBoolean` UNION with `targets` leaves the
four bosses as separate bodies (5 parts) — Onshape behaviour, reproduced locally.

## Native feature trees via the API (2026-10-01)

- Each standard feature (sketch, extrude, fillet, …) is one `POST …/features`; queries can be sent as
  FeatureScript text (`queryString`), so scripts can reference earlier features by name. (tested)
- Sketches can carry constraints/dimensions whose values are expressions with `#variables`; changing a variable
  re-solves the sketch (width 60 → 80 mm moved the geometry exactly as predicted). (tested)
- No endpoint adds several features at once → **1 call per feature is the floor** for native trees.
- Part Studio code ("Show code") is read-only: a native tree cannot be pasted in, only built via UI or API.

## API cost accounting (2026-10-01)

- Counted: every 2xx/3xx response, each redirect hop. Free: 4xx/5xx, browser/app use, public App Store apps.
  ([Onshape API limits](https://onshape-public.github.io/docs/auth/limits/))
- Over a quarter of the annual allocation was used on the first day of development (document scan, probes,
  full compat-suite runs, re-uploading example parts). Reconstructed estimate: the same work with the current
  workflow would have cost roughly a third of that.
- Costs now (measured with the ledger / fake server): new Part Studio 1, Variable Studio 3, feature 1 each,
  volume check 1, edit 1–2, STEP export ~4 (was ~14 with 0.5 s polling), STL 1. Pillow block: 14 calls total.
- The cost estimate shown before each push equals the real count (tested against the fake Onshape).

## Local builder (2026-10-01)

- The local OpenCascade builder reproduces Onshape's volume on all 7 reference parts (6 exact, loft +0.0001 %).
- Error policy matters: OpenCascade-only failures must be warnings, otherwise the LLM "fixes" correct code.

## Other tools (2026-10-01)

- **featuretree** (MIT): STEP → feature IR recognition works for prismatic parts with through holes; fillets,
  shells, bosses and revolves come back PARTIAL. Its build123d/Onshape emitters are weaker than ours
  (unconstrained sketches, not checked against Onshape). With a one-line build123d compatibility shim 56/64 of
  its tests pass on build123d 0.13. Our IR → script adapter matches its own rebuild exactly.
- **FeatureScript MCP server** (Onshape Labs): OAuth with the user's account; normal API limits apply; only
  writes Feature Studio custom features. `claude mcp add` is project-scoped by default.
- **Dropbox** integration: translation API can deliver to cloud storage (`cloudStorageAccountId`); not needed
  because local STEP files are free and match Onshape.
- **Pasting code in the Onshape UI is free** (browser use doesn't count). Only Feature Studio code can be
  pasted → a pasted part is one custom feature with a parameter dialog, not a native tree.

## Paste export: free parts via a custom feature (2026-10-01)

- Onshape experts recommend building feature patterns inside custom features from low-level operations
  (opExtrude → opTransform → opBoolean) rather than calling the Pattern feature
  ([forum](https://forum.onshape.com/discussion/21900/how-to-pattern-a-revolve-remove-feature-within-featurescript)).
  The paste export does exactly that; patterns become loops so count parameters still work.
- Checked offline for all 7 reference parts: the generated custom feature gives the same volume as the native
  local build, and stays identical when parameters change (bolt count 6→8, width 80→100 mm, slot count,
  wall thickness). (`tests/test_native_local.py::test_paste_export_matches`)
- Not yet confirmed in Onshape (needs a paste test, which is free): `opLoft` parameter names, negative
  `opShell` thickness for an inward shell, which body keeps its identity in an `opBoolean` UNION when the
  existing part is listed first, camelCase ids with numbers.

- **Confirmed in Onshape by the user (2026-10-01):** pasted custom features from the paste export work and are a
  valid 0-API-call workflow when sketches don't need editing afterwards (changes go through the parameter dialog).
- FeatureScript MCP server: connected and authenticated in Claude Code (user scope); its tools only appear in a
  new conversation, not in the one where it was added.

## FeatureScript MCP server: tool inventory (2026-10-01, from tool descriptions only — no calls made)

The server's own notice: **every tool call makes one or more Onshape REST calls on the user's allocation** (same
pool as API keys / private OAuth apps). None of the 14 tools is documented as free; whether the doc search and
notes tools really touch Onshape is unverified. Their calls go through OAuth, **not through fsgen's client, so
they are missing from `.fsgen_api_ledger.jsonl` and `fsgen usage` undercounts** once the MCP server is used.

| tool | writes to Onshape? | use for fsgen |
|---|---|---|
| `get_api_usage` | no | official used/limit/cycle → could replace the manual baseline in `.env` |
| `whoami` | no | which account/allocation is billed |
| `set_api_allocation` | changes billing account | no (only on explicit request) |
| `search_featurescript_documentation` | no | look up std signatures (e.g. `opLoft` params) when writing prompts/paste export |
| `read_/edit_featurescript_notes` | server-side notes, not documents | duplicate of FINDINGS.md; low value |
| `list_feature_studios`, `get_featurescript` | no | read existing Feature Studios (e.g. the `test` document) |
| `test_featurescript` | no (eval of a lambda) | same as fsgen's evalFeatureScript diagnostics |
| `test_feature` | writes a Feature Studio + adds a trojan feature | compile-checks a whole Feature Studio, which the REST API otherwise hides |
| `create_geometry` | new branched workspace (deleted by default) | builds a pasted custom feature for real; several calls |
| `create_feature_studio`, `put_featurescript` | yes | automate the paste step (but costs calls, while pasting by hand is free) |
| `logout` | — | no |

None of these builds native Sketch/Extrude trees, so the native pipeline is unchanged.

## Using the MCP server in the workflow (2026-10-01)

- The REST API has no endpoint for the user's API usage (checked in the public OpenAPI spec), so the official
  count is only available through the MCP tool `get_api_usage`.
- MCP tools can only be called by Claude in a chat where the server is loaded; the fsgen Python pipeline can't
  call them without its own Onshape OAuth login, and those calls would count too. So they are part of the
  *working* routine (project `CLAUDE.md`), not of fsgen's code: `get_api_usage` → `fsgen usage --set-official N`
  at session start, `search_featurescript_documentation` before changing generated FeatureScript.

## Dropbox (2026-10-01)

- Tested: fsgen copies outputs (script, paste feature, local STEP/STL, render) to `Dropbox/fsgen/<part>/`;
  the user pasted from `Dropbox/fsgen/paste-test/`.
- Not tested: whether Onshape's Dropbox integration lists those files in the Import dialog (UI, free — user
  can check); exporting from Onshape straight to Dropbox via the API (`cloudStorageAccountId`, no endpoint
  found that returns the account id).
- API-cost effect: none worth having. An API export to Dropbox would save at most the download call (~1 of ~4
  for STEP), while local STEP/STL cost 0 calls and match Onshape's volume. Dropbox is for convenience (files
  on other devices / in Onshape's import dialog, input folder for `fsgen recognize`).

## Chat-driven workflow (2026-10-01)

- The user prefers designing parts in a Claude Code chat over the unattended `generate` script. The chat
  model writes `parts/<name>.fs` and uses fsgen as tools (`spec`, `studio build`, `paste`/`push`);
  `/make-part` (project command in `.claude/commands/`) and `CLAUDE.md` describe the loop. Same checks and
  costs as `generate`, but the user can steer between iterations.

## First real request with threads: NEO 04-227 screwdriver (2026-10-01)

- `generate --paste` produced a sensible 3-part design (handle with collet nose, bit nut, end cap) in ~10 min,
  0 Onshape calls. The script checker caught a wrong enum (`HelixType`) and a wrong `helix` parameter; both were
  repaired automatically thanks to the "valid parameters" messages.
- Threads were modelled as `helix` + `sweep`. Neither is implemented in the local builder (geometry shown without
  threads, flagged as partial) nor in the paste export (no paste file). Threads are the first real capability
  gap: needed in the local builder (OCCT helix + sweep) and the paste export (`opHelix`/`opSweep` or a
  thread profile approach) for printable screw-together parts.
- The background LLM has no web access, so product dimensions (bit size, handle) are assumptions — product-based
  parts are better done in a chat (`/make-part`) where the dimensions can be looked up.
- Fixed: paste mode reported a paste file that had not been written.

## Threads: helix + sweep in the local builder and paste export (2026-10-01)

- **Doc search not done**: the `onshape-featurescript` MCP server was not authenticated in this session, so the
  `opHelix`/`opSweep` signatures were not checked with `search_featurescript_documentation`, and whether a doc
  search costs API calls (`get_api_usage` before/after) is still open. Used instead: the helix/sweep feature
  specs in `ref/partstudio_featurespecs.json` (tested: parameter ids, enums, filters, visibility) and the
  std `opHelix` fields as remembered (`direction`, `axisStart`, `startPoint`, `interval` in revolutions,
  `clockwise`, `helicalPitch`, `spiralPitch`) — **unverified**.
- **Helix feature semantics** (from the specs): `axisType` SURFACE (default) takes a cylinder/cone *face* in
  `entities` and has no `height`; CIRCLE takes a circle/arc in `edge`; AXIS takes `axis` + `startRadius`. The
  first screwdriver script passed a sketch circle in `entities` with the default SURFACE type, which the
  `entities` filter (FACE only) rejects, so it would have failed in Onshape. The local builder now reports this as an
  error with the fix (`AxisType.CIRCLE` + `edge`). (specs + tested locally)
- **OCCT sweep tolerance**: `BRepOffsetAPI_MakePipeShell`'s default tolerance (1e-4) is 0.1 mm in FS-Lite's
  metre units. A thread sweep then had volume errors from −0.5 % to +4.8 % (0.5–10 turns), and fusing a thread
  whose profile has a vertex exactly on the core cylinder gave 0 solids. With 1e-7 the error is <2e-6 and the
  fuse works. (tested, `tests/test_native_local.py::test_helix_sweep_is_a_screw_motion`)
- **OCCT profile placement**: MakePipeShell attaches the profile to the nearest point of the spine; for exactly
  one turn both helix ends are equally near and the thread came out one pitch too low (swept backwards). The
  profile is now pinned to the path's first vertex. (tested)
- **Check without Onshape**: a profile swept along a helix is a screw motion, so V = area × centroid radius ×
  swept angle exactly; the local sweep matches to 1e-5. The M12x2 bolt and nut (`examples/native/m12_bolt_nut.fs`)
  screw together locally: no overlap along the right-handed path at 4 positions, ~75 mm³ overlap when half a
  pitch out of phase or turned left-handed; 0.0995 mm flank gap for 0.2 mm radial clearance (expected 0.1).
- **Paste export**: helix → `evCurveDefinition` of the sketch circle (or `evAxis`) + `opHelix`, so it follows
  the parameters; sweep → `opSweep` + the usual boolean; helices are deleted with the sketches. Same volume as
  the native build, also after changing pitch/turns (tested). FS-Lite gained `opHelix`, `opSweep`,
  `evCurveDefinition` (lines, circles), `evAxis`, `sketchEntityQuery`.
- Fixed in passing: the paste export named sketch variables `sk<Name>`, so a sketch called "Circle" shadowed
  `skCircle()` ("Sketch is not callable"). Now `<name>Sk`.
- **Still to confirm in Onshape** (a free paste test of the M12 bolt/nut answers most of it): `opHelix` field
  names; `Direction.CW` = right-hand thread; a CIRCLE helix starts on the circle's x axis (sketch +x) at its
  plane; Onshape's sweep along a helix keeps the profile in the axial plane like a Frenet frame (a
  rotation-minimising frame would twist the profile ~21°/turn at M12x2); where Onshape attaches a profile that
  is not at the path start; `perpendicularVector` as the AXIS start reference. Then add the Onshape volume to
  `ONSHAPE_VOLUMES` in `tests/test_native_local.py`.

## NEO 04-227 screwdriver in chat, with threads (2026-10-01)

- **Product (web lookup)**: NEO 04-227 is a precision set: one two-material handle with a rotating top cap, plus
  7 *double-ended 120 mm* CrMo magnetic blades (SL1.5/2/3, PH000/00/0, T5–T8, H1.5–H3). Shop pages
  (narzedzia.pl, alimex.pl, Amazon listings) give no shank or handle diameter. The background `generate` run
  had assumed short 4 mm hex insert bits; the real blades need a deep bore that stores the unused end
  (`bitDepth` 65 mm → 55 mm out). Shank diameter stays a parameter (default 4 mm, per the user's request).
- **Both review issues fixed by moving the snap socket into the handle**: the handle now prints standing on a
  flat back face (no pin, no flat overhang). The cap carries a split pin (1.2 mm slot, two halves flex) with a
  0.25 mm snap per side over a 4 mm lip; the socket chamber has a 45° ceiling.
- **Fit checks without Onshape** (same method as the M12 test, `fsgen.fslite.geom` booleans on the local build):
  nut moved along the right-handed thread has 0 overlap down to the cone contact; past contact the overlap
  lies only in the collet cone (z 88–95 mm, r ≤ 4.9), i.e. it clamps and the threads stay clear; blade and
  cap overlap 0. Paste feature = native build, also with changed blade diameter / clearance / grip length.
- `parts/neo_screwdriver.fs`: 27 features, 31 calls to push; delivered as paste (`out/paste/neo_screwdriver.fs`).

## Workflow lessons from the screwdriver run (2026-10-01)

Observed in the first complex request (3 printed parts with a thread); fixes planned after the thread work:

1. The reviewer approved threads that were not in the (partial) local preview — it judged the script text.
   Partial builds must be flagged as "not shown, don't approve" and the run marked unverified.
2. Reviewer fixes were built but never re-reviewed (one review round). Need a follow-up review of the changes.
3. Mode-incompatible features (helix/sweep in paste mode) were only detected at the very end (~10 min).
   Check feature support per mode right after the script is written; tell the model the supported set.
4. The unattended LLM silently assumed product dimensions ("4 mm long bits" → 4 mm hex). Require an explicit
   assumptions list; consider allowing web tools in the background `claude -p`.
5. Printability (clearances, orientation, snap fits) was only caught in review → add 3D-printing rules to
   `rules.md` so the designer gets it right first time.
6. Small: output folder named from the prompt instead of `--name`; 15-min LLM timeout should be configurable.
7. Two Claude sessions edited the same files concurrently (thread work vs. repo prep); commit before parallel
   work or keep one development chat at a time.

**Implemented (2026-10-01, all offline, tests in `tests/test_generate_loop.py` with a scripted fake LLM):**
partial builds are listed in the review prompt ("NOT built locally … don't approve them") and the result has a
`verified` flag; up to 2 review rounds, the follow-up checks the previous review's requested fixes; the model is
told which features its delivery mode supports and paste mode rejects others right after the script check;
a required `Assumptions:` section is saved to `assumptions.md` and logged; the background `claude -p` may use
WebSearch/WebFetch (`--no-web` to turn off); 3D-printing rules in `rules.md`; output folder named from `--name`;
LLM timeout `FSGEN_LLM_TIMEOUT` (default 30 min). The git repository now exists; commit before parallel work.

## Confirmed in Onshape from the user's screenshots (2026-10-01)

- Variable features now show as `#name = value` in the feature tree (e.g. `#slotLength = 40 mm`), confirming the
  `###name = #value` name template + hidden `value` parameter fix.
- The fan adapter, enclosure, NEMA 17 mount and flange trees show readable feature names, sketches, extrudes,
  loft, patterns, mirrors and fillets as ordinary Onshape features (one part each).
- Variable Studio mode confirmed in the UI: the pillow block's 10 variables appear in "Pillow block variables",
  referenced by the Part Studio, whose tree then contains only sketches and features (13 features).

## Not tied to Claude (2026-10-01)

- The fsgen tools are model-agnostic: the AI only writes the Part Studio script. Added a generic backend for
  `fsgen generate` (`FSGEN_LLM_COMMAND`, prompt on stdin / answer on stdout: Ollama, Gemini CLI, `llm`, …),
  tested with a stand-in CLI in `tests/test_generate_loop.py`, and `AGENTS.md` with the shared workflow for
  other coding assistants (Codex, Cursor, Copilot, Gemini CLI). Only Claude has actually been used so far.

## Python 3.14 (2026-10-01)

- Newest stable Python is 3.14 (3.14.8). `cadquery-ocp` 8.0.1 ships wheels for cp311–cp314 and build123d 0.13
  requires `<3.15`, so 3.14 is the newest usable version. On Python 3.14.3 (Homebrew) all 71 tests pass and the
  CLI gives identical results; only DeprecationWarnings from `lib3mf` (ctypes `_pack_`, slated to error in 3.19).
  `requires-python` is now `>=3.11,<3.15`. Python 3.15 will need new OCP wheels first.

## Seeing the official API usage without spending calls (2026-10-02)

- Correction to "Using the MCP server in the workflow" above: the MCP tool is not the only way. Onshape's help
  for *My Account → Developer* has a "View your API usage" section with an *Annual API limits for* dropdown
  ([help page](https://cad.onshape.com/help/Content/Plans/my_account_developer.htm)); admins get e-mails at 25,
  50, 75 and 100 % of the limit. Browser use doesn't count, so this is a free way to read the official number
  for `fsgen usage --set-official`. (help page; not yet checked in the UI)
- A forum user reports an undocumented endpoint, `GET /api/v13/metrics/api/summary?startDate=…`, that returns
  usage for personal API keys when opened in a logged-in browser
  ([forum](https://forum.onshape.com/discussion/30053/viewing-personal-api-usage)). Not in the public OpenAPI
  spec. Opened in the browser it should be free; called from fsgen with API keys it would itself count.
  (unverified)
- If you belong to a company or classroom plan, usage is tracked at that level, and keys created under
  *My Account → Developer* may not appear in the company's usage table. (search summary of the help/forum;
  unverified)

## Ferrite clamp and current-clamp coil former (2026-10-08)

- Hinged and snapping parts can't be judged from the 4-view preview: the parts are laid out in print orientation.
  What worked: put the parts in their closed position, add the real component (the user's STEP of the ferrite)
  and measure the overlap volume between them in the closed position, while turning the hinge in steps and over
  the last few degrees, and while sliding parts off. That caught a latch slot that had cut off its own hook (the
  local build was clean, but there was no snap at all). A snap shows as a small overlap (0.1–0.8 mm³) in the
  last 2–4°; turning the wrong way shows as hundreds of mm³ at 90°. Now `fsgen studio fit` with a
  `<script>.fit.toml` (bounding-box pre-check makes it ~7 s for a 7-part clamp; the scratch version took
  minutes). (tested)
- Parts that print in different orientations (a flat shell and a former standing on its end) need a world map
  with the right handedness: if one half's own coordinates are the mirror image of the other's, its print-frame
  map must have determinant −1, or the printed part is mirrored. `fsgen studio fit` rejects mirror placements.
- Local builder: an extrude's end face reuses the sketch's edges (same TShape), and entity equality compared only
  the shape, so `qContainsPoint(qEverything(EntityType.EDGE), …)` deduplicated the solid edge away in favour of the
  sketch edge and the chamfer got no edges. Entities now also compare the owning body (as in Onshape, where sketch
  and part edges are different entities), and selecting only sketch edges says so. (tested, `tests/test_fit.py`)
- OCCT refused a 0.8 mm chamfer on the coil former's end-wall edges with the model in meters (the local builder's
  units) but built it on the same model scaled to millimeters. Fillets and chamfers now retry once at 1000× scale;
  the operation history is chained through both scale copies so later `qCreatedBy` queries still work. Onshape was
  not asked. (tested locally)
- Print check (`fsgen studio check/build`): downward faces flatter than 45° that are not on the bed, per face with
  area, height and size. Exactly-45° chamfers need a 0.5° allowance or meshing noise flags them. (tested)
- Artifacts (claude.ai pages) serve no binary files (`.stl` refused), so the 3D viewer embeds the meshes as base64
  text. `fsgen studio view` writes a single self-contained HTML file. (tested)

## Feedback from an outside test (vertical-cloud-lab/powder-doser PR #178, 2026-10-10)

A Claude agent in another project built a part with fsgen and reported what got in the way. Changes made:

- **Variables did not drive polylines, free lines or arcs.** Only rectangles, circles and explicit `dims` were
  dimensioned; other sketch entities were sent unconstrained, so a variable change in the Variable Studio did not
  move them. Now every vertex of a line/polyline/arc on a default plane, or on an offset plane of one
  (`cPlane` chains are followed back to Top/Front/Right), gets horizontal/vertical distance dimensions to the origin
  (or alignment when the coordinate is 0), using the script's expressions (`#width / 2`, …) where they exist.
  Tested in Onshape with `out/dimtest/dim_test.fs` (polyline base on Top driven by `#width`, tower with lines and
  an arc on an 8 mm offset plane of Right): volume 9542.478 mm³ as built locally; then **only** the Variable
  Studio was changed (width 40 → 50, plateThickness 4 → 6, hingeDrop 5 → 10, the way a user edits it) and
  Onshape gave 13842.478 mm³, exactly the local build with the new values; all features OK, no over-defined
  sketches. 14 calls (5 features, Part Studio + Variable Studio 3, 2 volume checks, 2 status checks, 1 setVariables).
  Sketches on part faces: see the next section.
- **Feature patterns of ADD extrudes failed** (`PATTERN_SWITCH_TO_PER_INSTANCE`) unless the script set
  `fullFeaturePattern`. fsgen now sends `fullFeaturePattern: true` ("Reapply features") for FEATURE patterns by
  default; an explicit `false` in the script wins. (tested on the fake server)
- **Pushing into the user's own document**: `--document <Onshape URL>` (or `FSGEN_DOCUMENT`) on
  `fsgen studio push` and `fsgen generate`. Studio state is now stored per workspace, so the same part name in two
  documents doesn't collide; old state files still work. Resolving a URL costs 0 calls (it is parsed, not
  looked up). (tested, `tests/test_document_target.py`)
- A new document comes with an empty "Part Studio 1". Reusing it would save one call but means renaming it
  (1 call) and guessing which tab is meant; deleting it in the UI is free, so fsgen leaves it.
- When fsgen is worth its calls: importing a STEP costs fewer calls if you only want to look at the part.
  The native tree pays off once someone edits or iterates (a variable change is 0 calls in the UI, a re-push
  1 call per changed feature).

## Sketches on part faces (2026-10-10)

- `evPlane` of a part face: origin = the face's bounding-box centre (loose for curved edges: 21.1 instead of 21 on
  a face with an arc), x-axis = from how the face was made (cap faces: the sketch plane's x; side faces: along the
  extruded edge, e.g. (-0.93, 0.37, 0) on a slanted face; some faces get x = -X or -Y). Not predictable locally.
  (1 call, 8 faces)
- But a sketch on that face (`BTMSketch`, read back with `evOwnerSketchPlane`) has its **origin at the world origin
  projected onto the face** and evPlane's x-axis. The first try converted with evPlane's origin: the slot landed
  half off the block and the side hole cut nothing (INFO); volumes were off by exactly those amounts. (1 call)
- fsgen now defines face sketch coordinates as: projected world origin, x = the parallel default plane's x,
  y = normal × x. The push measures the face's x-axis with evPlane (1 call per face sketch it sends, with
  `rollbackBarIndex` when later features exist, so the face is seen as it is at the sketch's position: worked) and
  turns the sketch; every vertex gets distances to the projected Origin. Dimensioned face sketches count as driven,
  so a variable change doesn't re-send them. The local builder and the paste export use the same frame (paste reads
  the face's plane at run time; checked locally only).
- Test (`examples/native/face_sketches.fs`: sketches on a plate's top face, a block's top and +Y side faces):
  6932.246 mm³ in Onshape vs 6932.245 locally; Variable Studio edit (4 values) → 11058.263, exactly the local
  value; edit back → 6932.246. All features OK.
- **Point-based queries are frozen.** The tracer evaluates query arguments to numbers (`#plateThickness` became
  4.0 in `qCoincidesWithPlane`), and Onshape stores them that way, so a face picked by a point is lost once a
  variable moves it. Pick by feature: `qCapEntity(id + "Base", CapType.END, EntityType.FACE)`,
  `qParallelPlanes(qNonCapEntity(id + "Block", EntityType.FACE), vector(0, 1, 0))` (all three work in Onshape;
  signature of qParallelPlanes confirmed with the MCP doc search). The local engine now has them too.
  `qFarthestAlong` is ambiguous for this: side faces reach the same height as the top face (locally it returned 5).
- Calls: 34 (probe 1, push + variable test 22, sketch-plane probe 1, re-send of the face sketches 8, last variable
  test 2).
- Official usage (MCP `get_api_usage`) was 1166 when fsgen's ledger said 741: about 425 calls came from outside
  fsgen (the MCP server, other sessions or apps). Recalibrated with `fsgen usage --set-official 1166`. Two MCP doc
  searches followed; whether they counted was not measured.

## Where the unlogged calls came from (2026-10-11)

- Onshape's official count (1166) was about 425 above fsgen's ledger. Checked locally, without API calls:
  only 3 Onshape MCP calls were ever made from this machine (all on 2026-10-10), and no other Claude session ran
  Onshape commands. The ledger itself only started on 2026-10-01 at 16:28, minutes after Onshape's "25 %" e-mail
  (697 used). Before that, the first development session made heavy unlogged calls (a scan of the user's
  documents with paging, the compat suite against Onshape, fetching all standard feature specs, two generate runs
  with cloud repairs, pushing every example twice). The e-mail's number most likely lagged behind them.
  Not proven: Onshape's per-day usage (My Account → Developer, free in the browser) would confirm it.
- The keys were never in git history or in any Onshape document (fsgen sends them only as the request's basic
  auth to cad.onshape.com). They were rotated anyway, as a precaution.
- Guard added: notification per run, daily cap across runs, keys in the macOS Keychain with an empty trusted-app
  list (macOS asks on every read, one prompt per run because both keys are stored in one item), and a Claude
  Code hook that logs Onshape MCP tool calls and asks before each one (the MCP server uses OAuth, not the keys).
