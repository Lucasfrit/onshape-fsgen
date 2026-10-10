FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");

// Sketches on part faces, driven by variables (verified in Onshape 2026-10-10). Face sketch coordinates: origin =
// the world origin projected onto the face, axes of the parallel default plane, y = normal x x. Faces are picked by
// feature (qCapEntity, qNonCapEntity + qParallelPlanes), so they are still found when variables move them.
export function build(context is Context, id is Id)
{
    variable(context, "width", 40 * millimeter);
    variable(context, "plateThickness", 4 * millimeter);
    variable(context, "pocketOffset", 8 * millimeter);
    variable(context, "holeDia", 6 * millimeter);

    var base = newSketch(context, id + "Base sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
    skRectangle(base, "plate", { "firstCorner" : vector(-#width / 2, -15 * millimeter), "secondCorner" : vector(#width / 2, 15 * millimeter) });
    skSolve(base);
    extrude(context, id + "Base", { "entities" : qSketchRegion(id + "Base sketch"), "endBound" : BoundingType.BLIND,
            "depth" : #plateThickness });

    // top face of the plate: Top's axes, origin above the world origin
    var top = newSketch(context, id + "Top face sketch", { "sketchPlane" : qCapEntity(id + "Base", CapType.END, EntityType.FACE) });
    skCircle(top, "hole", { "center" : vector(#width / 2 - #pocketOffset, 0 * millimeter), "radius" : #holeDia / 2 });
    skPolyline(top, "pocket", { "points" : [
            vector(-#width / 2 + 4 * millimeter, -5 * millimeter), vector(-#width / 2 + 11 * millimeter, -5 * millimeter),
            vector(-#width / 2 + 4 * millimeter, 5 * millimeter), vector(-#width / 2 + 4 * millimeter, -5 * millimeter)] });
    skSolve(top);
    extrude(context, id + "Pockets", { "entities" : qSketchRegion(id + "Top face sketch"), "operationType" : NewBodyOperationType.REMOVE,
            "endBound" : BoundingType.BLIND, "depth" : 2 * millimeter, "oppositeDirection" : true, "defaultScope" : false,
            "booleanScope" : qCreatedBy(id + "Base", EntityType.BODY) });

    // block on the Right plane (sketch x -> world +Y, y -> world +Z), extruded along +X
    var blk = newSketch(context, id + "Block sketch", { "sketchPlane" : qCreatedBy(makeId("Right"), EntityType.FACE) });
    skRectangle(blk, "block", { "firstCorner" : vector(-10 * millimeter, #plateThickness), "secondCorner" : vector(10 * millimeter, #plateThickness + 15 * millimeter) });
    skSolve(blk);
    extrude(context, id + "Block", { "entities" : qSketchRegion(id + "Block sketch"), "operationType" : NewBodyOperationType.ADD,
            "endBound" : BoundingType.BLIND, "depth" : 8 * millimeter, "defaultScope" : false,
            "booleanScope" : qCreatedBy(id + "Base", EntityType.BODY) });

    // block top: a side face of the block extrude, selected by its normal
    var btop = newSketch(context, id + "Block top sketch", { "sketchPlane" : qParallelPlanes(qNonCapEntity(id + "Block", EntityType.FACE), vector(0, 0, 1)) });
    skRectangle(btop, "slot", { "firstCorner" : vector(2 * millimeter, -#width / 8), "secondCorner" : vector(6 * millimeter, #width / 8) });
    skSolve(btop);
    extrude(context, id + "Slot", { "entities" : qSketchRegion(id + "Block top sketch"), "operationType" : NewBodyOperationType.REMOVE,
            "endBound" : BoundingType.BLIND, "depth" : 3 * millimeter, "oppositeDirection" : true, "defaultScope" : false,
            "booleanScope" : qCreatedBy(id + "Base", EntityType.BODY) });

    // block side (normal +Y): x = world +X, y = normal x x = world -Z (Onshape's own x-axis here is -X; the push turns it)
    var side = newSketch(context, id + "Block side sketch", { "sketchPlane" : qParallelPlanes(qNonCapEntity(id + "Block", EntityType.FACE), vector(0, 1, 0)) });
    skCircle(side, "pin", { "center" : vector(4 * millimeter, -(#plateThickness + 7.5 * millimeter)), "radius" : 1.5 * millimeter });
    skSolve(side);
    extrude(context, id + "Side hole", { "entities" : qSketchRegion(id + "Block side sketch"), "operationType" : NewBodyOperationType.REMOVE,
            "endBound" : BoundingType.BLIND, "depth" : 3 * millimeter, "oppositeDirection" : true, "defaultScope" : false,
            "booleanScope" : qCreatedBy(id + "Base", EntityType.BODY) });
}
