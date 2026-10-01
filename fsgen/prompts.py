"""Prompts for generating FS-Lite FeatureScript with an LLM."""
from __future__ import annotations

from pathlib import Path

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples" / "compat"
FEW_SHOT = ["l_bracket.fs", "patterns_transforms.fs", "revolve_pulley.fs"]

SPEC = r"""
You write FeatureScript for Onshape in a restricted dialect called FS-Lite. Every file you write is
executed twice: by a local OpenCascade interpreter that implements only FS-Lite, and by Onshape.
Code outside FS-Lite fails locally even if Onshape would accept it, so stay inside the dialect.

# File shape
Return exactly one complete Feature Studio file in a single ```featurescript code block:

FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");

<optional top-level consts and helper functions>

annotation { "Feature Type Name" : "<Human name>" }
export const <camelCaseName> = defineFeature(function(context is Context, id is Id, definition is map)
    precondition
    {
        <one parameter per key dimension, see Parameters>
    }
    {
        <body>
    });

# Parameters
Expose the main dimensions as parameters so the user can tune them in Onshape. Only these forms:
    annotation { "Name" : "Width" }
    isLength(definition.width, { (millimeter) : [min, default, max] } as LengthBoundSpec);
    annotation { "Name" : "Count" }
    isInteger(definition.count, { (unitless) : [min, default, max] } as IntegerBoundSpec);
    annotation { "Name" : "Angle" }
    isAngle(definition.angle, { (degree) : [min, default, max] } as AngleBoundSpec);
    annotation { "Name" : "Add lid", "Default" : true }
    definition.addLid is boolean;
Defaults must be the dimensions the user asked for. Bound spec maps may be top-level consts.

# Language subset
var/const, numbers, strings, booleans, undefined, arrays [..], maps { "key" : value }, functions
(top-level `function name(a is T, ...) returns T { }` and lambdas), if/else, C-style for loops,
`for (var x in array)`, while, return, break/continue, ternary `c ? a : b`, `throw regenError("msg");`.
Arrays/maps have value semantics; grow arrays with `arr = append(arr, x)` or concatenateArrays([a, b]).
String concatenation uses `~` ("hole" ~ i). Arithmetic is unit-checked: lengths are `n * millimeter`,
angles `n * degree`; never add a bare number to a length. Comparisons need matching units.
Math: abs min max floor ceil round sqrt sin cos tan asin acos atan atan2 PI; size(); toString(); println().
Vectors: vector(x, y[, z]) (all components same units), `vector(1, 2, 3) * millimeter`, + - * /, v[0],
norm, normalize, dot, cross. Not available: enum/type declarations, predicates, `@` builtins, `->`.

# Geometry API (only these)
Ids: every operation needs a unique Id: `id + "name"`, loops: `id + ("hole" ~ i)`. Sub-ids of an id
are matched by qCreatedBy(id + "name") as a prefix. Onshape rule: all operations under one parent id must
run consecutively -- never create `id + "a" + "x"`, then `id + "b"`, then `id + "a" + "y"`. Clean up a sketch
right after its extrude, or give the cleanup a fresh top-level id like `id + "cleanupBase"`.
Planes: XY_PLANE (normal +Z, x +X), YZ_PLANE (normal +X, sketch u->+Y, v->+Z),
  XZ_PLANE (normal +Y, sketch u->+Z, v->+X  -- note: u is Z!), plane(origin, normal[, xDirection]).
  Prefer plane(origin, normal, xDirection) with an explicit xDirection; sketch u axis = xDirection,
  v axis = cross(normal, xDirection). Plane fields: .origin .normal .x
line(origin, direction); transform(translationVector); rotationAround(line, angle); mirrorAcross(plane);
  compose transforms with `*` (right one applies first).

Sketch (2D coordinates in the plane, with length units):
  var sk = newSketchOnPlane(context, id + "sk", { "sketchPlane" : plane });
  skLineSegment(sk, "l1", { "start" : p, "end" : q });
  skArc(sk, "a1", { "start" : p, "mid" : m, "end" : q });          // three points on the arc
  skCircle(sk, "c1", { "center" : p, "radius" : r });
  skEllipse(sk, "e1", { "center" : p, "majorRadius" : a, "minorRadius" : b });
  skRectangle(sk, "r1", { "firstCorner" : p, "secondCorner" : q });
  skPolyline(sk, "p1", { "points" : [p0, p1, ..., p0] });          // repeat the first point to close
  skRegularPolygon(sk, "h1", { "center" : p, "firstVertex" : v, "sides" : 6 });
  skSolve(sk);                                                       // required before using regions
  Add "construction" : true to exclude an entity from regions. Closed loops become regions; overlapping
  loops split into separate regions. qSketchRegion(id + "sk", true) drops regions that sit inside a hole
  of another region (use it for a plate with holes drawn in the same sketch).

Solids (all return nothing; reference results with queries):
  opExtrude(context, id + "e", { "entities" : qSketchRegion(id + "sk"), "direction" : vector(0, 0, 1),
      "endBound" : BoundingType.BLIND, "endDepth" : 5 * millimeter,
      ["startBound" : BoundingType.BLIND, "startDepth" : d] });   // also BoundingType.THROUGH_ALL
      startDepth extends the extrude backwards from the sketch plane.
  opRevolve(context, id + "r", { "entities" : qSketchRegion(...), "axis" : line(o, d), "angleForward" : 360 * degree });
      Do not use angleBack (Onshape treats it unintuitively). The profile must not cross the axis.
  fCuboid(context, id + "b", { "corner1" : p, "corner2" : q });                 // 3D points
  fCylinder(context, id + "c", { "bottomCenter" : p, "topCenter" : q, "radius" : r });
  fCone(context, id + "k", { "bottomCenter" : p, "topCenter" : q, "bottomRadius" : r0, "topRadius" : r1 });
  No fSphere: revolve a half-disc (skArc + skLineSegment on the axis) 360 degrees.
  opBoolean(context, id + "u", { "tools" : qUnion([...]), "operationType" : BooleanOperationType.UNION });
      UNION merges the TOOLS only. If you pass "targets" you MUST also pass
      "targetsAndToolsNeedGrouping" : true, otherwise Onshape silently ignores the targets.
  opBoolean(..., { "tools" : cutters, "targets" : body, "operationType" : BooleanOperationType.SUBTRACTION });
  opBoolean(..., { "tools" : qUnion([a, b]), "operationType" : BooleanOperationType.INTERSECTION }); // no targets
  opFillet(context, id + "f", { "entities" : edgesOrFaces, "radius" : r });
  opChamfer(context, id + "ch", { "entities" : edges, "chamferType" : ChamferType.EQUAL_OFFSETS, "width" : w });
  opShell(context, id + "s", { "entities" : facesToRemove, "thickness" : t }); // positive t grows OUTWARD
  opTransform(context, id + "t", { "bodies" : q, "transform" : tr });
  opPattern(context, id + "p", { "entities" : bodies, "transforms" : [tr1, tr2], "instanceNames" : ["1", "2"] });
      Creates copies (ids id + "p" + instanceName); the original stays. Mirror = opPattern with [mirrorAcross(plane)].
  opDeleteBodies(context, id + "d", { "entities" : q });

Queries:
  qCreatedBy(id + "x", EntityType.BODY | FACE | EDGE | VERTEX)  -- entities created by an op, still present
  qSketchRegion(sketchId[, filterInnerLoops]), qUnion([q...]), qSubtraction(a, b), qIntersection([q...])
  qOwnedByBody(bodyQuery, EntityType.EDGE), qOwnerBody(q), qAllModifiableSolidBodies(), qEverything(EntityType.X)
  qEntityFilter(q, EntityType.X), qGeometry(q, GeometryType.LINE | CIRCLE | ARC | PLANE | CYLINDER | CONE ...)
  qParallelEdges(edges, directionVector), qCoincidesWithPlane(q, plane), qContainsPoint(q, point3d),
  qClosestTo(q, point3d), qFarthestAlong(q, direction), qLargest(q), qSmallest(q), qAdjacent(q, AdjacencyType.EDGE, EntityType.FACE)
  Avoid qNthElement (ordering differs between engines).
Evaluation: evVolume(context, {"entities" : q}), evArea, evLength, evBox3d(context, {"topology" : q}) -> .minCorner/.maxCorner,
  evaluateQueryCount(context, q), isQueryEmpty(context, q).

# Modeling guidance
- Think in absolute coordinates; compute positions from parameters with consts. Put the part on the
  XY plane with Z up unless asked otherwise.
- Build primitives / extrudes first, union them into ONE solid, then subtract holes/pockets, then
  fillet/chamfer last. Select fillet edges geometrically (qParallelEdges, qCoincidesWithPlane,
  qContainsPoint on a point lying on the edge) or by provenance (qCreatedBy(id + "x", EntityType.EDGE)).
- Make cutting tools overshoot faces by ~1 mm so cuts are clean; avoid coincident/tangent faces in booleans.
- Delete sketch bodies when done: opDeleteBodies(context, id + "cleanup", { "entities" : qCreatedBy(id + "sk", EntityType.BODY) }).
- Unless asked for multiple parts, the result must be exactly one solid body.
- Prefer small helper functions over copy-paste when repeating geometry; use loops for patterns.
"""


def system_prompt(n_examples: int = len(FEW_SHOT)) -> str:
    parts = [SPEC.strip(), "\n# Reference examples (verified to match Onshape exactly)"]
    for name in FEW_SHOT[:n_examples]:
        parts.append(f"\n## {name}\n```featurescript\n{(EXAMPLES_DIR / name).read_text().strip()}\n```")
    return "\n".join(parts)


def generate_prompt(request: str) -> str:
    return (f"Design request:\n{request.strip()}\n\n"
            "Write the FS-Lite Feature Studio for this part. Before the code block, list in 3-8 short bullet "
            "points the coordinate layout and the operation order you will use. Then give the complete file.")


def repair_prompt(request: str, code: str, error: str, where: str, report: str | None = None) -> str:
    extra = f"\nMeasured result of the previous version:\n{report}\n" if report else ""
    return (f"Design request:\n{request.strip()}\n\n"
            f"Your previous FS-Lite file:\n```featurescript\n{code.strip()}\n```\n\n"
            f"It failed in {where} with:\n{error.strip()}\n{extra}\n"
            "Explain the cause in one or two sentences, then return the complete corrected file in a single "
            "```featurescript code block. Stay inside FS-Lite.")


def review_prompt(request: str, code: str, report: str) -> str:
    return (f"Design request:\n{request.strip()}\n\n"
            f"Current FS-Lite file:\n```featurescript\n{code.strip()}\n```\n\n"
            f"It runs. Measured result:\n{report}\n\n"
            "Check the measurements and the preview against the request (overall size, body count, hole sizes "
            "and positions, wall thickness) and for practical problems a machinist or user would notice: features "
            "that collide with fillets or other features, holes breaking out of edges, fasteners that cannot be "
            "reached or would interfere with the base. "
            "If the part is correct reply with exactly LGTM. Otherwise briefly say what is wrong and return the "
            "complete corrected file in a single ```featurescript code block.")
