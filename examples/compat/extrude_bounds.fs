FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");

// Extrude semantics: startBound/startDepth, THROUGH_ALL cuts, sketches on YZ plane and offset planes.
annotation { "Feature Type Name" : "Extrude bounds" }
export const extrudeBounds = defineFeature(function(context is Context, id is Id, definition is map)
    precondition {}
    {
        var sk = newSketchOnPlane(context, id + "base", { "sketchPlane" : XY_PLANE });
        skRectangle(sk, "r", { "firstCorner" : vector(-30, -20) * millimeter, "secondCorner" : vector(30, 20) * millimeter });
        skSolve(sk);
        opExtrude(context, id + "block", {
            "entities" : qSketchRegion(id + "base"),
            "direction" : vector(0, 0, 1),
            "endBound" : BoundingType.BLIND, "endDepth" : 10 * millimeter,
            "startBound" : BoundingType.BLIND, "startDepth" : 4 * millimeter });

        // through-all slot cut from a sketch on the YZ plane
        var slot = newSketchOnPlane(context, id + "slotSketch", { "sketchPlane" : YZ_PLANE });
        skRectangle(slot, "s", { "firstCorner" : vector(-5, 2) * millimeter, "secondCorner" : vector(5, 6) * millimeter });
        skSolve(slot);
        opExtrude(context, id + "slotTool", {
            "entities" : qSketchRegion(id + "slotSketch"),
            "direction" : vector(1, 0, 0),
            "endBound" : BoundingType.THROUGH_ALL,
            "startBound" : BoundingType.THROUGH_ALL });
        opBoolean(context, id + "cutSlot", {
            "tools" : qCreatedBy(id + "slotTool", EntityType.BODY),
            "targets" : qCreatedBy(id + "block", EntityType.BODY),
            "operationType" : BooleanOperationType.SUBTRACTION });

        // boss on an offset plane, merged
        var top = newSketchOnPlane(context, id + "bossSketch", { "sketchPlane" : plane(vector(15, 0, 10) * millimeter, vector(0, 0, 1)) });
        skCircle(top, "c", { "center" : vector(0, 0) * millimeter, "radius" : 6 * millimeter });
        skSolve(top);
        opExtrude(context, id + "boss", {
            "entities" : qSketchRegion(id + "bossSketch"),
            "direction" : vector(0, 0, 1),
            "endBound" : BoundingType.BLIND, "endDepth" : 8 * millimeter });
        opBoolean(context, id + "join", {
            "tools" : qUnion([qCreatedBy(id + "block", EntityType.BODY), qCreatedBy(id + "boss", EntityType.BODY)]),
            "operationType" : BooleanOperationType.UNION });
        opDeleteBodies(context, id + "cleanup", { "entities" : qUnion([
                qCreatedBy(id + "base", EntityType.BODY), qCreatedBy(id + "slotSketch", EntityType.BODY),
                qCreatedBy(id + "bossSketch", EntityType.BODY)]) });
    });
