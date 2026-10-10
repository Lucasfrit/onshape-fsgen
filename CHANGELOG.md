# Changelog

## Unreleased

### Added

- **Sketches on part faces.** Pick the face by feature (`qCapEntity`, `qNonCapEntity` + `qParallelPlanes`).
  Coordinates start at the world origin projected onto the face, with the axes of the parallel default plane.
  The push measures Onshape's axes on that face (1 extra call per face sketch) and dimensions every point to the
  origin, so variables move the sketch. Paste export and local builder use the same frame.
  Example: `examples/native/face_sketches.fs`.
- **`--document <Onshape URL>`** (or `FSGEN_DOCUMENT`) on `studio push` and `generate`: build in an existing
  document instead of `fsgen-workspace`.
- **Fit check** (`fsgen studio fit`): moves printed parts into their assembled position, adds real components
  (STEP files, pins, wires) and reports overlaps, hinge and slide motions and snap fits. 0 calls.
- **3D viewer** (`fsgen studio view`): one HTML file with the print layout and the assembly; hide parts, cut
  along X/Y/Z.
- **Print check** in `studio check` / `build`: downward faces flatter than 45° that are not on the bed.
- Local engine: `qCapEntity`, `qNonCapEntity`, `qParallelPlanes`.

### Changed

- Variables now drive every line, polyline and arc point (before: only rectangles, circles and explicit
  dimensions), on default planes, offset planes and part faces.
- Feature patterns use "Reapply features" (`fullFeaturePattern`) by default; set it to `false` to copy geometry.
- A variable change no longer re-sends dimensioned face sketches.
- The spec tells the AI to pick faces by feature: point-based queries are stored as fixed numbers in Onshape and
  lose their face when a variable moves it.
- README: custom features vs. native trees, comparison with the Onshape MCP servers, public example document with
  the prompts and call costs.

### Fixed

- Mirrored or patterned ADD extrudes failed in Onshape with `PATTERN_SWITCH_TO_PER_INSTANCE`.
- Local builder: a query for edges could return the sketch edge instead of the part's edge, so chamfers found no
  edges; fillets and chamfers that OpenCascade refused at meter scale are retried at millimeter scale.

## 0.1.0 (2026-10-01)

First public version: native Onshape feature trees from a description or a script, variables in a Variable
Studio, local test build, cost estimate before every push, paste export (0 calls), threads, STEP → editable tree.
