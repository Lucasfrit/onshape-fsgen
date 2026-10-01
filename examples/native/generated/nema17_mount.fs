FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");

// NEMA 17 stepper motor mount: L-bracket with a motor face (22.5 mm boss bore,
// four M3 clearance holes on a 31 mm square) and two front-to-back M5 slots in the base.
export function build(context is Context, id is Id)
{
    variable(context, "thickness", 5 * millimeter);      // plate thickness
    variable(context, "width", 50 * millimeter);         // along Y
    variable(context, "baseDepth", 45 * millimeter);     // base, along X
    variable(context, "plateHeight", 50 * millimeter);   // upright, along Z

    variable(context, "motorHeight", 28 * millimeter);   // bore axis above the ground
    variable(context, "boreDiameter", 22.5 * millimeter);
    variable(context, "screwSpacing", 31 * millimeter);  // NEMA 17 square pattern
    variable(context, "screwDiameter", 3.4 * millimeter);// M3 clearance

    variable(context, "slotWidth", 5.5 * millimeter);    // M5 clearance
    variable(context, "slotLength", 12 * millimeter);    // travel, along X
    variable(context, "slotSpacing", 34 * millimeter);   // between slot centers, along Y
    variable(context, "slotOffset", 28 * millimeter);    // slot center from the upright

    // ---- L profile on the Front plane (sketch x -> world X, sketch y -> world Z)
    var profile = newSketch(context, id + "Profile sketch", { "sketchPlane" : qCreatedBy(makeId("Front"), EntityType.FACE) });
    skPolyline(profile, "L", { "points" : [
                vector(0 * millimeter, 0 * millimeter),
                vector(#baseDepth, 0 * millimeter),
                vector(#baseDepth, #thickness),
                vector(#thickness, #thickness),
                vector(#thickness, #plateHeight),
                vector(0 * millimeter, #plateHeight),
                vector(0 * millimeter, 0 * millimeter)] });
    skSolve(profile);
    extrude(context, id + "Bracket", {
            "entities" : qSketchRegion(id + "Profile sketch"),
            "endBound" : BoundingType.BLIND,
            "depth" : #width / 2,
            "hasSecondDirection" : true,
            "secondDirectionBound" : BoundingType.BLIND,
            "secondDirectionDepth" : #width / 2 });

    // ---- motor boss bore: Right plane (sketch x -> world Y, sketch y -> world Z), cut along +X
    var bore = newSketch(context, id + "Bore sketch", { "sketchPlane" : qCreatedBy(makeId("Right"), EntityType.FACE) });
    skCircle(bore, "bore", { "center" : vector(0 * millimeter, #motorHeight), "radius" : #boreDiameter / 2 });
    skSolve(bore);
    extrude(context, id + "Motor bore", {
            "entities" : qSketchRegion(id + "Bore sketch"),
            "operationType" : NewBodyOperationType.REMOVE,
            "endBound" : BoundingType.THROUGH_ALL,
            "defaultScope" : false,
            "booleanScope" : qCreatedBy(id + "Bracket", EntityType.BODY) });

    // ---- M3 clearance holes: upper and lower hole on one side of the axis,
    //      then mirrored across the Front plane to complete the 31 mm square
    var screw = newSketch(context, id + "Screw hole sketch", { "sketchPlane" : qCreatedBy(makeId("Right"), EntityType.FACE) });
    skCircle(screw, "upperHole", {
            "center" : vector(#screwSpacing / 2, #motorHeight + #screwSpacing / 2),
            "radius" : #screwDiameter / 2 });
    skCircle(screw, "lowerHole", {
            "center" : vector(#screwSpacing / 2, #motorHeight - #screwSpacing / 2),
            "radius" : #screwDiameter / 2 });
    skSolve(screw);
    extrude(context, id + "Screw hole", {
            "entities" : qSketchRegion(id + "Screw hole sketch"),
            "operationType" : NewBodyOperationType.REMOVE,
            "endBound" : BoundingType.THROUGH_ALL,
            "defaultScope" : false,
            "booleanScope" : qCreatedBy(id + "Bracket", EntityType.BODY) });

    mirror(context, id + "Mirror screws Y", {
            "patternType" : PatternType.FEATURE,
            "instanceFunction" : [id + "Screw hole"],
            "mirrorPlane" : qCreatedBy(makeId("Front"), EntityType.FACE) });

    // ---- M5 slot in the base: Top plane (sketch x -> world X, sketch y -> world Y), long axis along X
    var slot = newSketch(context, id + "Slot sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
    skLineSegment(slot, "far", {
            "start" : vector(#slotOffset - (#slotLength - #slotWidth) / 2, #slotSpacing / 2 + #slotWidth / 2),
            "end" : vector(#slotOffset + (#slotLength - #slotWidth) / 2, #slotSpacing / 2 + #slotWidth / 2) });
    skArc(slot, "frontEnd", {
            "start" : vector(#slotOffset + (#slotLength - #slotWidth) / 2, #slotSpacing / 2 + #slotWidth / 2),
            "mid" : vector(#slotOffset + #slotLength / 2, #slotSpacing / 2),
            "end" : vector(#slotOffset + (#slotLength - #slotWidth) / 2, #slotSpacing / 2 - #slotWidth / 2) });
    skLineSegment(slot, "near", {
            "start" : vector(#slotOffset + (#slotLength - #slotWidth) / 2, #slotSpacing / 2 - #slotWidth / 2),
            "end" : vector(#slotOffset - (#slotLength - #slotWidth) / 2, #slotSpacing / 2 - #slotWidth / 2) });
    skArc(slot, "backEnd", {
            "start" : vector(#slotOffset - (#slotLength - #slotWidth) / 2, #slotSpacing / 2 - #slotWidth / 2),
            "mid" : vector(#slotOffset - #slotLength / 2, #slotSpacing / 2),
            "end" : vector(#slotOffset - (#slotLength - #slotWidth) / 2, #slotSpacing / 2 + #slotWidth / 2) });
    skSolve(slot);
    extrude(context, id + "Mount slot", {
            "entities" : qSketchRegion(id + "Slot sketch"),
            "operationType" : NewBodyOperationType.REMOVE,
            "endBound" : BoundingType.THROUGH_ALL,
            "defaultScope" : false,
            "booleanScope" : qCreatedBy(id + "Bracket", EntityType.BODY) });
    mirror(context, id + "Mirror slots", {
            "patternType" : PatternType.FEATURE,
            "instanceFunction" : [id + "Mount slot"],
            "mirrorPlane" : qCreatedBy(makeId("Front"), EntityType.FACE) });

    // ---- inside corner: the Y-parallel edge through (thickness, 0, thickness)
    fillet(context, id + "Inside fillet", {
            "entities" : qContainsPoint(qParallelEdges(qCreatedBy(id + "Bracket", EntityType.EDGE), vector(0, 1, 0)),
                    vector(5, 0, 5) * millimeter),
            "radius" : 3 * millimeter });
}
