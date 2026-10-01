FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");

// Mounting plate: native Part Studio features driven by variables.
export function build(context is Context, id is Id)
{
    variable(context, "width", 80 * millimeter);
    variable(context, "depth", 50 * millimeter);
    variable(context, "thickness", 6 * millimeter);
    variable(context, "holeDiameter", 5.5 * millimeter);
    variable(context, "holeInset", 8 * millimeter);

    var base = newSketch(context, id + "Plate sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
    skRectangle(base, "outline", { "firstCorner" : vector(-#width / 2, -#depth / 2), "secondCorner" : vector(#width / 2, #depth / 2) });
    skSolve(base);
    extrude(context, id + "Plate", {
        "entities" : qSketchRegion(id + "Plate sketch"),
        "endBound" : BoundingType.BLIND,
        "depth" : #thickness });

    var holes = newSketch(context, id + "Hole sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
    skCircle(holes, "hole", { "center" : vector(-#width / 2 + #holeInset, -#depth / 2 + #holeInset), "radius" : #holeDiameter / 2 });
    skSolve(holes);
    extrude(context, id + "Hole", {
        "entities" : qSketchRegion(id + "Hole sketch"),
        "operationType" : NewBodyOperationType.REMOVE,
        "endBound" : BoundingType.THROUGH_ALL,
        "defaultScope" : false,
        "booleanScope" : qCreatedBy(id + "Plate", EntityType.BODY) });

    mirror(context, id + "Mirror holes X", {
        "patternType" : PatternType.FEATURE,
        "instanceFunction" : [id + "Hole"],
        "mirrorPlane" : qCreatedBy(makeId("Right"), EntityType.FACE) });
    mirror(context, id + "Mirror holes Y", {
        "patternType" : PatternType.FEATURE,
        "instanceFunction" : [id + "Hole", id + "Mirror holes X"],
        "mirrorPlane" : qCreatedBy(makeId("Front"), EntityType.FACE) });

    fillet(context, id + "Corner fillets", {
        "entities" : qParallelEdges(qCreatedBy(id + "Plate", EntityType.EDGE), vector(0, 0, 1)),
        "radius" : 6 * millimeter });
}
