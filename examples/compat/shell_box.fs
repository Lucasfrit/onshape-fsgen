FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");

// Hollow a box by removing its top face (opShell).
annotation { "Feature Type Name" : "Shell box" }
export const shellBox = defineFeature(function(context is Context, id is Id, definition is map)
    precondition {}
    {
        fCuboid(context, id + "box", { "corner1" : vector(0, 0, 0) * millimeter, "corner2" : vector(60, 40, 25) * millimeter });
        opShell(context, id + "shell", {
            "entities" : qCoincidesWithPlane(qCreatedBy(id + "box", EntityType.FACE), plane(vector(0, 0, 25) * millimeter, vector(0, 0, 1))),
            "thickness" : 2 * millimeter });
    });
