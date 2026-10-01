"""Recursive-descent parser for FS-Lite (a FeatureScript subset).

Produces a light AST of `Node` objects. Source spans (char offsets) are kept on
top-level declarations so other tools can slice the original text.
"""
from __future__ import annotations

from .lexer import FSSyntaxError, Tok, tokenize


class Node:
    def __init__(self, kind: str, tok: Tok | None = None, **attrs):
        self.kind = kind
        self.line = tok.line if tok else 0
        self.col = tok.col if tok else 0
        self.__dict__.update(attrs)

    def __repr__(self):
        fields = {k: v for k, v in self.__dict__.items() if k not in ("kind", "line", "col")}
        return f"{self.kind}({fields})"


ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "~="}


class Parser:
    def __init__(self, src: str):
        self.src = src
        self.toks = tokenize(src)
        self.i = 0
        self.line_starts = [0]
        for idx, ch in enumerate(src):
            if ch == "\n":
                self.line_starts.append(idx + 1)

    # -- helpers ---------------------------------------------------------
    @property
    def t(self) -> Tok:
        return self.toks[self.i]

    def peek(self, k=1) -> Tok:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def pos(self, tok: Tok) -> int:
        return self.line_starts[tok.line - 1] + tok.col - 1

    def at(self, kind, value=None) -> bool:
        t = self.t
        return t.kind == kind and (value is None or t.value == value)

    def at_op(self, *ops) -> bool:
        return self.t.kind == "op" and self.t.value in ops

    def at_kw(self, *kws) -> bool:
        return self.t.kind == "kw" and self.t.value in kws

    def next(self) -> Tok:
        t = self.t
        self.i += 1
        return t

    def err(self, msg, tok=None):
        tok = tok or self.t
        got = "end of file" if tok.kind == "eof" else repr(tok.value)
        raise FSSyntaxError(f"{msg} (got {got})", tok.line, tok.col)

    def expect_op(self, op) -> Tok:
        if not self.at_op(op):
            self.err(f"expected '{op}'")
        return self.next()

    def expect_kw(self, kw) -> Tok:
        if not self.at_kw(kw):
            self.err(f"expected '{kw}'")
        return self.next()

    def expect_id(self) -> Tok:
        if self.t.kind != "id":
            self.err("expected identifier")
        return self.next()

    # -- program ---------------------------------------------------------
    def parse_program(self) -> Node:
        decls = []
        version = None
        pending_ann = None
        while not self.at("eof"):
            start = self.t
            if self.at("id", "FeatureScript"):
                self.next()
                version = int(self.next().value)
                self.expect_op(";")
                continue
            exported = False
            if self.at_kw("export"):
                exported = True
                self.next()
            if self.at_kw("import"):
                self.next()
                self.expect_op("(")
                args = {}
                while not self.at_op(")"):
                    key = self.next().value
                    self.expect_op(":")
                    args[key] = self.parse_expr()
                    if self.at_op(","):
                        self.next()
                self.expect_op(")")
                self.expect_op(";")
                decls.append(Node("import", start, args=args))
                continue
            if self.at_kw("annotation"):
                self.next()
                pending_ann = self.parse_primary()
                continue
            if self.at_kw("const"):
                self.next()
                name = self.expect_id().value
                self.skip_type()
                self.expect_op("=")
                value = self.parse_expr()
                end = self.expect_op(";")
                decls.append(Node("const", start, name=name, value=value, exported=exported,
                                  annotation=pending_ann, span=(self.pos(start), self.pos(end) + 1)))
                pending_ann = None
                continue
            if self.at_kw("function", "predicate"):
                is_pred = self.next().value == "predicate"
                name = self.expect_id().value
                fn = self.parse_function_rest(name, start)
                fn.is_predicate = is_pred
                end = self.toks[self.i - 1]
                decls.append(Node("fundecl", start, name=name, fn=fn, exported=exported,
                                  span=(self.pos(start), self.pos(end) + 1)))
                pending_ann = None
                continue
            if self.at_kw("enum", "type"):
                self.err("enum/type declarations are not supported in FS-Lite")
            self.err("expected top-level declaration (const, function, import, annotation)")
        return Node("program", None, decls=decls, version=version)

    def skip_type(self):
        if self.at_kw("is"):
            self.next()
            self.expect_id()

    def parse_params(self):
        self.expect_op("(")
        params = []
        while not self.at_op(")"):
            name = self.expect_id().value
            self.skip_type()
            default = None
            if self.at_op("="):
                self.next()
                default = self.parse_expr()
            params.append((name, default))
            if self.at_op(","):
                self.next()
            elif not self.at_op(")"):
                self.err("expected ',' or ')' in parameter list")
        self.expect_op(")")
        return params

    def parse_function_rest(self, name, tok) -> Node:
        params = self.parse_params()
        if self.at_kw("returns"):
            self.next()
            self.expect_id()
        precond = None
        if self.at_kw("precondition"):
            self.next()
            precond = self.parse_block()
        body = self.parse_block()
        return Node("function", tok, name=name, params=params, body=body, precondition=precond)

    # -- statements ------------------------------------------------------
    def parse_block(self) -> Node:
        tok = self.expect_op("{")
        stmts = []
        while not self.at_op("}"):
            if self.at("eof"):
                self.err("unterminated block, expected '}'")
            stmts.append(self.parse_stmt())
        close = self.expect_op("}")
        return Node("block", tok, stmts=stmts, start=self.pos(tok), end=self.pos(close))

    def parse_vardecl(self, require_semi=True) -> Node:
        tok = self.next()  # var | const
        decls = []
        while True:
            name = self.expect_id().value
            self.skip_type()
            init = None
            if self.at_op("="):
                self.next()
                init = self.parse_expr()
            decls.append((name, init))
            if self.at_op(","):
                self.next()
                continue
            break
        if require_semi:
            self.expect_op(";")
        return Node("vardecl", tok, decls=decls, const=tok.value == "const")

    def parse_stmt(self) -> Node:
        t = self.t
        if self.at_op("{"):
            return self.parse_block()
        if self.at_op(";"):
            self.next()
            return Node("empty", t)
        if self.at_kw("annotation"):
            self.next()
            ann = self.parse_primary()
            inner = self.parse_stmt()
            inner.annotation = ann
            return inner
        if self.at_kw("var", "const"):
            return self.parse_vardecl()
        if self.at_kw("if"):
            self.next()
            self.expect_op("(")
            cond = self.parse_expr()
            self.expect_op(")")
            then = self.parse_stmt()
            other = None
            if self.at_kw("else"):
                self.next()
                other = self.parse_stmt()
            return Node("if", t, cond=cond, then=then, other=other)
        if self.at_kw("while"):
            self.next()
            self.expect_op("(")
            cond = self.parse_expr()
            self.expect_op(")")
            return Node("while", t, cond=cond, body=self.parse_stmt())
        if self.at_kw("for"):
            return self.parse_for()
        if self.at_kw("return"):
            self.next()
            value = None if self.at_op(";") else self.parse_expr()
            self.expect_op(";")
            return Node("return", t, value=value)
        if self.at_kw("break", "continue"):
            self.next()
            self.expect_op(";")
            return Node(t.value, t)
        if self.at_kw("throw"):
            self.next()
            value = self.parse_expr()
            self.expect_op(";")
            return Node("throw", t, value=value)
        if self.at_kw("try"):
            self.next()
            if self.at_kw("silent"):
                self.next()
            body = self.parse_block()
            var, handler = None, None
            if self.at_kw("catch"):
                self.next()
                self.expect_op("(")
                var = self.expect_id().value
                self.expect_op(")")
                handler = self.parse_block()
            return Node("try", t, body=body, var=var, handler=handler)
        stmt = self.parse_simple()
        self.expect_op(";")
        return stmt

    def parse_simple(self) -> Node:
        """Expression statement or assignment (no trailing ';')."""
        t = self.t
        expr = self.parse_expr()
        if self.at_op(*ASSIGN_OPS):
            op = self.next().value
            if expr.kind not in ("name", "index", "member"):
                self.err("invalid assignment target", t)
            value = self.parse_expr()
            return Node("assign", t, target=expr, op=op, value=value)
        return Node("exprstmt", t, expr=expr)

    def parse_for(self) -> Node:
        t = self.next()
        self.expect_op("(")
        # for (var x in arr) / for (var k, v in map)
        if self.at_kw("var", "const"):
            save = self.i
            self.next()
            if self.t.kind == "id":
                n1 = self.next().value
                n2 = None
                if self.at_op(",") and self.peek().kind == "id" and self.peek(2).kind == "kw" and self.peek(2).value == "in":
                    self.next()
                    n2 = self.next().value
                if self.at_kw("in"):
                    self.next()
                    it = self.parse_expr()
                    self.expect_op(")")
                    return Node("forin", t, names=(n1, n2), iterable=it, body=self.parse_stmt())
            self.i = save
        init = None
        if not self.at_op(";"):
            init = self.parse_vardecl(require_semi=False) if self.at_kw("var") else self.parse_simple()
        self.expect_op(";")
        cond = None if self.at_op(";") else self.parse_expr()
        self.expect_op(";")
        update = None if self.at_op(")") else self.parse_simple()
        self.expect_op(")")
        return Node("for", t, init=init, cond=cond, update=update, body=self.parse_stmt())

    # -- expressions -----------------------------------------------------
    def parse_expr(self) -> Node:
        return self.parse_ternary()

    def parse_ternary(self) -> Node:
        cond = self.parse_binary(0)
        if self.at_op("?"):
            t = self.next()
            a = self.parse_expr()
            self.expect_op(":")
            b = self.parse_expr()
            return Node("ternary", t, cond=cond, a=a, b=b)
        return cond

    LEVELS = [("||",), ("&&",), ("==", "!="), ("<", ">", "<=", ">="), ("+", "-", "~"), ("*", "/", "%")]

    def parse_binary(self, level: int) -> Node:
        if level == len(self.LEVELS):
            return self.parse_unary()
        left = self.parse_binary(level + 1)
        while True:
            if self.at_op(*self.LEVELS[level]):
                t = self.next()
                right = self.parse_binary(level + 1)
                left = Node("binop", t, op=t.value, left=left, right=right)
            elif level == 3 and self.at_kw("is"):
                t = self.next()
                tname = self.expect_id().value
                left = Node("istype", t, expr=left, type=tname)
            else:
                return left

    def parse_unary(self) -> Node:
        if self.at_op("-", "!", "+"):
            t = self.next()
            operand = self.parse_unary()
            return Node("unop", t, op=t.value, operand=operand)
        return self.parse_power()

    def parse_power(self) -> Node:
        base = self.parse_postfix()
        if self.at_op("^"):
            t = self.next()
            exp = self.parse_unary()
            return Node("binop", t, op="^", left=base, right=exp)
        return base

    def parse_postfix(self) -> Node:
        expr = self.parse_primary()
        while True:
            if self.t.kind == "id" and self.t.value == "as" and self.peek().kind == "id":
                self.next()
                self.next()
                continue
            if self.at_op("("):
                t = self.next()
                args = []
                while not self.at_op(")"):
                    args.append(self.parse_expr())
                    if self.at_op(","):
                        self.next()
                    elif not self.at_op(")"):
                        self.err("expected ',' or ')' in argument list")
                self.expect_op(")")
                expr = Node("call", t, fn=expr, args=args)
            elif self.at_op("["):
                t = self.next()
                idx = self.parse_expr()
                self.expect_op("]")
                expr = Node("index", t, obj=expr, idx=idx)
            elif self.at_op("."):
                t = self.next()
                if self.t.kind not in ("id", "kw"):
                    self.err("expected field name after '.'")
                expr = Node("member", t, obj=expr, name=self.next().value)
            else:
                return expr

    def parse_primary(self) -> Node:
        t = self.t
        if t.kind == "num":
            self.next()
            return Node("num", t, value=t.value)
        if t.kind == "str":
            self.next()
            return Node("str", t, value=t.value)
        if t.kind == "id":
            self.next()
            return Node("name", t, name=t.value)
        if t.kind == "var":
            self.next()
            return Node("varref", t, name=t.value)
        if self.at_kw("true", "false"):
            self.next()
            return Node("const_", t, value=t.value == "true")
        if self.at_kw("undefined"):
            self.next()
            return Node("const_", t, value=None)
        if self.at_kw("function"):
            self.next()
            return self.parse_function_rest("<lambda>", t)
        if self.at_op("("):
            self.next()
            e = self.parse_expr()
            self.expect_op(")")
            return e
        if self.at_op("["):
            self.next()
            items = []
            while not self.at_op("]"):
                items.append(self.parse_expr())
                if self.at_op(","):
                    self.next()
                elif not self.at_op("]"):
                    self.err("expected ',' or ']' in array literal")
            self.expect_op("]")
            return Node("array", t, items=items)
        if self.at_op("{"):
            self.next()
            entries = []
            while not self.at_op("}"):
                if self.t.kind in ("id", "kw") and self.peek().kind == "op" and self.peek().value == ":":
                    key = Node("str", self.t, value=self.next().value)
                else:
                    key = self.parse_expr()
                self.expect_op(":")
                entries.append((key, self.parse_expr()))
                if self.at_op(","):
                    self.next()
                elif not self.at_op("}"):
                    self.err("expected ',' or '}' in map literal")
            self.expect_op("}")
            return Node("map", t, entries=entries)
        self.err("expected expression")


def parse(src: str) -> Node:
    return Parser(src).parse_program()
