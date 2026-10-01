FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");

// Parametric L-bracket: base plate + upright, two mounting holes in each leg,
// fillet on the inside corner. Reference example for FS-Lite style.
const LENGTH_SPEC = { (millimeter) : [10, 60, 500] } as LengthBoundSpec;
const THICKNESS_SPEC = { (millimeter) : [1, 5, 50] } as LengthBoundSpec;
const HOLE_SPEC = { (millimeter) : [1, 5.5, 50] } as LengthBoundSpec;

// Extrude a rectangle drawn on `pl` along its normal. All operations are children of `prismId`
// (Onshape requires the sub-ids of one parent to be contiguous), so qCreatedBy(prismId, EntityType.BODY)
// finds the resulting solid.
function rectPrism(context is Context, prismId is Id, pl is Plane, c0 is Vector, c1 is Vector, depth is ValueWithUnits)
{
    var sk = newSketchOnPlane(context, prismId + "sketch", { "sketchPlane" : pl });
    skRectangle(sk, "r", { "firstCorner" : c0, "secondCorner" : c1 });
    skSolve(sk);
    opExtrude(context, prismId + "extrude", {
        "entities" : qSketchRegion(prismId + "sketch"),
        "direction" : pl.normal,
        "endBound" : BoundingType.BLIND,
        "endDepth" : depth });
    opDeleteBodies(context, prismId + "deleteSketch", { "entities" : qCreatedBy(prismId + "sketch", EntityType.BODY) });
}

annotation { "Feature Type Name" : "L bracket" }
export const lBracket = defineFeature(function(context is Context, id is Id, definition is map)
    precondition
    {
        annotation { "Name" : "Width" }
        isLength(definition.width, LENGTH_SPEC);
        annotation { "Name" : "Base length" }
        isLength(definition.baseLength, LENGTH_SPEC);
        annotation { "Name" : "Height" }
        isLength(definition.height, { (millimeter) : [10, 40, 500] } as LengthBoundSpec);
        annotation { "Name" : "Thickness" }
        isLength(definition.thickness, THICKNESS_SPEC);
        annotation { "Name" : "Hole diameter" }
        isLength(definition.holeDiameter, HOLE_SPEC);
        annotation { "Name" : "Inside fillet" }
        isLength(definition.filletRadius, { (millimeter) : [0.5, 4, 50] } as LengthBoundSpec);
    }
    {
        const W = definition.width;
        const L = definition.baseLength;
        const H = definition.height;
        const T = definition.thickness;

        // base plate: x in [0, L], y in [0, W], z in [0, T]
        rectPrism(context, id + "base", XY_PLANE, vector(0, 0) * millimeter, vector(L, W), T);
        // upright: x in [0, T], y in [0, W], z in [0, H]  (sketch on YZ plane: u -> Y, v -> Z, normal +X)
        rectPrism(context, id + "upright", YZ_PLANE, vector(0, 0) * millimeter, vector(W, H), T);

        opBoolean(context, id + "join", {
            "tools" : qUnion([qCreatedBy(id + "base", EntityType.BODY), qCreatedBy(id + "upright", EntityType.BODY)]),
            "operationType" : BooleanOperationType.UNION });

        // holes: two through the base (along Z), two through the upright (along X)
        const r = definition.holeDiameter / 2;
        var tools = [];
        for (var i = 0; i < 2; i += 1)
        {
            const y = W * (i + 1) / 3;
            const baseHole = id + ("baseHole" ~ i);
            fCylinder(context, baseHole, {
                "bottomCenter" : vector((L + T) / 2, y, -1 * millimeter),
                "topCenter" : vector((L + T) / 2, y, T + 1 * millimeter),
                "radius" : r });
            const upHole = id + ("upHole" ~ i);
            fCylinder(context, upHole, {
                "bottomCenter" : vector(-1 * millimeter, y, (H + T) / 2),
                "topCenter" : vector(T + 1 * millimeter, y, (H + T) / 2),
                "radius" : r });
            tools = concatenateArrays([tools, [qCreatedBy(baseHole, EntityType.BODY), qCreatedBy(upHole, EntityType.BODY)]]);
        }
        opBoolean(context, id + "holes", {
            "tools" : qUnion(tools),
            "targets" : qCreatedBy(id + "base", EntityType.BODY),
            "operationType" : BooleanOperationType.SUBTRACTION });

        // inside corner edge: the Y-parallel edge passing through (T, *, T)
        opFillet(context, id + "insideFillet", {
            "entities" : qContainsPoint(qParallelEdges(qOwnedByBody(qCreatedBy(id + "base", EntityType.BODY), EntityType.EDGE), vector(0, 1, 0)),
                    vector(T, W / 2, T)),
            "radius" : definition.filletRadius });
    });
