"""Tiny, fast S-expression reader for KiCad files (read-only)."""
from __future__ import annotations

import re
from typing import Any, List

_TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')


def parse(text: str) -> list:
    stack: List[list] = [[]]
    for m in _TOKEN.finditer(text):
        t = m.group(0)
        if t == "(":
            stack.append([])
        elif t == ")":
            node = stack.pop()
            stack[-1].append(node)
        elif t[0] == '"':
            stack[-1].append(Str(t[1:-1].replace('\\"', '"').replace("\\\\", "\\")))
        else:
            stack[-1].append(t)
    return stack[0][0] if stack[0] else []


class Str(str):
    """A quoted string (distinguishes "1" from the bare atom 1)."""


def head(node: Any) -> str:
    return node[0] if isinstance(node, list) and node else ""


def children(node: list, name: str):
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == name]


def child(node: list, name: str):
    for c in node[1:]:
        if isinstance(c, list) and c and c[0] == name:
            return c
    return None


def num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default
