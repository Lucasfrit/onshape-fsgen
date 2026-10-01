FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");

// L-bracket from one profile sketch on the Front plane, extruded symmetrically,
// with a hole in each leg (sketches on an offset plane and on Top), and an inside fillet.
export function build(context is Context, id is Id)
{
    variable(context, "width", 50 * millimeter);      // along Y
    variable(context, "baseLength", 60 * millimeter); // along X
    variable(context, "height", 45 * millimeter);     // along Z
    variable(context, "thickness", 5 * millimeter);
    variable(context, "holeDiameter", 5.5 * millimeter);

    // Front plane: sketch x -> world X, sketch y -> world Z (normal -Y)
    var profile = newSketch(context, id + "Profile sketch", { "sketchPlane" : qCreatedBy(makeId("Front"), EntityType.FACE) });
    skPolyline(profile, "L", { "points" : [
            vector(0 * millimeter, 0 * millimeter), vector(#baseLength, 0 * millimeter), vector(#baseLength, #thickness),
            vector(#thickness, #thickness), vector(#thickness, #height), vector(0 * millimeter, #height),
            vector(0 * millimeter, 0 * millimeter)] });
    skSolve(profile);
    extrude(context, id + "Bracket", {
        "entities" : qSketchRegion(id + "Profile sketch"),
        "endBound" : BoundingType.BLIND,
        "depth" : #width / 2,
        "hasSecondDirection" : true,
        "secondDirectionBound" : BoundingType.BLIND,
        "secondDirectionDepth" : #width / 2 });

    // hole through the base (sketch on Top, cut upward through the base)
    var baseHole = newSketch(context, id + "Base hole sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
    skCircle(baseHole, "hole", { "center" : vector((#baseLength + #thickness) / 2, 0 * millimeter), "radius" : #holeDiameter / 2 });
    skSolve(baseHole);
    extrude(context, id + "Base hole", {
        "entities" : qSketchRegion(id + "Base hole sketch"),
        "operationType" : NewBodyOperationType.REMOVE,
        "endBound" : BoundingType.THROUGH_ALL,
        "defaultScope" : false,
        "booleanScope" : qCreatedBy(id + "Bracket", EntityType.BODY) });

    // hole through the upright: sketch on the Right plane (x -> world Y, y -> world Z), cut along +X
    var upHole = newSketch(context, id + "Upright hole sketch", { "sketchPlane" : qCreatedBy(makeId("Right"), EntityType.FACE) });
    skCircle(upHole, "hole", { "center" : vector(0 * millimeter, (#height + #thickness) / 2), "radius" : #holeDiameter / 2 });
    skSolve(upHole);
    extrude(context, id + "Upright hole", {
        "entities" : qSketchRegion(id + "Upright hole sketch"),
        "operationType" : NewBodyOperationType.REMOVE,
        "endBound" : BoundingType.THROUGH_ALL,
        "defaultScope" : false,
        "booleanScope" : qCreatedBy(id + "Bracket", EntityType.BODY) });

    // inside corner: the Y-parallel edge through (thickness, 0, thickness)
    fillet(context, id + "Inside fillet", {
        "entities" : qContainsPoint(qParallelEdges(qCreatedBy(id + "Bracket", EntityType.EDGE), vector(0, 1, 0)),
                vector(5, 0, 5) * millimeter),
        "radius" : 3 * millimeter });
}
