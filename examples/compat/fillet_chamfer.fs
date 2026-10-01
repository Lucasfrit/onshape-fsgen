FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");

// Edge selection by provenance and geometry, then fillets and chamfers.
annotation { "Feature Type Name" : "Fillet chamfer" }
export const filletChamfer = defineFeature(function(context is Context, id is Id, definition is map)
    precondition {}
    {
        fCuboid(context, id + "box", { "corner1" : vector(0, 0, 0) * millimeter, "corner2" : vector(50, 30, 20) * millimeter });
        // round the 4 vertical edges
        opFillet(context, id + "vertFillet", {
            "entities" : qParallelEdges(qCreatedBy(id + "box", EntityType.EDGE), vector(0, 0, 1)),
            "radius" : 5 * millimeter });
        // chamfer every edge lying in the top plane
        opChamfer(context, id + "topChamfer", {
            "entities" : qCoincidesWithPlane(qOwnedByBody(qCreatedBy(id + "box", EntityType.BODY), EntityType.EDGE),
                    plane(vector(0, 0, 20) * millimeter, vector(0, 0, 1))),
            "chamferType" : ChamferType.EQUAL_OFFSETS,
            "width" : 1.5 * millimeter });

        // second body: cylinder with a filleted top edge picked by a point on it
        fCylinder(context, id + "cyl", {
            "bottomCenter" : vector(80, 15, 0) * millimeter,
            "topCenter" : vector(80, 15, 25) * millimeter,
            "radius" : 10 * millimeter });
        opFillet(context, id + "cylFillet", {
            "entities" : qContainsPoint(qCreatedBy(id + "cyl", EntityType.EDGE), vector(90, 15, 25) * millimeter),
            "radius" : 3 * millimeter });
    });
