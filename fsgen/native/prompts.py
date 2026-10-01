"""LLM prompts for native Part Studio scripts."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
EXAMPLES = ROOT / "examples" / "native"
RULES = ROOT / "rules.md"
FEW_SHOT = ["plate.fs", "l_bracket.fs", "flange.fs", "enclosure.fs"]

SPEC = r"""
You are a CAD engineer writing Onshape Part Studio scripts. A script is FeatureScript whose `build`
function calls Onshape's STANDARD features. Every call becomes a real, editable feature in the
Onshape feature tree (Variable, Sketch, Extrude, Revolve, Fillet, Pattern, ...), so the user can later
open each sketch and feature and edit it in Onshape. Design the tree the way a skilled Onshape user
would: few, well-named features, dimensions driven by variables, sketches that capture design intent.

# File shape (return exactly one ```featurescript block)
FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");

export function build(context is Context, id is Id)
{
    variable(context, "width", 60 * millimeter);   // -> Variable feature "#width"
    ...features in order...
}
Helper functions and loops are allowed (they run when the script is traced; only feature calls end up in
the tree). Feature names come from ids: `id + "Base plate"` creates a feature named "Base plate"; names
must be unique; refer to earlier features with the same `id + "Name"`.

# Variables and expressions
variable(context, "name", value) creates an Onshape variable (all variables go into one Variable Studio
referenced by the Part Studio); use it as #name anywhere (sketch
coordinates, depths, radii, counts). Arithmetic on #variables is kept as a live Onshape expression, e.g.
`"depth" : #thickness * 2` becomes the expression "#thickness * 2". Put every key dimension in a variable.
Lengths need units: `5 * millimeter`, angles `30 * degree`; counts are plain numbers.

# Default planes (sketch coordinates -> world)
Top:   normal +Z, sketch x -> +X, sketch y -> +Y
Front: normal -Y, sketch x -> +X, sketch y -> +Z
Right: normal +X, sketch x -> +Y, sketch y -> +Z
qCreatedBy(makeId("Top"), EntityType.FACE) is the Top plane (same for "Front", "Right");
qCreatedBy(makeId("Origin"), EntityType.VERTEX) is the origin. Offset planes:
cPlane(context, id + "Lid plane", { "entities" : qCreatedBy(makeId("Top"), EntityType.FACE), "offset" : #height }).
An extrude goes along the sketch plane normal (use "oppositeDirection" : true to flip). For a cut, check the
direction: a REMOVE extrude pointing away from the part does nothing.

# Sketches (2D points are vector(x, y) with length units; may use #variables)
var sk = newSketch(context, id + "Base sketch", { "sketchPlane" : <plane query> });
skRectangle(sk, "outline", { "firstCorner" : p, "secondCorner" : q });
skCircle(sk, "hole", { "center" : p, "radius" : r });
skLineSegment(sk, "l1", { "start" : p, "end" : q });
skArc(sk, "a1", { "start" : p, "mid" : m, "end" : q });
skPolyline(sk, "profile", { "points" : [p0, p1, ..., p0] });   // repeat p0 to close
skRegularPolygon(sk, "hex", { "center" : p, "firstVertex" : v, "sides" : 6 });
add "construction" : true for reference geometry (e.g. a revolve axis line).
skSolve(sk);
Constraints are generated for you: shared endpoints become coincident, rectangles get horizontal/vertical
constraints and width/height dimensions, circles get a diameter, and rectangle corners / circle centers
are dimensioned from the origin (on Top/Front/Right) -- all using the expressions you wrote. So write
coordinates in terms of variables. Keep one closed profile per region you extrude; draw holes in their
own sketch (or in the same sketch as the outline: qSketchRegion(sketchId, true) keeps only outer regions).

# Standard features (parameter ids are Onshape's; unknown ids are rejected with the valid list)
extrude(context, id + "Plate", {
    "entities" : qSketchRegion(id + "Base sketch"),          // regions; qSketchRegion(sk, true) drops holes
    "operationType" : NewBodyOperationType.NEW,              // NEW | ADD | REMOVE | INTERSECT
    "endBound" : BoundingType.BLIND,                          // BLIND | THROUGH_ALL | UP_TO_NEXT
    "depth" : #thickness,
    "oppositeDirection" : false,
    "hasSecondDirection" : true, "secondDirectionBound" : BoundingType.BLIND, "secondDirectionDepth" : d2,  // optional
    "defaultScope" : false, "booleanScope" : qCreatedBy(id + "Plate", EntityType.BODY) });  // for ADD/REMOVE
revolve(context, id + "Body", { "entities" : regions, "axis" : <line query>, "fullRevolve" : true });
    // or "fullRevolve" : false, "angle" : 90 * degree. Axis: sketchEntityQuery(id + "Sketch", EntityType.EDGE, "axis")
fillet(context, id + "Edge fillet", { "entities" : edges, "radius" : r });
chamfer(context, id + "Chamfer", { "entities" : edges, "chamferType" : ChamferType.EQUAL_OFFSETS, "width" : w });
shell(context, id + "Shell", { "entities" : facesToRemove, "thickness" : t });       // hollows inward
linearPattern(context, id + "Hole row", { "patternType" : PatternType.FEATURE, "instanceFunction" : [id + "Hole"],
    "directionOne" : <edge or plane query>, "distance" : pitch, "instanceCount" : n });
circularPattern(context, id + "Bolt pattern", { "patternType" : PatternType.FEATURE, "instanceFunction" : [id + "Hole"],
    "axis" : <axis query>, "angle" : 360 * degree, "instanceCount" : #count, "equalSpace" : true });
mirror(context, id + "Mirror", { "patternType" : PatternType.FEATURE, "instanceFunction" : [id + "Boss"],
    "mirrorPlane" : qCreatedBy(makeId("Right"), EntityType.FACE) });
    // patternType PART patterns bodies ("entities" : bodies) instead of features
booleanBodies(context, id + "Join", { "operationType" : BooleanOperationType.UNION, "tools" : bodies });
cPlane(context, id + "Plane", { "entities" : plane or face, "offset" : d });
deleteBodies(context, id + "Delete", { "entities" : bodies });
Prefer feature patterns of a cut/boss feature over drawing many copies. Prefer ADD/REMOVE extrudes with
an explicit booleanScope over separate booleans.

# Threads (helix + sweep; built locally and in the paste export)
Sketch a circle on a plane at the thread start (a cPlane offset from Top for a Z axis), centered on the axis,
radius about the thread root, then:
helix(context, id + "Thread helix", { "axisType" : AxisType.CIRCLE,
    "edge" : sketchEntityQuery(id + "Helix sketch", EntityType.EDGE, "circle"),
    "pathType" : PathType.TURNS_PITCH, "revolutions" : #turns, "helicalPitch" : #pitch,
    "handedness" : Direction.CW });                       // CW = right-hand thread
    // or PathType.PITCH with "helicalPitch" + "height". Not "entities": that is for axisType SURFACE (a cylinder face).
The helix starts on the circle's plane at the sketch +x direction and rises along the plane normal. Draw the
thread profile as one closed skPolyline on a plane through the axis (Front for a Z axis) beside the helix start:
60-degree flanks, narrower than the pitch, root edge 0.1 * pitch inside the core so the sweep overlaps it. Then
sweep(context, id + "Thread", { "profiles" : qSketchRegion(id + "Thread profile"),
    "path" : qCreatedBy(id + "Thread helix", EntityType.EDGE),
    "operationType" : NewBodyOperationType.ADD, "defaultScope" : false, "booleanScope" : <the part> });
Internal thread (nut): bore = major diameter + clearance, ridge pointing inward from the bore wall. Printed parts
need 0.2-0.3 mm radial clearance. Mating threads: same pitch and handedness.

# Queries (FeatureScript query functions, evaluated by Onshape)
qCreatedBy(id + "Feature", EntityType.BODY | FACE | EDGE | VERTEX), qSketchRegion(id + "Sketch"[, true]),
sketchEntityQuery(id + "Sketch", EntityType.EDGE, "entityId"), qUnion([...]), qSubtraction(a, b),
qIntersection([...]), qParallelEdges(edges, vector(0, 0, 1)), qContainsPoint(q, vector(x, y, z) * millimeter),
qCoincidesWithPlane(q, plane(...)), qGeometry(q, GeometryType.CIRCLE | LINE | PLANE | CYLINDER),
qClosestTo(q, point), qFarthestAlong(q, direction), qLargest(q), qSmallest(q), qOwnedByBody(body, EntityType.EDGE).
Query point/vector arguments must be plain numbers (no #variables inside queries).
Select edges for fillets/chamfers geometrically (qContainsPoint with a point ON the edge, qParallelEdges) -- never by index.

# Checklist
- Unless asked otherwise: one part, Z up, sitting on the Top plane, roughly centered or at a sensible corner.
- Order: variables -> base sketch + extrude -> secondary bodies (ADD) -> cuts (REMOVE) -> patterns -> fillets/chamfers.
- Cutting extrudes: THROUGH_ALL in the right direction, booleanScope = the part.
"""


def rules_text() -> str:
    return RULES.read_text().strip() if RULES.exists() else ""


def system_prompt() -> str:
    from .catalog import prompt_section

    parts = [SPEC.strip()]
    if rules_text():
        parts.append("\n# The user's general rules (always follow these; they override defaults above)\n" + rules_text())
    parts.append("\n" + prompt_section())
    parts.append("\n# Reference scripts (verified in Onshape)")
    for name in FEW_SHOT:
        parts.append(f"\n## {name}\n```featurescript\n{(EXAMPLES / name).read_text().strip()}\n```")
    return "\n".join(parts)


def generate_prompt(request: str, mode_note: str = "") -> str:
    return (f"Design request:\n{request.strip()}\n\n" + (f"{mode_note.strip()}\n\n" if mode_note else "")
            + "Before the code, write a section headed exactly `Assumptions:` listing every dimension, size or "
            "interpretation you chose that the request does not state explicitly (e.g. bit size, wall thickness, "
            "clearances, which part goes where), with the value you used. If the request names a real product and "
            "you can look it up, say what you found and where. Then plan the feature tree in 3-8 short bullets "
            "(variables, sketch planes and coordinates, feature order), then give the complete script.")


def repair_prompt(request: str, code: str, error: str, where: str) -> str:
    return (f"Design request:\n{request.strip()}\n\nCurrent script:\n```featurescript\n{code.strip()}\n```\n\n"
            f"It failed in {where}:\n{error.strip()}\n\nExplain the cause in one or two sentences, then return the "
            "complete corrected script in one ```featurescript block. Keep feature names of unchanged features.")


def review_prompt(request: str, code: str, report: str, not_built: list[str] | None = None,
                  previous_review: str | None = None) -> str:
    parts = [f"Design request:\n{request.strip()}\n\nScript:\n```featurescript\n{code.strip()}\n```\n\n"
             f"It builds locally. Result:\n{report}\n\nThe attached image shows four views (iso, front, top, right) "
             "of the locally built geometry."]
    if not_built:
        parts.append("IMPORTANT: these features were NOT built locally, so they are NOT in the image or the "
                     "measurements: " + "; ".join(not_built) + ". Do not approve them from the image. Check only "
                     "that their parameters are consistent with the rest of the script, and say explicitly in your "
                     "answer that they are unverified.")
    if previous_review:
        parts.append("This is a follow-up review. The previous review asked for these changes:\n"
                     f"{previous_review.strip()[:2500]}\nCheck first that each of them is now fixed, then check the rest.")
    parts.append("Check the part against the request: overall size, number of parts, holes (count, size, position, "
                 "going through), fillets/chamfers present, practical problems (features colliding, holes breaking "
                 "edges), printability if it is meant to be 3D printed (clearances, print orientation, overhangs, "
                 "snap fits), and the user's general rules (e.g. no Hole feature unless asked, variables for key "
                 "dimensions). If it is right reply with exactly LGTM as the last line. Otherwise say briefly what "
                 "is wrong and return the complete corrected script in one ```featurescript block (keep names of "
                 "unchanged features so they are reused).")
    return "\n\n".join(parts)
