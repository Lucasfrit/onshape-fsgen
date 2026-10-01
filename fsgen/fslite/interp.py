"""Tree-walking evaluator for FS-Lite."""
from __future__ import annotations

import math

import numpy as np

from .lexer import FSSyntaxError
from .parser import Node, parse
from .values import (combine, expr_of, has_expr, Box3d, EnumType, EnumVal, FSError, Id, Line, Plane, Q, Transform, Vec, dims_of, fmt, mag, mk,
                     type_name)


UNIT_MM = Q(0.001, (1, 0))


class _Return(Exception):
    def __init__(self, value):
        self.value = value


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


class FSThrow(FSError):
    def __init__(self, value, node=None):
        super().__init__(f"thrown: {fmt(value)}", node)
        self.value = value


class Scope:
    __slots__ = ("vars", "consts", "parent")

    def __init__(self, parent=None):
        self.vars: dict = {}
        self.consts: set = set()
        self.parent = parent

    def lookup(self, name):
        s = self
        while s is not None:
            if name in s.vars:
                return s
            s = s.parent
        return None

    def get(self, name, node):
        s = self.lookup(name)
        if s is None:
            raise FSError(f"Variable {name} not found", node)
        return s.vars[name]

    def declare(self, name, value, const, node):
        if name in self.vars:
            raise FSError(f"Variable {name} is already declared in this scope", node)
        self.vars[name] = value
        if const:
            self.consts.add(name)

    def set(self, name, value, node):
        s = self.lookup(name)
        if s is None:
            raise FSError(f"Variable {name} not found (declare it with 'var' first)", node)
        if name in s.consts:
            raise FSError(f"Cannot assign to const {name}", node)
        s.vars[name] = value


class Function:
    def __init__(self, node: Node, closure: Scope, interp: "Interpreter"):
        self.node, self.closure, self.interp = node, closure, interp
        self.name = node.name

    def __call__(self, *args):
        return self.interp.call_user(self, list(args), None)

    def __repr__(self):
        return f"function {self.name}"


class Builtin:
    def __init__(self, name, fn):
        self.name, self.fn = name, fn

    def __repr__(self):
        return f"builtin {self.name}"


class FeatureFn:
    """Result of defineFeature(...)."""

    def __init__(self, fn: Function):
        self.fn = fn


def copy_value(v):
    """FeatureScript arrays and maps have value semantics."""
    if isinstance(v, list):
        return [copy_value(x) for x in v]
    if isinstance(v, dict):
        return {k: copy_value(x) for k, x in v.items()}
    return v


def truthy(v, node):
    if isinstance(v, bool):
        return v
    raise FSError(f"condition must be a boolean, got {type_name(v)}", node)


def map_key(v):
    if isinstance(v, (str, bool)) or v is None:
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, EnumVal):
        return v
    if isinstance(v, Q):
        return v
    raise FSError(f"unsupported map key type {type_name(v)}")


def to_index(v, n, node):
    if isinstance(v, Q) and v.dims == (0, 0):
        v = v.v
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not float(v).is_integer():
        raise FSError(f"array index must be an integer, got {fmt(v)}", node)
    i = int(v)
    if i < 0 or i >= n:
        raise FSError(f"array index {i} out of bounds (size {n})", node)
    return i


# ---------------------------------------------------------------- arithmetic
def add(a, b, node, sign=1):
    if isinstance(a, Id) and isinstance(b, str) and sign == 1:
        if not b:
            raise FSError("Id components must be non-empty strings", node)
        return Id(tuple(a) + (b,))
    if isinstance(a, Vec) and isinstance(b, Vec):
        if len(a) != len(b):
            raise FSError("cannot add vectors of different sizes", node)
        if a.dims != b.dims:
            raise FSError(f"cannot add vectors with different units ({fmt(a)} and {fmt(b)})", node)
        exprs = None
        if a.exprs or b.exprs:
            exprs = [combine("+" if sign == 1 else "-", x, y) for x, y in zip(a.items(), b.items())]
        return Vec(a.a + sign * b.a, a.dims, exprs)
    if isinstance(a, list) and isinstance(b, list) and sign == 1:
        raise FSError("use concatenateArrays([a, b]) to join arrays", node)
    if isinstance(a, (int, float, Q)) and isinstance(b, (int, float, Q)) and not isinstance(a, bool) and not isinstance(b, bool):
        da, db = dims_of(a), dims_of(b)
        if da != db:
            raise FSError(f"cannot {'add' if sign == 1 else 'subtract'} {fmt(a)} and {fmt(b)}: units differ", node)
        return mk(mag(a) + sign * mag(b), da, combine("+" if sign == 1 else "-", a, b))
    if isinstance(a, str) and isinstance(b, str) and sign == 1:
        raise FSError("use '~' to concatenate strings", node)
    raise FSError(f"cannot apply '{'+' if sign == 1 else '-'}' to {type_name(a)} and {type_name(b)}", node)


def mul(a, b, node):
    if isinstance(a, Transform) and isinstance(b, Transform):
        return Transform(a.m @ b.m)
    if isinstance(a, Transform) and isinstance(b, Vec):
        if b.dims == (1, 0) and len(b) == 3:
            return Vec(a.apply_point(b.a), (1, 0))
        if b.dims == (0, 0) and len(b) == 3:
            return Vec(a.apply_dir(b.a))
    if isinstance(a, Vec) and isinstance(b, (int, float, Q)) and not isinstance(b, bool):
        db = dims_of(b)
        exprs = [combine("*", x, b) for x in a.items()] if (a.exprs or has_expr(b)) else None
        return Vec(a.a * mag(b), (a.dims[0] + db[0], a.dims[1] + db[1]), exprs)
    if isinstance(b, Vec) and isinstance(a, (int, float, Q)) and not isinstance(a, bool):
        return mul(b, a, node)
    if isinstance(a, (int, float, Q)) and isinstance(b, (int, float, Q)) and not isinstance(a, bool) and not isinstance(b, bool):
        da, db = dims_of(a), dims_of(b)
        return mk(mag(a) * mag(b), (da[0] + db[0], da[1] + db[1]), combine("*", a, b))
    raise FSError(f"cannot multiply {type_name(a)} by {type_name(b)}", node)


def div(a, b, node):
    if isinstance(b, (int, float, Q)) and not isinstance(b, bool):
        if mag(b) == 0:
            raise FSError("division by zero", node)
        db = dims_of(b)
        if isinstance(a, Vec):
            exprs = [combine("/", x, b) for x in a.items()] if (a.exprs or has_expr(b)) else None
            return Vec(a.a / mag(b), (a.dims[0] - db[0], a.dims[1] - db[1]), exprs)
        if isinstance(a, (int, float, Q)) and not isinstance(a, bool):
            da = dims_of(a)
            return mk(mag(a) / mag(b), (da[0] - db[0], da[1] - db[1]), combine("/", a, b))
    raise FSError(f"cannot divide {type_name(a)} by {type_name(b)}", node)


def power(a, b, node):
    if isinstance(b, bool) or not (isinstance(b, (int, float)) or (isinstance(b, Q) and b.dims == (0, 0))):
        raise FSError("exponent must be a number", node)
    if isinstance(b, Q) and b.dims == (0, 0):
        b = b.v
    if isinstance(a, (int, float)) and not isinstance(a, bool):
        return float(a) ** b
    if isinstance(a, Q):
        if a.dims == (0, 0):
            return mk(a.v ** b, (0, 0), combine("^", a, float(b)))
        if not float(b).is_integer():
            raise FSError("units can only be raised to integer powers (use sqrt)", node)
        return mk(a.v ** b, (a.dims[0] * int(b), a.dims[1] * int(b)), combine("^", a, float(b)))
    raise FSError(f"cannot raise {type_name(a)} to a power", node)


def equals(a, b) -> bool:
    if isinstance(a, Vec) and isinstance(b, Vec):
        return a.dims == b.dims and a.a.shape == b.a.shape and bool(np.all(a.a == b.a))
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(equals(x, y) for x, y in zip(a, b))
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, (int, float, Q)) and isinstance(b, (int, float, Q)):
        return dims_of(a) == dims_of(b) and mag(a) == mag(b)
    return a == b


def compare(op, a, b, node):
    if isinstance(a, str) and isinstance(b, str):
        x, y = a, b
    else:
        if isinstance(a, bool) or isinstance(b, bool) or not isinstance(a, (int, float, Q)) or not isinstance(b, (int, float, Q)):
            raise FSError(f"cannot compare {type_name(a)} and {type_name(b)}", node)
        if dims_of(a) != dims_of(b):
            raise FSError(f"cannot compare {fmt(a)} and {fmt(b)}: units differ", node)
        x, y = mag(a), mag(b)
    return {"<": x < y, ">": x > y, "<=": x <= y, ">=": x >= y}[op]


def concat(a, b, node):
    def s(v):
        if isinstance(v, str):
            return v
        if isinstance(v, (int, float, Q, bool)) or v is None:
            return fmt(v)
        raise FSError(f"cannot concatenate {type_name(v)} with '~'", node)
    return s(a) + s(b)


TYPE_CHECKS = {
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, (list, Vec)),
    "map": lambda v: isinstance(v, dict),
    "function": lambda v: isinstance(v, (Function, Builtin)),
    "ValueWithUnits": lambda v: isinstance(v, Q),
    "Vector": lambda v: isinstance(v, Vec),
    "Id": lambda v: isinstance(v, Id),
    "undefined": lambda v: v is None,
}


# ---------------------------------------------------------------- interpreter
class Interpreter:
    MAX_STEPS = 5_000_000

    def __init__(self, builtins: dict):
        self.globals = Scope()
        for k, v in builtins.items():
            self.globals.vars[k] = v
            self.globals.consts.add(k)
        self.steps = 0
        self.call_depth = 0
        self.output: list[str] = []
        self.features: dict[str, tuple[FeatureFn, dict]] = {}
        self.variables: dict = {}  # Onshape #variables (name -> value)

    # -- program -----------------------------------------------------------
    def load(self, src: str):
        prog = parse(src)
        self.program = prog
        # Hoist functions so declaration order does not matter.
        for d in prog.decls:
            if d.kind == "fundecl":
                self.globals.declare(d.name, Function(d.fn, self.globals, self), True, d)
        for d in prog.decls:
            if d.kind == "const":
                val = self.eval(d.value, self.globals)
                self.globals.declare(d.name, val, True, d)
                if isinstance(val, FeatureFn):
                    self.features[d.name] = (val, self.eval(d.annotation, self.globals) if d.annotation else {})
        return prog

    # -- feature parameters --------------------------------------------------
    PARAM_FUNCS = {"isLength": "length", "isAngle": "angle", "isInteger": "integer", "isReal": "real"}

    def collect_parameters(self, ff: "FeatureFn", overrides: dict | None = None):
        """Build `definition` from the precondition's parameter declarations.

        Returns (definition, params) where params lists {name, kind, label, default, min, max}.
        Overrides (name -> value) replace defaults; conditional parameters see earlier values.
        """
        definition: dict = {}
        params: list[dict] = []
        overrides = overrides or {}
        pre = ff.fn.node.precondition
        if pre is None:
            return definition, params
        scope = Scope(self.globals)
        scope.vars["definition"] = definition

        def param_name(node):
            if node.kind == "member" and node.obj.kind == "name" and node.obj.name == "definition":
                return node.name
            return None

        def visit(st):
            ann = getattr(st, "annotation", None)
            ann = self.eval(ann, scope) if ann is not None else {}
            label = ann.get("Name") if isinstance(ann, dict) else None
            if st.kind == "block":
                for x in st.stmts:
                    visit(x)
            elif st.kind == "if":
                if truthy(self.eval(st.cond, scope), st.cond):
                    visit(st.then)
                elif st.other is not None:
                    visit(st.other)
            elif st.kind == "exprstmt":
                e = st.expr
                if e.kind == "call" and e.fn.kind == "name" and e.fn.name in self.PARAM_FUNCS and e.args:
                    name = param_name(e.args[0])
                    if name is None:
                        return
                    kind = self.PARAM_FUNCS[e.fn.name]
                    spec = self.eval(e.args[1], scope) if len(e.args) > 1 else None
                    lo = default = hi = None
                    if isinstance(spec, dict) and spec:
                        unit, rng = next(iter(spec.items()))
                        if isinstance(rng, list) and len(rng) == 3:
                            lo, default, hi = (mul(v, unit, e) for v in rng)
                    if default is None:
                        default = {"length": UNIT_MM * 25, "angle": Q(math.radians(30), (0, 1)),
                                   "integer": 2.0, "real": 1.0}[kind]
                    params.append({"name": name, "kind": kind, "label": label, "default": default,
                                   "min": lo, "max": hi})
                    definition[name] = overrides.get(name, default)
                elif e.kind == "istype":
                    name = param_name(e.expr)
                    if name is None:
                        return
                    if e.type == "boolean":
                        default = bool(ann.get("Default", False)) if isinstance(ann, dict) else False
                        params.append({"name": name, "kind": "boolean", "label": label, "default": default})
                        definition[name] = overrides.get(name, default)
                    elif e.type == "string":
                        default = ann.get("Default", "") if isinstance(ann, dict) else ""
                        params.append({"name": name, "kind": "string", "label": label, "default": default})
                        definition[name] = overrides.get(name, default)
        visit(pre)
        unknown = set(overrides) - {p["name"] for p in params}
        if unknown:
            raise FSError(f"unknown parameter(s): {', '.join(sorted(unknown))}; "
                          f"available: {', '.join(p['name'] for p in params) or 'none'}")
        return definition, params

    def unknown_name_message(self, name, member=None, scope=None) -> str:
        """'Variable X not found' plus the closest names (and, for X.MEMBER, enums that have MEMBER)."""
        import difflib

        names = set()
        s = scope
        while s is not None:
            names |= set(s.vars)
            s = s.parent
        msg = f"Variable {name} not found."
        if member is not None:
            enums = sorted(n for n in names if isinstance(self.globals.vars.get(n), EnumType)
                           and member in self.globals.vars[n].members)
            if enums:
                msg += f" Enums with member {member}: {', '.join(enums[:8])}."
        close = difflib.get_close_matches(name, sorted(names), n=4, cutoff=0.6)
        if close:
            msg += f" Did you mean: {', '.join(close)}?"
        return msg

    # -- calls -------------------------------------------------------------
    def call(self, fn, args, node):
        if isinstance(fn, Builtin):
            try:
                return fn.fn(*args)
            except FSError as e:
                raise e.with_node(node)
            except TypeError as e:
                raise FSError(f"{fn.name}: wrong number/type of arguments ({e})", node)
        if isinstance(fn, Function):
            return self.call_user(fn, args, node)
        if isinstance(fn, FeatureFn):
            return self.call_user(fn.fn, args, node)
        raise FSError(f"{type_name(fn)} is not callable", node)

    def call_user(self, fn: Function, args, node):
        params = fn.node.params
        if len(args) > len(params):
            raise FSError(f"{fn.name}: expected {len(params)} arguments, got {len(args)}", node)
        scope = Scope(fn.closure)
        for i, (pname, default) in enumerate(params):
            if i < len(args):
                val = copy_value(args[i])
            elif default is not None:
                val = self.eval(default, scope)
            else:
                raise FSError(f"{fn.name}: missing argument '{pname}'", node)
            scope.vars[pname] = val
        self.call_depth += 1
        if self.call_depth > 200:
            raise FSError("maximum call depth exceeded (infinite recursion?)", node)
        try:
            self.exec_block(fn.node.body, scope, new_scope=False)
        except _Return as r:
            return r.value
        finally:
            self.call_depth -= 1
        return None

    # -- statements --------------------------------------------------------
    def exec_block(self, block: Node, scope: Scope, new_scope=True):
        s = Scope(scope) if new_scope else scope
        for st in block.stmts:
            self.exec(st, s)

    def exec(self, st: Node, scope: Scope):
        self.steps += 1
        if self.steps > self.MAX_STEPS:
            raise FSError("step limit exceeded (infinite loop?)", st)
        k = st.kind
        if k == "exprstmt":
            self.eval(st.expr, scope)
        elif k == "vardecl":
            for name, init in st.decls:
                val = copy_value(self.eval(init, scope)) if init is not None else None
                scope.declare(name, val, st.const, st)
        elif k == "assign":
            self.assign(st, scope)
        elif k == "block":
            self.exec_block(st, scope)
        elif k == "if":
            if truthy(self.eval(st.cond, scope), st.cond):
                self.exec(st.then, Scope(scope))
            elif st.other is not None:
                self.exec(st.other, Scope(scope))
        elif k == "for":
            s = Scope(scope)
            if st.init is not None:
                self.exec(st.init, s)
            while st.cond is None or truthy(self.eval(st.cond, s), st.cond):
                try:
                    self.exec(st.body, Scope(s))
                except _Break:
                    break
                except _Continue:
                    pass
                if st.update is not None:
                    self.exec(st.update, s)
                self.steps += 1
                if self.steps > self.MAX_STEPS:
                    raise FSError("step limit exceeded (infinite loop?)", st)
        elif k == "forin":
            it = self.eval(st.iterable, scope)
            n1, n2 = st.names
            if isinstance(it, Vec):
                items = [(i, it.item(i)) for i in range(len(it))]
            elif isinstance(it, list):
                items = list(enumerate(copy_value(it)))
            elif isinstance(it, dict):
                items = list(copy_value(it).items())
            else:
                raise FSError(f"cannot iterate over {type_name(it)}", st)
            for key, val in items:
                s = Scope(scope)
                if n2 is None:
                    s.vars[n1] = key if isinstance(it, dict) else val
                else:
                    s.vars[n1], s.vars[n2] = key, val
                try:
                    self.exec(st.body, s)
                except _Break:
                    break
                except _Continue:
                    pass
        elif k == "while":
            while truthy(self.eval(st.cond, scope), st.cond):
                try:
                    self.exec(st.body, Scope(scope))
                except _Break:
                    break
                except _Continue:
                    pass
                self.steps += 1
                if self.steps > self.MAX_STEPS:
                    raise FSError("step limit exceeded (infinite loop?)", st)
        elif k == "return":
            raise _Return(None if st.value is None else self.eval(st.value, scope))
        elif k == "break":
            raise _Break()
        elif k == "continue":
            raise _Continue()
        elif k == "throw":
            raise FSThrow(self.eval(st.value, scope), st)
        elif k == "try":
            try:
                self.exec_block(st.body, scope)
            except FSError as e:
                if st.handler is not None:
                    s = Scope(scope)
                    s.vars[st.var] = getattr(e, "value", e.msg)
                    self.exec_block(st.handler, s, new_scope=False)
        elif k == "empty":
            pass
        else:
            raise FSError(f"unsupported statement {k}", st)

    def assign(self, st: Node, scope: Scope):
        value = self.eval(st.value, scope)
        if st.op != "=":
            cur = self.eval(st.target, scope)
            value = self.binop(st.op[0], cur, value, st)
        value = copy_value(value)
        t = st.target
        if t.kind == "name":
            scope.set(t.name, value, st)
            return
        # index/member assignment: resolve the container chain in place
        container = self.lvalue_container(t.obj, scope)
        if t.kind == "member":
            if not isinstance(container, dict):
                raise FSError(f"cannot set field '{t.name}' on {type_name(container)}", st)
            container[t.name] = value
        else:
            idx = self.eval(t.idx, scope)
            if isinstance(container, list):
                container[to_index(idx, len(container), st)] = value
            elif isinstance(container, dict):
                container[map_key(idx)] = value
            else:
                raise FSError(f"cannot index-assign into {type_name(container)}", st)

    def lvalue_container(self, node: Node, scope: Scope):
        if node.kind == "name":
            s = scope.lookup(node.name)
            if s is None:
                raise FSError(f"Variable {node.name} not found", node)
            if node.name in s.consts:
                raise FSError(f"Cannot modify const {node.name}", node)
            return s.vars[node.name]
        if node.kind in ("member", "index"):
            parent = self.lvalue_container(node.obj, scope)
            if node.kind == "member":
                key = node.name
            else:
                key = self.eval(node.idx, scope)
            if isinstance(parent, list):
                return parent[to_index(key, len(parent), node)]
            if isinstance(parent, dict):
                k = map_key(key)
                if parent.get(k) is None:
                    parent[k] = {}
                return parent[k]
            raise FSError(f"cannot assign into {type_name(parent)}", node)
        raise FSError("invalid assignment target", node)

    # -- expressions -------------------------------------------------------
    def binop(self, op, a, b, node):
        if op == "+":
            return add(a, b, node)
        if op == "-":
            return add(a, b, node, sign=-1)
        if op == "*":
            return mul(a, b, node)
        if op == "/":
            return div(a, b, node)
        if op == "%":
            if dims_of(a) != dims_of(b):
                raise FSError("'%' operands must have the same units", node)
            return mk(math.fmod(mag(a), mag(b)), dims_of(a))
        if op == "^":
            return power(a, b, node)
        if op == "~":
            return concat(a, b, node)
        if op == "==":
            return equals(a, b)
        if op == "!=":
            return not equals(a, b)
        if op in ("<", ">", "<=", ">="):
            return compare(op, a, b, node)
        raise FSError(f"unknown operator {op}", node)

    def eval(self, e: Node, scope: Scope):
        k = e.kind
        if k == "num":
            return e.value
        if k == "str":
            return e.value
        if k == "const_":
            return e.value
        if k == "name":
            if scope.lookup(e.name) is None:
                raise FSError(self.unknown_name_message(e.name, scope=scope), e)
            return scope.get(e.name, e)
        if k == "varref":
            if e.name not in self.variables:
                raise FSError(f"variable #{e.name} is not defined; declare it first with "
                              f"variable(context, \"{e.name}\", <value>)", e)
            v = self.variables[e.name]
            return mk(mag(v), dims_of(v), f"#{e.name}")
        if k == "binop":
            if e.op == "&&":
                return truthy(self.eval(e.left, scope), e.left) and truthy(self.eval(e.right, scope), e.right)
            if e.op == "||":
                return truthy(self.eval(e.left, scope), e.left) or truthy(self.eval(e.right, scope), e.right)
            return self.binop(e.op, self.eval(e.left, scope), self.eval(e.right, scope), e)
        if k == "unop":
            v = self.eval(e.operand, scope)
            if e.op == "!":
                return not truthy(v, e)
            if e.op == "+":
                return v
            if has_expr(v):
                return mk(-v.v, v.dims, f"-{v.expr}" if " " not in v.expr else f"-({v.expr})")
            if isinstance(v, Vec) and v.exprs:
                return Vec(-v.a, v.dims, [None if x is None else f"-({x})" for x in v.exprs])
            return mul(v, -1.0, e)
        if k == "ternary":
            return self.eval(e.a if truthy(self.eval(e.cond, scope), e.cond) else e.b, scope)
        if k == "call":
            fn = self.eval(e.fn, scope)
            args = [self.eval(a, scope) for a in e.args]
            return self.call(fn, args, e)
        if k == "index":
            obj = self.eval(e.obj, scope)
            idx = self.eval(e.idx, scope)
            if isinstance(obj, list):
                return obj[to_index(idx, len(obj), e)]
            if isinstance(obj, Vec):
                return obj.item(to_index(idx, len(obj), e))
            if isinstance(obj, dict):
                return obj.get(map_key(idx))
            if isinstance(obj, str):
                return obj[to_index(idx, len(obj), e)]
            raise FSError(f"cannot index into {type_name(obj)}", e)
        if k == "member":
            if e.obj.kind == "name" and scope.lookup(e.obj.name) is None:
                raise FSError(self.unknown_name_message(e.obj.name, member=e.name, scope=scope), e.obj)
            obj = self.eval(e.obj, scope)
            if isinstance(obj, dict):
                return obj.get(e.name)
            if isinstance(obj, EnumType):
                try:
                    return obj.get(e.name)
                except FSError as err:
                    raise err.with_node(e)
            if isinstance(obj, (Plane, Line, Box3d)):
                try:
                    return obj.field(e.name)
                except FSError as err:
                    raise err.with_node(e)
            if obj is None:
                raise FSError(f"cannot read field '{e.name}' of undefined", e)
            raise FSError(f"{type_name(obj)} has no field '{e.name}'", e)
        if k == "array":
            return [copy_value(self.eval(i, scope)) for i in e.items]
        if k == "map":
            return {map_key(self.eval(kn, scope)): copy_value(self.eval(vn, scope)) for kn, vn in e.entries}
        if k == "function":
            return Function(e, scope, self)
        if k == "istype":
            v = self.eval(e.expr, scope)
            chk = TYPE_CHECKS.get(e.type)
            if chk is None:
                return False
            return chk(v)
        raise FSError(f"unsupported expression {k}", e)


__all__ = ["Interpreter", "Function", "Builtin", "FeatureFn", "FSError", "FSSyntaxError", "copy_value"]
