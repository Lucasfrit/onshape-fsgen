"""FS-Lite language semantics (no geometry)."""
import pytest

from fsgen.fslite.interp import Interpreter
from fsgen.fslite.lexer import FSSyntaxError
from fsgen.fslite.stdlib import StdLib
from fsgen.fslite.values import FSError, Q, Vec, fmt


def ev(body: str):
    lib = StdLib()
    it = Interpreter(lib.b)
    lib.interp = it
    it.load("function f() {\n" + body + "\n}")
    return it.call(it.globals.vars["f"], [], None)


def test_units_and_arithmetic():
    assert fmt(ev("return 2 * millimeter + 1 * centimeter;")) == "12 mm"
    assert ev("return (10 * millimeter) / (2 * millimeter);") == 5.0
    assert fmt(ev("return sqrt(16 * millimeter * millimeter);")) == "4 mm"
    with pytest.raises(FSError, match="units differ"):
        ev("return 1 * millimeter + 1;")
    with pytest.raises(FSError, match="units differ"):
        ev("return 1 * millimeter < 2;")


def test_trig_and_power():
    assert abs(ev("return cos(60 * degree);") - 0.5) < 1e-12
    assert ev("return -2 ^ 2;") == -4.0
    assert ev("return 2 ^ 3 ^ 2;") == 512.0


def test_vectors():
    v = ev("return vector(1, 2, 3) * millimeter + vector(1, 1, 1) * millimeter;")
    assert isinstance(v, Vec) and v.dims == (1, 0) and list(v.a * 1000) == [2, 3, 4]
    assert fmt(ev("return vector(3, 4) * millimeter;")) == "(3, 4) mm"
    assert fmt(ev("return norm(vector(3, 4) * millimeter);")) == "5 mm"
    assert ev("return dot(vector(1, 0, 0), vector(0, 1, 0));") == 0.0
    with pytest.raises(FSError, match="same units"):
        ev("return vector(1 * millimeter, 2);")


def test_value_semantics_of_arrays_and_maps():
    assert ev("var a = [1, 2]; var b = a; b[0] = 9; return a[0];") == 1.0
    assert ev("var m = { 'k' : [1] }; var n = m; n.k[0] = 5; return m.k[0];") == 1.0
    assert ev("var a = []; a = append(a, 3); return size(a);") == 1.0
    assert ev("var m = {}; m['x'] = 2; m.y = 3; return m.x + m.y;") == 5.0


def test_control_flow_and_functions():
    assert ev("var s = 0; for (var i = 0; i < 5; i += 1) { if (i == 3) { continue; } s += i; } return s;") == 7.0
    assert ev("var s = 0; for (var x in [1, 2, 3]) { s += x; } return s;") == 6.0
    assert ev("var s = ''; for (var k, v in { 'a' : 1 }) { s = s ~ k ~ v; } return s;") == "a1"
    assert ev("var f = function(x) { return x * 2; }; return f(4);") == 8.0
    assert ev("return true ? 'y' : 'n';") == "y"
    assert ev("var i = 0; while (true) { i += 1; if (i > 3) { break; } } return i;") == 4.0


def test_ids_and_strings():
    assert ev("return newId() + 'a' + ('b' ~ 2);") == ("a", "b2")
    with pytest.raises(FSError, match="'~'"):
        ev("return 'a' + 'b';")


def test_errors_carry_locations():
    with pytest.raises(FSSyntaxError) as e:
        ev("var x = ;")
    assert e.value.line == 2
    with pytest.raises(FSError) as e:
        ev("return nope;")
    assert e.value.line == 2 and "nope" in str(e.value)
    with pytest.raises(FSError, match="Cannot assign to const"):
        ev("const c = 1; c = 2;")
