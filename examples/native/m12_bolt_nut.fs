FeatureScript 3083;
import(path : "onshape/std/common.fs", version : "3083.0");

// M12x2 bolt and nut that screw together (3D-print clearance on the nut). Threads: helix (axisType CIRCLE on a
// sketch circle) + sweep of a 60-degree thread profile drawn on the Front plane at the helix start (+X).
// ISO basic profile: crest flat P/8, depth 0.5413 P; ridges are embedded 0.1 P into their part so the sweep
// overlaps the core. Bolt at the origin, nut beside it at x = nutX; both stand on the Top plane.
export function build(context is Context, id is Id)
{
    variable(context, "majorDiameter", 12 * millimeter);
    variable(context, "threadPitch", 2 * millimeter);
    variable(context, "clearance", 0.2 * millimeter);      // radial play of the nut thread
    variable(context, "threadTurns", 12);
    variable(context, "nutTurns", 4);

    const mm = millimeter;
    const zero = 0 * millimeter;
    const topPlane = qCreatedBy(makeId("Top"), EntityType.FACE);
    const frontPlane = qCreatedBy(makeId("Front"), EntityType.FACE);
    const rMaj = #majorDiameter / 2;
    const rMin = #majorDiameter / 2 - 0.5413 * #threadPitch;  // bolt core / nut crest (before clearance)
    const headAF = 18 * mm;
    const headHeight = 7.5 * mm;
    const zS = headHeight + 2 * mm;                            // bolt thread start
    const nutX = 30 * mm;
    const nutHeight = (#nutTurns + 1) * #threadPitch;

    // ---------- Bolt: hex head + core ----------
    var headSk = newSketch(context, id + "Head sketch", { "sketchPlane" : topPlane });
    skRegularPolygon(headSk, "hex", { "center" : vector(zero, zero), "firstVertex" : vector(headAF * 0.57735, zero), "sides" : 6 });
    skSolve(headSk);
    extrude(context, id + "Bolt", { "entities" : qSketchRegion(id + "Head sketch"), "endBound" : BoundingType.BLIND, "depth" : headHeight });

    cPlane(context, id + "Head top", { "entities" : topPlane, "offset" : headHeight });
    var coreSk = newSketch(context, id + "Core sketch", { "sketchPlane" : qCreatedBy(id + "Head top", EntityType.FACE) });
    skCircle(coreSk, "core", { "center" : vector(zero, zero), "radius" : rMin });
    skSolve(coreSk);
    extrude(context, id + "Core", {
        "entities" : qSketchRegion(id + "Core sketch"),
        "operationType" : NewBodyOperationType.ADD,
        "endBound" : BoundingType.BLIND,
        "depth" : (#threadTurns + 2) * #threadPitch,
        "defaultScope" : false,
        "booleanScope" : qCreatedBy(id + "Bolt", EntityType.BODY) });

    // ---------- Bolt thread ----------
    cPlane(context, id + "Thread start", { "entities" : topPlane, "offset" : zS });
    var boltHelixSk = newSketch(context, id + "Bolt helix sketch", { "sketchPlane" : qCreatedBy(id + "Thread start", EntityType.FACE) });
    skCircle(boltHelixSk, "helixCircle", { "center" : vector(zero, zero), "radius" : rMin });
    skSolve(boltHelixSk);
    helix(context, id + "Bolt helix", {
        "axisType" : AxisType.CIRCLE,
        "edge" : sketchEntityQuery(id + "Bolt helix sketch", EntityType.EDGE, "helixCircle"),
        "pathType" : PathType.TURNS_PITCH,
        "revolutions" : #threadTurns,
        "helicalPitch" : #threadPitch,
        "handedness" : Direction.CW });

    var boltProfileSk = newSketch(context, id + "Bolt thread profile", { "sketchPlane" : frontPlane });
    skPolyline(boltProfileSk, "ridge", { "points" : [
            vector(rMin - 0.1 * #threadPitch, zS + 0.0670 * #threadPitch),
            vector(rMaj, zS + 0.4375 * #threadPitch),
            vector(rMaj, zS + 0.5625 * #threadPitch),
            vector(rMin - 0.1 * #threadPitch, zS + 0.9330 * #threadPitch),
            vector(rMin - 0.1 * #threadPitch, zS + 0.0670 * #threadPitch)] });
    skSolve(boltProfileSk);
    sweep(context, id + "Bolt thread", {
        "profiles" : qSketchRegion(id + "Bolt thread profile"),
        "path" : qCreatedBy(id + "Bolt helix", EntityType.EDGE),
        "operationType" : NewBodyOperationType.ADD,
        "defaultScope" : false,
        "booleanScope" : qCreatedBy(id + "Bolt", EntityType.BODY) });

    // ---------- Nut: hex with bore ----------
    var nutSk = newSketch(context, id + "Nut sketch", { "sketchPlane" : topPlane });
    skRegularPolygon(nutSk, "hex", { "center" : vector(nutX, zero), "firstVertex" : vector(nutX + headAF * 0.57735, zero), "sides" : 6 });
    skCircle(nutSk, "bore", { "center" : vector(nutX, zero), "radius" : rMaj + #clearance });
    skSolve(nutSk);
    extrude(context, id + "Nut", { "entities" : qSketchRegion(id + "Nut sketch", true), "endBound" : BoundingType.BLIND, "depth" : nutHeight });

    // ---------- Nut thread: the bolt's gap, moved out by the clearance ----------
    var nutHelixSk = newSketch(context, id + "Nut helix sketch", { "sketchPlane" : topPlane });
    skCircle(nutHelixSk, "helixCircle", { "center" : vector(nutX, zero), "radius" : rMaj + #clearance });
    skSolve(nutHelixSk);
    helix(context, id + "Nut helix", {
        "axisType" : AxisType.CIRCLE,
        "edge" : sketchEntityQuery(id + "Nut helix sketch", EntityType.EDGE, "helixCircle"),
        "pathType" : PathType.TURNS_PITCH,
        "revolutions" : #nutTurns,
        "helicalPitch" : #threadPitch,
        "handedness" : Direction.CW });

    var nutProfileSk = newSketch(context, id + "Nut thread profile", { "sketchPlane" : frontPlane });
    skPolyline(nutProfileSk, "ridge", { "points" : [
            vector(nutX + rMaj + #clearance + 0.1 * #threadPitch, 0.0048 * #threadPitch),
            vector(nutX + rMin + #clearance, 0.375 * #threadPitch),
            vector(nutX + rMin + #clearance, 0.625 * #threadPitch),
            vector(nutX + rMaj + #clearance + 0.1 * #threadPitch, 0.9952 * #threadPitch),
            vector(nutX + rMaj + #clearance + 0.1 * #threadPitch, 0.0048 * #threadPitch)] });
    skSolve(nutProfileSk);
    sweep(context, id + "Nut thread", {
        "profiles" : qSketchRegion(id + "Nut thread profile"),
        "path" : qCreatedBy(id + "Nut helix", EntityType.EDGE),
        "operationType" : NewBodyOperationType.ADD,
        "defaultScope" : false,
        "booleanScope" : qCreatedBy(id + "Nut", EntityType.BODY) });
}
