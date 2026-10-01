"""Native Part Studio scripts: tracing, expressions, validation and feature JSON (offline)."""
from pathlib import Path

from fsgen.native.build import feature_json
from fsgen.native.script import trace as _trace


def trace(src):
    return _trace(src, variables_mode="tree")

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "native"
HDR = 'FeatureScript 3083;\nimport(path : "onshape/std/common.fs", version : "3083.0");\n'


def body(code: str) -> str:
    return HDR + "export function build(context is Context, id is Id)\n{\n" + code + "\n}\n"


def test_examples_trace():
    for f in EXAMPLES.glob("*.fs"):
        tr = trace(f.read_text())
        assert tr.ok, (f.name, tr.error)


def test_variables_become_expressions():
    tr = trace(body('''
        variable(context, "t", 5 * millimeter);
        var sk = newSketch(context, id + "S", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
        skCircle(sk, "c", { "center" : vector(#t * 2, 0 * millimeter), "radius" : #t / 2 });
        skSolve(sk);
        extrude(context, id + "E", { "entities" : qSketchRegion(id + "S"), "endBound" : BoundingType.BLIND, "depth" : #t * 2 + 1 * millimeter });'''))
    assert tr.ok, tr.error
    var, sk, ex = tr.features
    assert var.feature_type == "assignVariable" and var.name == "#t"
    depth = [p for p in feature_json(ex, {"S": "FS1"})["parameters"] if p["parameterId"] == "depth"][0]
    assert depth["expression"] == "#t * 2 + 1 mm"
    params = {p["parameterId"]: p for p in feature_json(ex, {"S": "FS1"})["parameters"]}
    ents = params["entities"]["queries"][0]["queryString"]
    assert params["operationType"]["value"] == "NEW"  # unspecified parameters are sent with their defaults
    assert ents == 'query=qSketchRegion(makeId("FS1"));'
    cons = feature_json(sk, {})["constraints"]
    exprs = {c["constraintType"]: [p.get("expression") for p in c["parameters"] if "expression" in p] for c in cons}
    assert exprs["DIAMETER"] == ["2 * (#t / 2)"]
    assert any(e == ["#t * 2"] for t, e in ((c["constraintType"], [p.get("expression") for p in c["parameters"] if "expression" in p]) for c in cons) if t == "DISTANCE")


def test_unknown_parameter_lists_valid_ids():
    tr = trace(body('''
        var sk = newSketch(context, id + "S", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
        skCircle(sk, "c", { "center" : vector(0, 0) * millimeter, "radius" : 1 * millimeter });
        skSolve(sk);
        extrude(context, id + "E", { "entities" : qSketchRegion(id + "S"), "endDepth" : 5 * millimeter });'''))
    assert not tr.ok and 'unknown parameter "endDepth"' in tr.error and "depth" in tr.error


def test_enum_and_units_checked():
    tr = trace(body('''
        var sk = newSketch(context, id + "S", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
        skCircle(sk, "c", { "center" : vector(0, 0) * millimeter, "radius" : 1 * millimeter });
        skSolve(sk);
        extrude(context, id + "E", { "entities" : qSketchRegion(id + "S"), "depth" : 5 });'''))
    assert not tr.ok and "expects a length" in tr.error
    tr = trace(body('fillet(context, id + "F", { "entities" : qNothing(), "radius" : 1 * millimeter, "filletType" : BoundingType.BLIND });'))
    assert not tr.ok and "FilletType" in tr.error or "filletType" in tr.error


def test_duplicate_names_and_unsolved_sketch():
    tr = trace(body('variable(context, "a", 1 * millimeter); variable(context, "a", 2 * millimeter);'))
    assert not tr.ok and "already defined" in tr.error
    tr = trace(body('var sk = newSketch(context, id + "S", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });'
                    'skCircle(sk, "c", { "center" : vector(0, 0) * millimeter, "radius" : 1 * millimeter });'))
    assert not tr.ok and "never solved" in tr.error


def test_low_level_ops_rejected():
    tr = trace(body('fCuboid(context, id + "b", { "corner1" : vector(0, 0, 0) * millimeter, "corner2" : vector(1, 1, 1) * millimeter });'))
    assert not tr.ok and "standard features" in tr.error


def test_variable_uses_ui_name_template_and_hidden_value():
    tr = trace(body('variable(context, "w", 60 * millimeter);'))
    j = feature_json(tr.features[0], {})
    assert j["name"] == "###name = #value"
    params = {p["parameterId"]: p for p in j["parameters"]}
    assert params["value"]["expression"] == "60 mm" and params["lengthValue"]["expression"] == "60 mm"
    assert params["mode"]["value"] == "ASSIGNED"


def test_any_spec_feature_and_array_parameters():
    tr = trace(body('''
        var a = newSketch(context, id + "A", { "sketchPlane" : qCreatedBy(makeId("Top"), EntityType.FACE) });
        skCircle(a, "c", { "center" : vector(0, 0) * millimeter, "radius" : 5 * millimeter });
        skSolve(a);
        loft(context, id + "Loft", { "sheetProfilesArray" : [
            { "sheetProfileEntities" : qSketchRegion(id + "A") },
            { "sheetProfileEntities" : qSketchRegion(id + "A") }] });'''))
    assert tr.ok, tr.error
    j = feature_json(tr.features[-1], {"A": "FA"})
    arr = [p for p in j["parameters"] if p["parameterId"] == "sheetProfilesArray"][0]
    assert arr["btType"] == "BTMParameterArray-2025" and len(arr["items"]) == 2
    q = arr["items"][0]["parameters"][0]["queries"][0]["queryString"]
    assert q == 'query=qSketchRegion(makeId("FA"));'
    bad = trace(body('loft(context, id + "L", { "sheetProfilesArray" : [{ "profile" : qNothing() }] });'))
    assert not bad.ok and "sheetProfileEntities" in bad.error


def test_names_with_hash_rejected():
    tr = trace(body('fillet(context, id + "#bad", { "entities" : qNothing(), "radius" : 1 * millimeter });'))
    assert not tr.ok and "must not contain '#'" in tr.error


def test_variable_studio_mode_collects_variables():
    tr = _trace(body('''variable(context, "w", 60 * millimeter); variable(context, "h", #w / 2);
        variable(context, "n", 4); variable(context, "a", 30 * degree);'''), variables_mode="studio")
    assert tr.ok and tr.features == []
    assert tr.variable_list == [
        {"name": "w", "expression": "60 mm", "type": "LENGTH", "description": ""},
        {"name": "h", "expression": "#w / 2", "type": "LENGTH", "description": ""},
        {"name": "n", "expression": "4", "type": "NUMBER", "description": ""},
        {"name": "a", "expression": "30 deg", "type": "ANGLE", "description": ""}]
