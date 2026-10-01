FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");

// 3D-printable handle for the NEO Tools 04-227 precision set (7 double-ended 120 mm CrMo blades).
// Three parts, each standing in its print orientation (Z up, no supports):
//   handle   - stands on its flat back; deep bore stores the blade's unused end; M12x2 nose thread and a
//              3-slot collet with a 1:4 cone; snap socket for the cap in the back
//   end cap  - swivel cap, finger side down; split snap pin (two halves flex through the socket lip)
//   top      - collet nut, thread end down; internal thread + cone that squeezes the collet onto the blade
// Front-plane sketches: sketch x -> radius (+X), sketch y -> height (+Z).
export function build(context is Context, id is Id)
{
    variable(context, "handleDiameter", 18 * millimeter);
    variable(context, "gripLength", 70 * millimeter);
    variable(context, "bitDiameter", 4 * millimeter);      // blade shank; measure yours (not published)
    variable(context, "bitDepth", 65 * millimeter);        // blade length inside the handle (120 mm blade -> 55 mm out)
    variable(context, "threadDiameter", 12 * millimeter);
    variable(context, "threadPitch", 2 * millimeter);
    variable(context, "fitClearance", 0.25 * millimeter);  // radial print clearance (thread, nut, cap)
    variable(context, "fluteCount", 8);

    const mm = millimeter;
    const zero = 0 * millimeter;
    const topPlane = qCreatedBy(makeId("Top"), EntityType.FACE);
    const frontPlane = qCreatedBy(makeId("Front"), EntityType.FACE);

    // ---- derived layout (live expressions)
    const R = #handleDiameter / 2;
    const P = #threadPitch;
    const cl = #fitClearance;
    const rMaj = #threadDiameter / 2;
    const rMin = #threadDiameter / 2 - 0.5413 * #threadPitch;  // thread root = collet radius
    const bitR = #bitDiameter / 2 + 0.1 * mm;                  // bore: slip fit, the collet closes it
    const zS = #gripLength + 5 * mm;                           // thread start (after the taper)
    const zE = #gripLength + 15 * mm;                          // thread end (10 mm thread)
    const zK = #gripLength + 19 * mm;                          // collet cone start
    const zTip = #gripLength + 25 * mm;                        // nose tip (cone 1:4, 1.5 mm)
    // snap socket (handle back) / pin (cap): lip 4 mm long, head 2 mm, 0.25 mm snap per side
    const lipR = 2.5 * mm + #fitClearance;
    const headR = 2.75 * mm + #fitClearance;
    const chamberR = 2.75 * mm + 2 * #fitClearance;
    // nut: 8 mm thread (3 turns), touches the collet cone 3 mm before it bottoms out
    const uK = 11 * mm;                                        // nut cone point at radius rMin
    const rExit = #bitDiameter / 2 + 0.7 * mm;
    const nutLength = 11 * mm + (#threadDiameter / 2 - 0.5413 * #threadPitch - #bitDiameter / 2 - 0.7 * mm) * 4;
    const nutX = #handleDiameter + 6 * mm;
    const capX = -#handleDiameter - 6 * mm;

    // ---------- Handle body ----------
    var bodySk = newSketch(context, id + "Handle sketch", { "sketchPlane" : frontPlane });
    skPolyline(bodySk, "profile", { "points" : [
            vector(lipR + 0.5 * mm, zero), vector(R - 1 * mm, zero), vector(R, 1 * mm),   // back face, chamfer
            vector(R, #gripLength), vector(rMin, zS),                                      // grip, taper
            vector(rMin, zK), vector(rMin - 1.5 * mm, zTip),                               // thread core, collet cone
            vector(bitR, zTip), vector(bitR, zTip - #bitDepth),                            // blade bore
            vector(zero, zTip - #bitDepth), vector(zero, 6.5 * mm + chamberR),            // 45 deg socket ceiling
            vector(chamberR, 6.5 * mm), vector(chamberR, 4 * mm),                          // head chamber
            vector(lipR, 4 * mm), vector(lipR, 0.5 * mm),                                  // lip
            vector(lipR + 0.5 * mm, zero)] });
    skLineSegment(bodySk, "axis", { "start" : vector(zero, zero), "end" : vector(zero, 10 * mm), "construction" : true });
    skSolve(bodySk);
    revolve(context, id + "Handle", {
        "entities" : qSketchRegion(id + "Handle sketch"),
        "axis" : sketchEntityQuery(id + "Handle sketch", EntityType.EDGE, "axis"),
        "fullRevolve" : true });

    // ---------- Nose thread (M12x2-style, 60 deg) ----------
    cPlane(context, id + "Thread start plane", { "entities" : topPlane, "offset" : zS });
    var noseHelixSk = newSketch(context, id + "Nose helix sketch", { "sketchPlane" : qCreatedBy(id + "Thread start plane", EntityType.FACE) });
    skCircle(noseHelixSk, "helixCircle", { "center" : vector(zero, zero), "radius" : rMin });
    skSolve(noseHelixSk);
    helix(context, id + "Nose helix", {
        "axisType" : AxisType.CIRCLE,
        "edge" : sketchEntityQuery(id + "Nose helix sketch", EntityType.EDGE, "helixCircle"),
        "pathType" : PathType.PITCH,
        "helicalPitch" : #threadPitch,
        "height" : 10 * mm - #threadPitch,
        "handedness" : Direction.CW });
    var noseProfileSk = newSketch(context, id + "Nose thread profile", { "sketchPlane" : frontPlane });
    skPolyline(noseProfileSk, "ridge", { "points" : [
            vector(rMin - 0.1 * P, zS + 0.067 * P), vector(rMaj, zS + 0.4375 * P),
            vector(rMaj, zS + 0.5625 * P), vector(rMin - 0.1 * P, zS + 0.933 * P),
            vector(rMin - 0.1 * P, zS + 0.067 * P)] });
    skSolve(noseProfileSk);
    sweep(context, id + "Nose thread", {
        "profiles" : qSketchRegion(id + "Nose thread profile"),
        "path" : qCreatedBy(id + "Nose helix", EntityType.EDGE),
        "operationType" : NewBodyOperationType.ADD,
        "defaultScope" : false,
        "booleanScope" : qCreatedBy(id + "Handle", EntityType.BODY) });

    // ---------- Collet slots: 3 x 1 mm through the nose, 6 fingers ----------
    cPlane(context, id + "Collet slot plane", { "entities" : topPlane, "offset" : zE - 2 * mm });
    var slotSk = newSketch(context, id + "Collet slot sketch", { "sketchPlane" : qCreatedBy(id + "Collet slot plane", EntityType.FACE) });
    const sl = 7 * mm;
    const w = 0.5 * mm;
    skRectangle(slotSk, "slot0", { "firstCorner" : vector(-sl, -w), "secondCorner" : vector(sl, w) });
    for (var a in [60, 120])
    {
        const c = cos(a * degree);
        const s = sin(a * degree);
        skPolyline(slotSk, "slot" ~ a, { "points" : [
                vector(sl * c - w * s, sl * s + w * c), vector(-sl * c - w * s, -sl * s + w * c),
                vector(-sl * c + w * s, -sl * s - w * c), vector(sl * c + w * s, sl * s - w * c),
                vector(sl * c - w * s, sl * s + w * c)] });
    }
    skSolve(slotSk);
    extrude(context, id + "Collet slots", {
        "entities" : qSketchRegion(id + "Collet slot sketch"),
        "operationType" : NewBodyOperationType.REMOVE,
        "endBound" : BoundingType.THROUGH_ALL,
        "defaultScope" : false,
        "booleanScope" : qCreatedBy(id + "Handle", EntityType.BODY) });

    // ---------- Grip flutes ----------
    var fluteSk = newSketch(context, id + "Grip flute sketch", { "sketchPlane" : topPlane });
    skCircle(fluteSk, "flute", { "center" : vector(R + 1 * mm, zero), "radius" : 2 * mm });
    skSolve(fluteSk);
    extrude(context, id + "Grip flute", {
        "entities" : qSketchRegion(id + "Grip flute sketch"),
        "operationType" : NewBodyOperationType.REMOVE,
        "endBound" : BoundingType.THROUGH_ALL,
        "defaultScope" : false,
        "booleanScope" : qCreatedBy(id + "Handle", EntityType.BODY) });
    circularPattern(context, id + "Grip flute pattern", {
        "patternType" : PatternType.FEATURE,
        "instanceFunction" : [id + "Grip flute"],
        "axis" : sketchEntityQuery(id + "Handle sketch", EntityType.EDGE, "axis"),
        "angle" : 360 * degree,
        "instanceCount" : #fluteCount,
        "equalSpace" : true });

    // ---------- End cap (swivel) ----------
    var capSk = newSketch(context, id + "End cap sketch", { "sketchPlane" : frontPlane });
    skPolyline(capSk, "profile", { "points" : [
            vector(capX, zero), vector(capX + R - 0.8 * mm, zero), vector(capX + R, 0.8 * mm),  // finger face
            vector(capX + R, 3.5 * mm), vector(capX + R - 0.5 * mm, 4 * mm),
            vector(capX + 2.5 * mm, 4 * mm), vector(capX + 2.5 * mm, 8.6 * mm),                // pin shank
            vector(capX + headR, 8.6 * mm), vector(capX + headR, 9.6 * mm),                    // snap head
            vector(capX + 2 * mm, 10.6 * mm), vector(capX, 10.6 * mm), vector(capX, zero)] });
    skLineSegment(capSk, "axis", { "start" : vector(capX, zero), "end" : vector(capX, 10 * mm), "construction" : true });
    skSolve(capSk);
    revolve(context, id + "End cap", {
        "entities" : qSketchRegion(id + "End cap sketch"),
        "axis" : sketchEntityQuery(id + "End cap sketch", EntityType.EDGE, "axis"),
        "fullRevolve" : true });

    cPlane(context, id + "Pin slot plane", { "entities" : topPlane, "offset" : 4.5 * mm });
    var pinSlotSk = newSketch(context, id + "Pin slot sketch", { "sketchPlane" : qCreatedBy(id + "Pin slot plane", EntityType.FACE) });
    skRectangle(pinSlotSk, "slot", { "firstCorner" : vector(capX - 4 * mm, -0.6 * mm), "secondCorner" : vector(capX + 4 * mm, 0.6 * mm) });
    skSolve(pinSlotSk);
    extrude(context, id + "Pin slot", {
        "entities" : qSketchRegion(id + "Pin slot sketch"),
        "operationType" : NewBodyOperationType.REMOVE,
        "endBound" : BoundingType.THROUGH_ALL,
        "defaultScope" : false,
        "booleanScope" : qCreatedBy(id + "End cap", EntityType.BODY) });

    // ---------- Top: collet nut ----------
    var nutSk = newSketch(context, id + "Nut sketch", { "sketchPlane" : frontPlane });
    skPolyline(nutSk, "profile", { "points" : [
            vector(nutX + rMaj + cl + 0.5 * mm, zero), vector(nutX + R - 0.8 * mm, zero), vector(nutX + R, 0.8 * mm),
            vector(nutX + R, nutLength - 0.8 * mm), vector(nutX + R - 0.8 * mm, nutLength),
            vector(nutX + rExit, nutLength),                                                    // blade exit
            vector(nutX + rMin + cl, uK - 4 * cl),                                              // 1:4 clamping cone
            vector(nutX + rMin + cl, 8 * mm + cl + rMaj - rMin),                                   // clearance round the fingers
            vector(nutX + rMaj + cl, 8 * mm + cl),                                              // 45 deg step
            vector(nutX + rMaj + cl, 0.5 * mm), vector(nutX + rMaj + cl + 0.5 * mm, zero)] });
    skLineSegment(nutSk, "axis", { "start" : vector(nutX, zero), "end" : vector(nutX, 10 * mm), "construction" : true });
    skSolve(nutSk);
    revolve(context, id + "Collet nut", {
        "entities" : qSketchRegion(id + "Nut sketch"),
        "axis" : sketchEntityQuery(id + "Nut sketch", EntityType.EDGE, "axis"),
        "fullRevolve" : true });

    var nutHelixSk = newSketch(context, id + "Nut helix sketch", { "sketchPlane" : topPlane });
    skCircle(nutHelixSk, "helixCircle", { "center" : vector(nutX, zero), "radius" : rMaj + cl });
    skSolve(nutHelixSk);
    helix(context, id + "Nut helix", {
        "axisType" : AxisType.CIRCLE,
        "edge" : sketchEntityQuery(id + "Nut helix sketch", EntityType.EDGE, "helixCircle"),
        "pathType" : PathType.PITCH,
        "helicalPitch" : #threadPitch,
        "height" : 8 * mm - #threadPitch,
        "handedness" : Direction.CW });
    var nutProfileSk = newSketch(context, id + "Nut thread profile", { "sketchPlane" : frontPlane });
    skPolyline(nutProfileSk, "ridge", { "points" : [
            vector(nutX + rMaj + cl + 0.1 * P, 0.0048 * P), vector(nutX + rMin + cl, 0.375 * P),
            vector(nutX + rMin + cl, 0.625 * P), vector(nutX + rMaj + cl + 0.1 * P, 0.9952 * P),
            vector(nutX + rMaj + cl + 0.1 * P, 0.0048 * P)] });
    skSolve(nutProfileSk);
    sweep(context, id + "Nut thread", {
        "profiles" : qSketchRegion(id + "Nut thread profile"),
        "path" : qCreatedBy(id + "Nut helix", EntityType.EDGE),
        "operationType" : NewBodyOperationType.ADD,
        "defaultScope" : false,
        "booleanScope" : qCreatedBy(id + "Collet nut", EntityType.BODY) });

    var nutFluteSk = newSketch(context, id + "Nut flute sketch", { "sketchPlane" : topPlane });
    skCircle(nutFluteSk, "flute", { "center" : vector(nutX + R + 1 * mm, zero), "radius" : 2 * mm });
    skSolve(nutFluteSk);
    extrude(context, id + "Nut flute", {
        "entities" : qSketchRegion(id + "Nut flute sketch"),
        "operationType" : NewBodyOperationType.REMOVE,
        "endBound" : BoundingType.THROUGH_ALL,
        "defaultScope" : false,
        "booleanScope" : qCreatedBy(id + "Collet nut", EntityType.BODY) });
    circularPattern(context, id + "Nut flute pattern", {
        "patternType" : PatternType.FEATURE,
        "instanceFunction" : [id + "Nut flute"],
        "axis" : sketchEntityQuery(id + "Nut sketch", EntityType.EDGE, "axis"),
        "angle" : 360 * degree,
        "instanceCount" : #fluteCount,
        "equalSpace" : true });
}
