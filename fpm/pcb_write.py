"""Write proposed footprint positions into a copy of a .kicad_pcb text.

Only footprint placement changes: the footprint's own (at x y rot), plus the
absolute angle KiCad stores on each pad, field and text when the footprint
rotates. Everything else is copied byte for byte. Used to hand KiCad's own DRC
(kicad-cli) a board that shows the proposal, without touching the real file.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

_NUM = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


class _Node:
    __slots__ = ("start", "end", "head", "children")

    def __init__(self, start, head):
        self.start = start
        self.end = -1
        self.head = head
        self.children: List["_Node"] = []


def _tree(text: str) -> _Node:
    root = _Node(0, "")
    stack = [root]
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
            continue
        if ch == "(":
            j = i + 1
            while j < n and text[j] not in " \t\r\n()\"":
                j += 1
            node = _Node(i, text[i + 1:j])
            stack[-1].children.append(node)
            stack.append(node)
            i = j
            continue
        if ch == ")":
            node = stack.pop()
            node.end = i + 1
        i += 1
    return root


def _fmt(v: float) -> str:
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _norm(a: float) -> float:
    a = a % 360.0
    if a > 180:
        a -= 360
    return round(a, 6)


def _ref_of(text: str, fp: _Node) -> Optional[str]:
    for c in fp.children:
        if c.head == "property":
            m = re.match(r'\(property\s+"Reference"\s+"((?:[^"\\]|\\.)*)"', text[c.start:c.end])
            if m:
                return m.group(1)
        elif c.head == "fp_text":
            m = re.match(r'\(fp_text\s+reference\s+"?((?:[^"\\\s)]|\\.)*)"?', text[c.start:c.end])
            if m:
                return m.group(1)
    return None


def apply_positions(text: str, moves: Dict[str, Tuple[float, float, float]]) -> Tuple[str, List[str]]:
    """Return (new text, refs that were moved). Duplicate refs get #2, #3 like the loader."""
    root = _tree(text)
    top = root.children[0] if root.children else root
    edits: List[Tuple[int, int, str]] = []
    seen: Dict[str, int] = {}
    done: List[str] = []
    for fp in top.children:
        if fp.head not in ("footprint", "module"):
            continue
        ref = _ref_of(text, fp) or "?"
        k = seen.get(ref, 0) + 1
        seen[ref] = k
        key = ref if k == 1 else f"{ref}#{k}"
        if key not in moves:
            continue
        at = next((c for c in fp.children if c.head == "at"), None)
        if at is None:
            continue
        nums = _NUM.findall(text[at.start + 3:at.end - 1])
        old_rot = float(nums[2]) if len(nums) > 2 else 0.0
        x, y, rot = moves[key]
        rot = _norm(rot)
        delta = _norm(rot - old_rot)
        tail = f" {_fmt(rot)}" if abs(rot) > 1e-9 else ""
        edits.append((at.start, at.end, f"(at {_fmt(x)} {_fmt(y)}{tail})"))
        if abs(delta) > 1e-9:
            for c in fp.children:
                if c.head not in ("pad", "property", "fp_text", "fp_text_box"):
                    continue
                cat = next((g for g in c.children if g.head == "at"), None)
                if cat is None:
                    continue
                body = text[cat.start + 3:cat.end - 1]
                cn = _NUM.findall(body)
                if len(cn) < 2:
                    continue
                ang = _norm((float(cn[2]) if len(cn) > 2 else 0.0) + delta)
                rest = re.sub(r"^\s*" + r"\s+".join(re.escape(t) for t in cn[:3]), "", body)
                edits.append((cat.start, cat.end, f"(at {cn[0]} {cn[1]} {_fmt(ang)}{rest})"))
        done.append(key)
    out = []
    pos = 0
    for a, b, rep in sorted(edits):
        out.append(text[pos:a])
        out.append(rep)
        pos = b
    out.append(text[pos:])
    return "".join(out), done
