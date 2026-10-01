FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");

// Fan adapter: vented base plate with a square-to-round loft transition.
export function build(context is Context, id is Id)
{
    variable(context, "plateLength", 100 * millimeter);      // X
    variable(context, "plateWidth", 60 * millimeter);        // Y
    variable(context, "plateThickness", 3 * millimeter);     // Z

    variable(context, "slotWidth", 4 * millimeter);          // across the slot (X)
    variable(context, "slotLength", 40 * millimeter);        // along the slot (Y)
    variable(context, "slotPitch", 8 * millimeter);          // 12 mm x 6 would overrun the left half
    variable(context, "slotCount", 6);

    variable(context, "loftSquare", 40 * millimeter);
    variable(context, "loftCircleDiameter", 30 * millimeter);
    variable(context, "loftHeight", 25 * millimeter);

    variable(context, "holeDiameter", 3.4 * millimeter);
    variable(context, "holeInset", 5 * millimeter);

    // ---------- base plate ----------
    var plate = newSketch(context, id + "Base plate sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
    skRectangle(plate, "outline", {
            "firstCorner" : vector(-#plateLength / 2, -#plateWidth / 2),
            "secondCorner" : vector(#plateLength / 2, #plateWidth / 2) });
    skSolve(plate);
    extrude(context, id + "Base plate", {
            "entities" : qSketchRegion(id + "Base plate sketch"),
            "operationType" : NewBodyOperationType.NEW,
            "endBound" : BoundingType.BLIND,
            "depth" : #plateThickness });

    // ---------- square-to-round loft on the right half ----------
    cPlane(context, id + "Plate top plane", {
            "entities" : qCreatedBy(makeId("Top"), EntityType.FACE),
            "offset" : #plateThickness });
    cPlane(context, id + "Loft top plane", {
            "entities" : qCreatedBy(id + "Plate top plane", EntityType.FACE),
            "offset" : #loftHeight });

    var loftBase = newSketch(context, id + "Loft base sketch", { "sketchPlane" : qCreatedBy(id + "Plate top plane", EntityType.FACE) });
    skRectangle(loftBase, "square", {
            "firstCorner" : vector(#plateLength / 4 - #loftSquare / 2, -#loftSquare / 2),
            "secondCorner" : vector(#plateLength / 4 + #loftSquare / 2, #loftSquare / 2) });
    skSolve(loftBase);

    var loftTop = newSketch(context, id + "Loft top sketch", { "sketchPlane" : qCreatedBy(id + "Loft top plane", EntityType.FACE) });
    skCircle(loftTop, "circle", {
            "center" : vector(#plateLength / 4, 0 * millimeter),
            "radius" : #loftCircleDiameter / 2 });
    skSolve(loftTop);

    loft(context, id + "Transition loft", {
            "operationType" : NewBodyOperationType.ADD,
            "sheetProfilesArray" : [
                { "sheetProfileEntities" : qSketchRegion(id + "Loft base sketch") },
                { "sheetProfileEntities" : qSketchRegion(id + "Loft top sketch") }
            ],
            "defaultScope" : false,
            "booleanScope" : qCreatedBy(id + "Base plate", EntityType.BODY) });

    // ---------- vent slots on the left half ----------
    // one obround: straight sides of (slotLength - slotWidth), semicircular ends of slotWidth/2
    const slotX = -#plateLength / 4 - #slotPitch * (#slotCount - 1) / 2;   // centre of the first slot
    const slotStraight = #slotLength / 2 - #slotWidth / 2;

    var slot = newSketch(context, id + "Vent slot sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
    skLineSegment(slot, "left", {
            "start" : vector(slotX - #slotWidth / 2, slotStraight),
            "end" : vector(slotX - #slotWidth / 2, -slotStraight) });
    skArc(slot, "bottomEnd", {
            "start" : vector(slotX - #slotWidth / 2, -slotStraight),
            "mid" : vector(slotX, -#slotLength / 2),
            "end" : vector(slotX + #slotWidth / 2, -slotStraight) });
    skLineSegment(slot, "right", {
            "start" : vector(slotX + #slotWidth / 2, -slotStraight),
            "end" : vector(slotX + #slotWidth / 2, slotStraight) });
    skArc(slot, "topEnd", {
            "start" : vector(slotX + #slotWidth / 2, slotStraight),
            "mid" : vector(slotX, #slotLength / 2),
            "end" : vector(slotX - #slotWidth / 2, slotStraight) });
    skSolve(slot);

    extrude(context, id + "Vent slot", {
            "entities" : qSketchRegion(id + "Vent slot sketch"),
            "operationType" : NewBodyOperationType.REMOVE,
            "endBound" : BoundingType.THROUGH_ALL,
            "defaultScope" : false,
            "booleanScope" : qCreatedBy(id + "Base plate", EntityType.BODY) });

    linearPattern(context, id + "Vent slot pattern", {
            "patternType" : PatternType.FEATURE,
            "instanceFunction" : [id + "Vent slot"],
            "directionOne" : qCreatedBy(makeId("Right"), EntityType.FACE),   // +X
            "distance" : #slotPitch,
            "instanceCount" : #slotCount });

    // ---------- mounting holes ----------
    var holes = newSketch(context, id + "Mounting hole sketch", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
    skCircle(holes, "hole", {
            "center" : vector(-#plateLength / 2 + #holeInset, -#plateWidth / 2 + #holeInset),
            "radius" : #holeDiameter / 2 });
    skSolve(holes);
    extrude(context, id + "Mounting hole", {
            "entities" : qSketchRegion(id + "Mounting hole sketch"),
            "operationType" : NewBodyOperationType.REMOVE,
            "endBound" : BoundingType.THROUGH_ALL,
            "defaultScope" : false,
            "booleanScope" : qCreatedBy(id + "Base plate", EntityType.BODY) });

    mirror(context, id + "Mirror holes X", {
            "patternType" : PatternType.FEATURE,
            "instanceFunction" : [id + "Mounting hole"],
            "mirrorPlane" : qCreatedBy(makeId("Right"), EntityType.FACE) });
    mirror(context, id + "Mirror holes Y", {
            "patternType" : PatternType.FEATURE,
            "instanceFunction" : [id + "Mounting hole", id + "Mirror holes X"],
            "mirrorPlane" : qCreatedBy(makeId("Front"), EntityType.FACE) });
}
