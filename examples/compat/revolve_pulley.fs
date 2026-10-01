FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");

// Revolve a closed profile drawn on the XZ plane around the world Z axis.
// XZ_PLANE sketch coordinates: u -> world Z, v -> world X (Onshape convention).
annotation { "Feature Type Name" : "Pulley" }
export const pulley = defineFeature(function(context is Context, id is Id, definition is map)
    precondition {}
    {
        const sketchPlane = plane(vector(0, 0, 0) * millimeter, vector(0, -1, 0), vector(1, 0, 0));
        var sk = newSketchOnPlane(context, id + "profile", { "sketchPlane" : sketchPlane });
        // profile in (x = radius, y = height): hub, groove, flange
        skPolyline(sk, "outline", { "points" : [
                vector(4, 0) * millimeter, vector(20, 0) * millimeter, vector(20, 3) * millimeter,
                vector(15, 6) * millimeter, vector(20, 9) * millimeter, vector(20, 12) * millimeter,
                vector(4, 12) * millimeter, vector(4, 0) * millimeter] });
        skSolve(sk);
        opRevolve(context, id + "rev", {
            "entities" : qSketchRegion(id + "profile"),
            "axis" : line(vector(0, 0, 0) * millimeter, vector(0, 0, 1)),
            "angleForward" : 360 * degree });
        opDeleteBodies(context, id + "cleanup", { "entities" : qCreatedBy(id + "profile", EntityType.BODY) });

        // partial revolve with angleBack, as a separate body
        var sk2 = newSketchOnPlane(context, id + "wedgeProfile", { "sketchPlane" : XZ_PLANE });
        skRectangle(sk2, "r", { "firstCorner" : vector(30, 30) * millimeter, "secondCorner" : vector(40, 36) * millimeter });
        skSolve(sk2);
        opRevolve(context, id + "wedge", {
            "entities" : qSketchRegion(id + "wedgeProfile"),
            "axis" : line(vector(0, 0, 0) * millimeter, vector(0, 0, 1)),
            "angleForward" : 90 * degree,
            "angleBack" : 30 * degree });
    });
