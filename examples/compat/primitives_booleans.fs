FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");

// Primitive features and boolean intersection; query helpers qLargest / qFarthestAlong.
annotation { "Feature Type Name" : "Primitives" }
export const primitives = defineFeature(function(context is Context, id is Id, definition is map)
    precondition {}
    {
        // sphere = half-disc revolved around Z (fSphere needs a vertex query in Onshape)
        var hd = newSketchOnPlane(context, id + "halfDisc", { "sketchPlane" : XZ_PLANE });
        skArc(hd, "arc", { "start" : vector(-15, 0) * millimeter, "mid" : vector(0, 15) * millimeter, "end" : vector(15, 0) * millimeter });
        skLineSegment(hd, "axis", { "start" : vector(15, 0) * millimeter, "end" : vector(-15, 0) * millimeter });
        skSolve(hd);
        opRevolve(context, id + "ball", {
            "entities" : qSketchRegion(id + "halfDisc"),
            "axis" : line(vector(0, 0, 0) * millimeter, vector(0, 0, 1)),
            "angleForward" : 360 * degree });
        opDeleteBodies(context, id + "dropSketch", { "entities" : qCreatedBy(id + "halfDisc", EntityType.BODY) });
        fCuboid(context, id + "cube", { "corner1" : vector(-11, -11, -11) * millimeter, "corner2" : vector(11, 11, 11) * millimeter });
        opBoolean(context, id + "rounded", {
            "tools" : qUnion([qCreatedBy(id + "ball", EntityType.BODY), qCreatedBy(id + "cube", EntityType.BODY)]),
            "operationType" : BooleanOperationType.INTERSECTION });

        fCone(context, id + "cone", { "bottomCenter" : vector(40, 0, 0) * millimeter, "topCenter" : vector(40, 0, 30) * millimeter,
                "bottomRadius" : 12 * millimeter, "topRadius" : 4 * millimeter });
        // fillet the largest (bottom) circular edge of the cone
        opFillet(context, id + "coneFillet", {
            "entities" : qLargest(qGeometry(qCreatedBy(id + "cone", EntityType.EDGE), GeometryType.CIRCLE)),
            "radius" : 2 * millimeter });
        // chamfer the top edge, found as the edge farthest along +Z
        opChamfer(context, id + "coneTop", {
            "entities" : qFarthestAlong(qCreatedBy(id + "cone", EntityType.EDGE), vector(0, 0, 1)),
            "chamferType" : ChamferType.EQUAL_OFFSETS, "width" : 1 * millimeter });
    });
