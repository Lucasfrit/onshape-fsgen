FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");

// Circular pattern with rotationAround, mirror with mirrorAcross, translation with opTransform,
// all combined into one part. Uses a helper function and arrays.
function boltPatternTransforms(count is number, axis is Line) returns array
{
    var transforms = [];
    for (var i = 1; i < count; i += 1)
    {
        transforms = append(transforms, rotationAround(axis, i * 360 * degree / count));
    }
    return transforms;
}

annotation { "Feature Type Name" : "Patterns" }
export const patterns = defineFeature(function(context is Context, id is Id, definition is map)
    precondition {}
    {
        const count = 6;
        const zAxis = line(vector(0, 0, 0) * millimeter, vector(0, 0, 1));
        fCylinder(context, id + "disc", { "bottomCenter" : vector(0, 0, 0) * millimeter, "topCenter" : vector(0, 0, 6) * millimeter, "radius" : 40 * millimeter });

        fCylinder(context, id + "hole", { "bottomCenter" : vector(30, 0, -1) * millimeter, "topCenter" : vector(30, 0, 7) * millimeter, "radius" : 3 * millimeter });
        var names = [];
        for (var i = 1; i < count; i += 1)
        {
            names = append(names, "h" ~ i);
        }
        opPattern(context, id + "holePattern", {
            "entities" : qCreatedBy(id + "hole", EntityType.BODY),
            "transforms" : boltPatternTransforms(count, zAxis),
            "instanceNames" : names });
        opBoolean(context, id + "cutHoles", {
            "tools" : qUnion([qCreatedBy(id + "hole", EntityType.BODY), qCreatedBy(id + "holePattern", EntityType.BODY)]),
            "targets" : qCreatedBy(id + "disc", EntityType.BODY),
            "operationType" : BooleanOperationType.SUBTRACTION });

        // tab, mirrored across the YZ plane, then all moved up
        fCuboid(context, id + "tab", { "corner1" : vector(38, -5, 0) * millimeter, "corner2" : vector(55, 5, 6) * millimeter });
        opPattern(context, id + "mirror", {
            "entities" : qCreatedBy(id + "tab", EntityType.BODY),
            "transforms" : [mirrorAcross(YZ_PLANE)],
            "instanceNames" : ["m"] });
        opBoolean(context, id + "joinTabs", {
            "tools" : qUnion([qCreatedBy(id + "disc", EntityType.BODY), qCreatedBy(id + "tab", EntityType.BODY), qCreatedBy(id + "mirror", EntityType.BODY)]),
            "operationType" : BooleanOperationType.UNION });
        opTransform(context, id + "lift", {
            "bodies" : qCreatedBy(id + "disc", EntityType.BODY),
            "transform" : transform(vector(0, 0, 5) * millimeter) * rotationAround(zAxis, 15 * degree) });
    });
