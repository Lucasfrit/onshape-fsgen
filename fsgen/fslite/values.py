"""Runtime value types for FS-Lite: unit-carrying quantities, vectors, ids, planes..."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np


class FSError(Exception):
    """Runtime error raised by FS-Lite programs (mirrors an Onshape regen error)."""

    def __init__(self, msg: str, node=None):
        self.msg = msg
        self.line = getattr(node, "line", 0)
        self.col = getattr(node, "col", 0)
        super().__init__(msg)

    def with_node(self, node):
        if not self.line and node is not None:
            self.line, self.col = node.line, node.col
        return self

    def __str__(self):
        return f"line {self.line}:{self.col}: {self.msg}" if self.line else self.msg


# ---------------------------------------------------------------- units
# Dimensions are (length exponent, angle exponent). Values are stored in SI (meter, radian).
DIM_NAMES = ("meter", "radian")


@dataclass(frozen=True)
class Q:
    v: float
    dims: tuple[int, int]
    # Onshape expression this value came from (e.g. "#width / 2"); None for plain literals.
    expr: str | None = field(default=None, compare=False)

    def __repr__(self):
        return fmt(self)


def mk(v: float, dims, expr: str | None = None) -> object:
    dims = tuple(dims)
    if expr is not None:
        return Q(float(v), dims, expr)
    return float(v) if dims == (0, 0) else Q(float(v), dims)


# ---------------------------------------------------------------- Onshape expressions
_UNIT_TEXT = {(1, 0): "mm", (0, 1): "deg"}


def literal_expr(x) -> str:
    """Onshape expression text for a plain value (5 mm, 30 deg, 4)."""
    v, d = mag(x), dims_of(x)
    if d == (0, 0):
        return _num(v)
    if d == (1, 0):
        return f"{_num(v * 1000)} mm"
    if d == (0, 1):
        return f"{_num(math.degrees(v))} deg"
    if d[1] == 0:
        return f"{_num(v * 1000 ** d[0])} mm^{d[0]}"
    raise FSError(f"cannot express {fmt(x)} in an Onshape expression")


def expr_of(x) -> str:
    if isinstance(x, Q) and x.expr is not None:
        return x.expr
    return literal_expr(x)


def has_expr(x) -> bool:
    return isinstance(x, Q) and x.expr is not None


def _wrap(e: str) -> str:
    return f"({e})" if any(op in e for op in (" + ", " - ", " * ", " / ", " ^ ")) else e


def combine(op: str, a, b) -> str | None:
    """Expression for `a op b`, or None if neither side refers to a variable."""
    if not (has_expr(a) or has_expr(b)):
        return None
    ea, eb = expr_of(a), expr_of(b)
    za = not has_expr(a) and mag(a) == 0
    zb = not has_expr(b) and mag(b) == 0
    if op == "+" and za:
        return eb
    if op in ("+", "-") and zb:
        return ea
    if op == "*" and not has_expr(b) and dims_of(b) == (0, 0) and mag(b) == 1:
        return ea
    if op in ("+",):
        return f"{ea} + {eb}"
    if op == "-":
        return f"{ea} - {_wrap(eb)}"
    return f"{_wrap(ea)} {op} {_wrap(eb)}"


def dims_of(x) -> tuple[int, int]:
    if isinstance(x, Q):
        return x.dims
    if isinstance(x, Vec):
        return x.dims
    if isinstance(x, (int, float, bool)):
        return (0, 0)
    raise FSError(f"expected a number or value with units, got {type_name(x)}")


def mag(x) -> float:
    """Magnitude in SI units."""
    if isinstance(x, Q):
        return x.v
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return float(x)
    raise FSError(f"expected a number or value with units, got {type_name(x)}")


def unit_str(dims) -> str:
    parts = []
    for name, p in zip(DIM_NAMES, dims):
        if p:
            parts.append(name if p == 1 else f"{name}^{p}")
    return "*".join(parts) or "unitless"


UNITS = {
    "meter": Q(1.0, (1, 0)),
    "centimeter": Q(0.01, (1, 0)),
    "millimeter": Q(0.001, (1, 0)),
    "inch": Q(0.0254, (1, 0)),
    "foot": Q(0.3048, (1, 0)),
    "yard": Q(0.9144, (1, 0)),
    "radian": Q(1.0, (0, 1)),
    "degree": Q(math.pi / 180, (0, 1)),
}


# ---------------------------------------------------------------- vectors
class Vec:
    """FeatureScript Vector: array of numbers sharing the same units."""

    __slots__ = ("a", "dims", "exprs")

    def __init__(self, a, dims=(0, 0), exprs=None):
        self.a = np.asarray(a, dtype=float)
        self.dims = tuple(dims)
        # per-component Onshape expressions (None entries = plain literal); None if no variables involved
        self.exprs = exprs if exprs and any(e is not None for e in exprs) else None

    @staticmethod
    def of(items) -> "Vec":
        if len(items) == 0:
            raise FSError("vector() needs at least one component")
        d = dims_of(items[0])
        for it in items:
            if isinstance(it, Vec) or dims_of(it) != d:
                raise FSError("all vector components must have the same units")
        return Vec([mag(i) for i in items], d, [i.expr if isinstance(i, Q) else None for i in items])

    def item(self, i: int):
        return mk(self.a[i], self.dims, self.exprs[i] if self.exprs else None)

    def items(self):
        return [self.item(i) for i in range(len(self.a))]

    def __len__(self):
        return len(self.a)

    def __repr__(self):
        return fmt(self)


def as_point3(x, what="point") -> np.ndarray:
    if not isinstance(x, Vec) or len(x) != 3 or x.dims != (1, 0):
        raise FSError(f"{what} must be a 3D length vector like vector(x, y, z) * millimeter, got {fmt(x)}")
    return x.a.copy()


def as_point2(x, what="point") -> np.ndarray:
    if not isinstance(x, Vec) or len(x) != 2 or x.dims != (1, 0):
        raise FSError(f"{what} must be a 2D length vector like vector(x, y) * millimeter, got {fmt(x)}")
    return x.a.copy()


def as_dir3(x, what="direction") -> np.ndarray:
    if not isinstance(x, Vec) or len(x) != 3 or x.dims != (0, 0):
        raise FSError(f"{what} must be a unitless 3D vector like vector(0, 0, 1), got {fmt(x)}")
    n = np.linalg.norm(x.a)
    if n < 1e-12:
        raise FSError(f"{what} must be non-zero")
    return x.a / n


def as_length(x, what="length") -> float:
    if not isinstance(x, Q) or x.dims != (1, 0):
        raise FSError(f"{what} must be a length (e.g. 5 * millimeter), got {fmt(x)}")
    return x.v


def as_angle(x, what="angle") -> float:
    if isinstance(x, Q) and x.dims == (0, 1):
        return x.v
    raise FSError(f"{what} must be an angle (e.g. 90 * degree), got {fmt(x)}")


# ---------------------------------------------------------------- ids / enums
class Id(tuple):
    def __repr__(self):
        return "id(" + ".".join(self) + ")"


@dataclass(frozen=True)
class EnumVal:
    enum: str
    name: str

    def __repr__(self):
        return f"{self.enum}.{self.name}"


class EnumType:
    def __init__(self, name: str, members):
        self.name = name
        self.members = {m: EnumVal(name, m) for m in members}

    def get(self, member):
        if member not in self.members:
            raise FSError(f"{self.name} has no member {member}")
        return self.members[member]


# ---------------------------------------------------------------- geometry values
def default_x(normal: np.ndarray) -> np.ndarray:
    """Onshape's default plane x-direction (reverse-engineered, see README)."""
    ax, ay, az = np.abs(normal)
    if ay >= ax and ay >= az:
        ref = np.array([1.0, 0, 0])
    elif ax >= az:
        ref = np.array([0, 0, 1.0])
    else:
        ref = np.array([0, 1.0, 0])
    x = np.cross(ref, normal)
    return x / np.linalg.norm(x)


@dataclass
class Plane:
    origin: np.ndarray  # meters
    normal: np.ndarray
    x: np.ndarray

    @property
    def y(self) -> np.ndarray:
        return np.cross(self.normal, self.x)

    def to_world(self, uv) -> np.ndarray:
        return self.origin + uv[0] * self.x + uv[1] * self.y

    def field(self, name):
        if name == "origin":
            return Vec(self.origin, (1, 0))
        if name == "normal":
            return Vec(self.normal)
        if name == "x":
            return Vec(self.x)
        raise FSError(f"Plane has no field '{name}'")


@dataclass
class Line:
    origin: np.ndarray
    direction: np.ndarray

    def field(self, name):
        if name == "origin":
            return Vec(self.origin, (1, 0))
        if name == "direction":
            return Vec(self.direction)
        raise FSError(f"Line has no field '{name}'")


@dataclass
class Transform:
    m: np.ndarray  # 4x4, translation in meters

    def apply_point(self, p):
        return (self.m @ np.append(p, 1.0))[:3]

    def apply_dir(self, d):
        return self.m[:3, :3] @ d


@dataclass
class Box3d:
    lo: np.ndarray
    hi: np.ndarray

    def field(self, name):
        if name == "minCorner":
            return Vec(self.lo, (1, 0))
        if name == "maxCorner":
            return Vec(self.hi, (1, 0))
        raise FSError(f"Box3d has no field '{name}'")


# ---------------------------------------------------------------- formatting
def type_name(x) -> str:
    if x is None:
        return "undefined"
    if isinstance(x, bool):
        return "boolean"
    if isinstance(x, (int, float)):
        return "number"
    if isinstance(x, Q):
        return f"ValueWithUnits({unit_str(x.dims)})"
    if isinstance(x, str):
        return "string"
    if isinstance(x, Vec):
        return "Vector"
    if isinstance(x, list):
        return "array"
    if isinstance(x, dict):
        return "map"
    if isinstance(x, Id):
        return "Id"
    return type(x).__name__


def _num(v: float) -> str:
    if abs(v - round(v)) < 1e-9 and abs(v) < 1e15:
        return str(int(round(v)))
    return f"{v:.6g}"


def fmt(x) -> str:
    if x is None:
        return "undefined"
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, (int, float)):
        return _num(x)
    if isinstance(x, Q):
        if x.dims == (1, 0):
            return f"{_num(x.v * 1000)} mm"
        if x.dims == (0, 1):
            return f"{_num(math.degrees(x.v))} deg"
        return f"{_num(x.v)} {unit_str(x.dims)}"
    if isinstance(x, str):
        return x
    if isinstance(x, Vec):
        if x.dims == (1, 0):
            return "(" + ", ".join(_num(v * 1000) for v in x.a) + ") mm"
        return "(" + ", ".join(_num(v) for v in x.a) + ")" + ("" if x.dims == (0, 0) else " " + unit_str(x.dims))
    if isinstance(x, list):
        return "[" + ", ".join(fmt(i) for i in x) + "]"
    if isinstance(x, dict):
        return "{" + ", ".join(f"{fmt(k)}: {fmt(v)}" for k, v in x.items()) + "}"
    return repr(x)
