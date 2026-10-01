FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");

// Flange with hub: revolved half-profile (Front plane, around the world Z axis), one bolt hole
// patterned with circularPattern, chamfered rim.
export function build(context is Context, id is Id)
{
    variable(context, "flangeDiameter", 60 * millimeter);
    variable(context, "flangeThickness", 8 * millimeter);
    variable(context, "hubDiameter", 30 * millimeter);
    variable(context, "hubHeight", 20 * millimeter);
    variable(context, "boreDiameter", 12 * millimeter);
    variable(context, "boltCircle", 45 * millimeter);
    variable(context, "boltDiameter", 5.5 * millimeter);
    variable(context, "boltCount", 6);

    // half profile in the XZ half-plane x >= bore radius; construction line on the Z axis is the revolve axis
    var profile = newSketch(context, id + "Profile sketch", { "sketchPlane" : qCreatedBy(makeId("Front"), EntityType.FACE) });
    skPolyline(profile, "outline", { "points" : [
            vector(#boreDiameter / 2, 0 * millimeter), vector(#flangeDiameter / 2, 0 * millimeter),
            vector(#flangeDiameter / 2, #flangeThickness), vector(#hubDiameter / 2, #flangeThickness),
            vector(#hubDiameter / 2, #flangeThickness + #hubHeight), vector(#boreDiameter / 2, #flangeThickness + #hubHeight),
            vector(#boreDiameter / 2, 0 * millimeter)] });
    skLineSegment(profile, "axis", { "start" : vector(0, 0) * millimeter, "end" : vector(0, 10) * millimeter, "construction" : true });
    skSolve(profile);
    revolve(context, id + "Body", {
        "entities" : qSketchRegion(id + "Profile sketch"),
        "axis" : sketchEntityQuery(id + "Profile sketch", EntityType.EDGE, "axis"),
        "fullRevolve" : true });

    var bolt = newSketch(context, id + "Bolt hole sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
    skCircle(bolt, "hole", { "center" : vector(#boltCircle / 2, 0 * millimeter), "radius" : #boltDiameter / 2 });
    skSolve(bolt);
    extrude(context, id + "Bolt hole", {
        "entities" : qSketchRegion(id + "Bolt hole sketch"),
        "operationType" : NewBodyOperationType.REMOVE,
        "endBound" : BoundingType.THROUGH_ALL,
        "defaultScope" : false,
        "booleanScope" : qCreatedBy(id + "Body", EntityType.BODY) });
    circularPattern(context, id + "Bolt pattern", {
        "patternType" : PatternType.FEATURE,
        "instanceFunction" : [id + "Bolt hole"],
        "axis" : sketchEntityQuery(id + "Profile sketch", EntityType.EDGE, "axis"),
        "angle" : 360 * degree,
        "instanceCount" : #boltCount,
        "equalSpace" : true });

    // 1 mm chamfer on the top outer rim of the flange: circular edge through (flangeDiameter/2, 0, flangeThickness)
    chamfer(context, id + "Rim chamfer", {
        "entities" : qContainsPoint(qCreatedBy(id + "Body", EntityType.EDGE), vector(30, 0, 8) * millimeter),
        "chamferType" : ChamferType.EQUAL_OFFSETS,
        "width" : 1 * millimeter });
}
