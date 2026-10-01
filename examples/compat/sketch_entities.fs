FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");

// Sketch entity coverage: arcs, polygons, ellipses, nested loops (filterInnerLoops), planes with x direction.
annotation { "Feature Type Name" : "Sketch entities" }
export const sketchEntities = defineFeature(function(context is Context, id is Id, definition is map)
    precondition {}
    {
        // washer: outer circle + inner circle, keep only the ring
        var w = newSketchOnPlane(context, id + "washer", { "sketchPlane" : XY_PLANE });
        skCircle(w, "outer", { "center" : vector(0, 0) * millimeter, "radius" : 12 * millimeter });
        skCircle(w, "inner", { "center" : vector(0, 0) * millimeter, "radius" : 6.5 * millimeter });
        skSolve(w);
        opExtrude(context, id + "washerBody", { "entities" : qSketchRegion(id + "washer", true), "direction" : vector(0, 0, 1),
                "endBound" : BoundingType.BLIND, "endDepth" : 2 * millimeter });

        // slot (two arcs + two lines) on a tilted plane with explicit x direction
        const slotPlane = plane(vector(40, 0, 0) * millimeter, normalize(vector(0, -1, 1)), vector(1, 0, 0));
        var s = newSketchOnPlane(context, id + "slot", { "sketchPlane" : slotPlane });
        const L = 10 * millimeter;
        const R = 4 * millimeter;
        skLineSegment(s, "l1", { "start" : vector(-L, -R), "end" : vector(L, -R) });
        skArc(s, "a1", { "start" : vector(L, -R), "mid" : vector(L + R, 0 * millimeter), "end" : vector(L, R) });
        skLineSegment(s, "l2", { "start" : vector(L, R), "end" : vector(-L, R) });
        skArc(s, "a2", { "start" : vector(-L, R), "mid" : vector(-L - R, 0 * millimeter), "end" : vector(-L, -R) });
        skSolve(s);
        opExtrude(context, id + "slotBody", { "entities" : qSketchRegion(id + "slot"), "direction" : slotPlane.normal,
                "endBound" : BoundingType.BLIND, "endDepth" : 3 * millimeter });

        // hexagon prism and an ellipse prism
        var h = newSketchOnPlane(context, id + "hex", { "sketchPlane" : plane(vector(0, 40, 0) * millimeter, vector(0, 0, 1)) });
        skRegularPolygon(h, "p", { "center" : vector(0, 0) * millimeter, "firstVertex" : vector(8, 0) * millimeter, "sides" : 6 });
        skEllipse(h, "e", { "center" : vector(25, 0) * millimeter, "majorRadius" : 9 * millimeter, "minorRadius" : 4 * millimeter });
        skSolve(h);
        opExtrude(context, id + "hexBody", { "entities" : qSketchRegion(id + "hex"), "direction" : vector(0, 0, 1),
                "endBound" : BoundingType.BLIND, "endDepth" : 5 * millimeter });
        opDeleteBodies(context, id + "cleanup", { "entities" : qUnion([qCreatedBy(id + "washer", EntityType.BODY),
                qCreatedBy(id + "slot", EntityType.BODY), qCreatedBy(id + "hex", EntityType.BODY)]) });
    });
