FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");

// Small enclosure: box, shell from the top, rounded vertical edges, four screw bosses (one sketch,
// extruded up from the floor), a lid-height reference plane and a cable hole in the front wall.
export function build(context is Context, id is Id)
{
    variable(context, "length", 90 * millimeter);
    variable(context, "width", 60 * millimeter);
    variable(context, "height", 30 * millimeter);
    variable(context, "wall", 2 * millimeter);
    variable(context, "cornerRadius", 6 * millimeter);
    variable(context, "bossDiameter", 7 * millimeter);
    variable(context, "bossHole", 2.5 * millimeter);
    variable(context, "bossInset", 8 * millimeter);

    var base = newSketch(context, id + "Box sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
    skRectangle(base, "outline", { "firstCorner" : vector(-#length / 2, -#width / 2), "secondCorner" : vector(#length / 2, #width / 2) });
    skSolve(base);
    extrude(context, id + "Box", { "entities" : qSketchRegion(id + "Box sketch"), "endBound" : BoundingType.BLIND, "depth" : #height });
    fillet(context, id + "Corner rounds", {
        "entities" : qParallelEdges(qCreatedBy(id + "Box", EntityType.EDGE), vector(0, 0, 1)),
        "radius" : #cornerRadius });
    // remove the top face (the face lying in z = 30 mm plane)
    shell(context, id + "Shell", {
        "entities" : qCoincidesWithPlane(qCreatedBy(id + "Box", EntityType.FACE), plane(vector(0, 0, 30) * millimeter, vector(0, 0, 1))),
        "thickness" : #wall });

    // bosses: four circles on a plane at floor height, extruded up to just below the rim
    cPlane(context, id + "Floor plane", { "entities" : qCreatedBy(makeId("Top"), EntityType.FACE), "offset" : #wall });
    var bosses = newSketch(context, id + "Boss sketch", { "sketchPlane" : qCreatedBy(id + "Floor plane", EntityType.FACE) });
    for (var sx in [-1, 1])
    {
        for (var sy in [-1, 1])
        {
            const c = vector(sx * (#length / 2 - #bossInset), sy * (#width / 2 - #bossInset));
            skCircle(bosses, "boss" ~ (sx > 0 ? "R" : "L") ~ (sy > 0 ? "B" : "F"), { "center" : c, "radius" : #bossDiameter / 2 });
            skCircle(bosses, "hole" ~ (sx > 0 ? "R" : "L") ~ (sy > 0 ? "B" : "F"), { "center" : c, "radius" : #bossHole / 2 });
        }
    }
    skSolve(bosses);
    extrude(context, id + "Bosses", {
        "entities" : qSketchRegion(id + "Boss sketch", true),
        "operationType" : NewBodyOperationType.ADD,
        "endBound" : BoundingType.BLIND,
        "depth" : #height - #wall - 2 * millimeter,
        "defaultScope" : false,
        "booleanScope" : qCreatedBy(id + "Box", EntityType.BODY) });

    // cable hole through the front wall (Front plane: x -> X, y -> Z; normal -Y points out of the front)
    var cable = newSketch(context, id + "Cable sketch", { "sketchPlane" : qCreatedBy(makeId("Front"), EntityType.FACE) });
    skCircle(cable, "cable", { "center" : vector(0 * millimeter, #height / 2), "radius" : 4 * millimeter });
    skSolve(cable);
    extrude(context, id + "Cable hole", {
        "entities" : qSketchRegion(id + "Cable sketch"),
        "operationType" : NewBodyOperationType.REMOVE,
        "endBound" : BoundingType.BLIND,
        "depth" : #width / 2 + 1 * millimeter,
        "defaultScope" : false,
        "booleanScope" : qCreatedBy(id + "Box", EntityType.BODY) });
}
