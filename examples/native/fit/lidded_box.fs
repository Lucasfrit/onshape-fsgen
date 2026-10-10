FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");

// Box with a lid, laid out for printing: the box upright, the lid beside it upside down (plate on the bed,
// the locating lip pointing up). lidded_box.fit.toml puts the lid on the box for `fsgen studio fit` / `view`.
export function build(context is Context, id is Id)
{
    variable(context, "width", 30 * millimeter);
    variable(context, "depth", 20 * millimeter);
    variable(context, "height", 15 * millimeter);
    variable(context, "wall", 2 * millimeter);
    variable(context, "clearance", 0.2 * millimeter);

    const mm = millimeter;
    const topPlane = qCreatedBy(makeId("Top"), EntityType.FACE);
    const lidX = #width + 10 * mm;   // lid centre in the print layout

    var boxSk = newSketch(context, id + "Box sketch", { "sketchPlane" : topPlane });
    skRectangle(boxSk, "outline", { "firstCorner" : vector(-#width / 2, -#depth / 2), "secondCorner" : vector(#width / 2, #depth / 2) });
    skSolve(boxSk);
    extrude(context, id + "Box", { "entities" : qSketchRegion(id + "Box sketch"), "endBound" : BoundingType.BLIND, "depth" : #height });

    cPlane(context, id + "Floor plane", { "entities" : topPlane, "offset" : #wall });
    var pocketSk = newSketch(context, id + "Pocket sketch", { "sketchPlane" : qCreatedBy(id + "Floor plane", EntityType.FACE) });
    skRectangle(pocketSk, "pocket", { "firstCorner" : vector(-#width / 2 + #wall, -#depth / 2 + #wall),
            "secondCorner" : vector(#width / 2 - #wall, #depth / 2 - #wall) });
    skSolve(pocketSk);
    extrude(context, id + "Pocket", {
        "entities" : qSketchRegion(id + "Pocket sketch"),
        "operationType" : NewBodyOperationType.REMOVE, "endBound" : BoundingType.THROUGH_ALL,
        "defaultScope" : false, "booleanScope" : qCreatedBy(id + "Box", EntityType.BODY) });

    var lidSk = newSketch(context, id + "Lid sketch", { "sketchPlane" : topPlane });
    skRectangle(lidSk, "plate", { "firstCorner" : vector(lidX - #width / 2, -#depth / 2), "secondCorner" : vector(lidX + #width / 2, #depth / 2) });
    skSolve(lidSk);
    extrude(context, id + "Lid", {
        "entities" : qSketchRegion(id + "Lid sketch"),
        "operationType" : NewBodyOperationType.NEW, "endBound" : BoundingType.BLIND, "depth" : #wall });

    // lip: a ring that fits inside the box opening with #clearance per side
    var lipSk = newSketch(context, id + "Lip sketch", { "sketchPlane" : topPlane });
    skRectangle(lipSk, "outer", { "firstCorner" : vector(lidX - #width / 2 + #wall + #clearance, -#depth / 2 + #wall + #clearance),
            "secondCorner" : vector(lidX + #width / 2 - #wall - #clearance, #depth / 2 - #wall - #clearance) });
    skRectangle(lipSk, "inner", { "firstCorner" : vector(lidX - #width / 2 + 2 * #wall, -#depth / 2 + 2 * #wall),
            "secondCorner" : vector(lidX + #width / 2 - 2 * #wall, #depth / 2 - 2 * #wall) });
    skSolve(lipSk);
    extrude(context, id + "Lip", {
        "entities" : qSketchRegion(id + "Lip sketch", true),
        "operationType" : NewBodyOperationType.ADD, "endBound" : BoundingType.BLIND, "depth" : #wall + 3 * mm,
        "defaultScope" : false, "booleanScope" : qCreatedBy(id + "Lid", EntityType.BODY) });
}
