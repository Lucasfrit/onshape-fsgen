FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");

annotation { "Feature Type Name" : "Gen Part" }
export const genPart = defineFeature(function(context is Context, id is Id, definition is map)
    precondition {}
    {
        var sk = newSketchOnPlane(context, id + "sk", { "sketchPlane" : plane(vector(0, 0, 0) * meter, vector(0, 0, 1)) });
        skRectangle(sk, "r", { "firstCorner" : vector(0, 0) * millimeter, "secondCorner" : vector(40, 20) * millimeter });
        skCircle(sk, "c", { "center" : vector(20, 10) * millimeter, "radius" : 4 * millimeter });
        skSolve(sk);
        opExtrude(context, id + "ext", {
            "entities" : qSketchRegion(id + "sk", true),
            "direction" : vector(0, 0, 1),
            "endBound" : BoundingType.BLIND, "endDepth" : 5 * millimeter });
    });
