"""Tokenizer for the FS-Lite subset of FeatureScript."""
from __future__ import annotations

from dataclasses import dataclass


class FSSyntaxError(Exception):
    def __init__(self, msg: str, line: int, col: int):
        super().__init__(f"line {line}:{col}: {msg}")
        self.msg, self.line, self.col = msg, line, col


KEYWORDS = {
    "var", "const", "function", "return", "if", "else", "for", "while", "in", "is",
    "export", "import", "annotation", "precondition", "returns", "true", "false",
    "undefined", "break", "continue", "throw", "try", "catch", "silent", "predicate",
    "enum", "type", "typecheck",
}

# Longest first so that e.g. "<=" wins over "<".
OPERATORS = sorted([
    "==", "!=", "<=", ">=", "&&", "||", "+=", "-=", "*=", "/=", "~=", "->", "...",
    "+", "-", "*", "/", "%", "^", "~", "!", "<", ">", "=", "?", ":", ".", ",", ";",
    "(", ")", "[", "]", "{", "}", "@",
], key=len, reverse=True)


@dataclass
class Tok:
    kind: str  # "num" | "str" | "id" | "kw" | "op" | "eof"
    value: object
    line: int
    col: int

    def __repr__(self):
        return f"{self.kind}:{self.value!r}@{self.line}:{self.col}"


def tokenize(src: str) -> list[Tok]:
    toks: list[Tok] = []
    i, line, col, n = 0, 1, 1, len(src)

    def adv(k: int):
        nonlocal i, line, col
        for _ in range(k):
            if src[i] == "\n":
                line += 1
                col = 1
            else:
                col += 1
            i += 1

    while i < n:
        c = src[i]
        if c in " \t\r\n":
            adv(1)
            continue
        if src.startswith("//", i):
            while i < n and src[i] != "\n":
                adv(1)
            continue
        if src.startswith("/*", i):
            end = src.find("*/", i + 2)
            if end < 0:
                raise FSSyntaxError("unterminated comment", line, col)
            adv(end + 2 - i)
            continue
        sl, sc = line, col
        if c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit()):
            j = i
            while j < n and (src[j].isdigit() or src[j] == "."):
                j += 1
            if j < n and src[j] in "eE" and (j + 1 < n and (src[j + 1].isdigit() or src[j + 1] in "+-")):
                j += 2
                while j < n and src[j].isdigit():
                    j += 1
            text = src[i:j]
            try:
                val = float(text)
            except ValueError:
                raise FSSyntaxError(f"bad number {text!r}", sl, sc)
            toks.append(Tok("num", val, sl, sc))
            adv(j - i)
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            word = src[i:j]
            toks.append(Tok("kw" if word in KEYWORDS else "id", word, sl, sc))
            adv(j - i)
            continue
        if c == "#" and i + 1 < n and (src[i + 1].isalpha() or src[i + 1] == "_"):
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            toks.append(Tok("var", src[i + 1:j], sl, sc))
            adv(j - i)
            continue
        if c == '"' or c == "'":
            q = c
            j = i + 1
            out = []
            while j < n and src[j] != q:
                if src[j] == "\\" and j + 1 < n:
                    out.append({"n": "\n", "t": "\t", "\\": "\\", '"': '"', "'": "'"}.get(src[j + 1], src[j + 1]))
                    j += 2
                    continue
                if src[j] == "\n":
                    raise FSSyntaxError("unterminated string", sl, sc)
                out.append(src[j])
                j += 1
            if j >= n:
                raise FSSyntaxError("unterminated string", sl, sc)
            toks.append(Tok("str", "".join(out), sl, sc))
            adv(j + 1 - i)
            continue
        for op in OPERATORS:
            if src.startswith(op, i):
                toks.append(Tok("op", op, sl, sc))
                adv(len(op))
                break
        else:
            raise FSSyntaxError(f"unexpected character {c!r}", sl, sc)
    toks.append(Tok("eof", None, line, col))
    return toks
