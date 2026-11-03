"""Reusable block arrangements and repeated-channel placement.

Members are matched between blocks by role, not reference designator: the
value, footprint and which anchor pins each part connects to.
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Tuple

from .model import Board, rotate, unrotate, norm_angle
from .classify import Block, role
from .netrules import net_kind


def _anchor_pins(board: Board, anchor: str, ref: str) -> frozenset:
    a = board.components.get(anchor)
    c = board.components.get(ref)
    if not a or not c:
        return frozenset()
    mine = {p.net for p in c.pads if p.net and net_kind(p.net) == "sig"}
    return frozenset(p.number for p in a.pads if p.net in mine)


def member_sig(board: Board, anchor: str, ref: str):
    c = board.components[ref]
    val = "" if role(c) == "connector" else c.value.upper()
    return (val, c.footprint.split(":")[-1], _anchor_pins(board, anchor, ref))


def match_members(board: Board, src: Block, dst: Block) -> Tuple[Dict[str, str], List[str]]:
    """Map source member -> destination member. Returns (mapping, unmatched_dst)."""
    mapping = {src.anchor: dst.anchor} if src.anchor and dst.anchor else {}
    s_left = [m for m in src.members if m not in mapping]
    d_left = [m for m in dst.members if m not in mapping.values()]
    levels = [
        lambda b, a, r: member_sig(b, a, r),
        lambda b, a, r: member_sig(b, a, r)[:2],
        lambda b, a, r: (member_sig(b, a, r)[1],),
    ]
    for lvl in levels:
        sig_d: Dict[tuple, List[str]] = {}
        for d in d_left:
            sig_d.setdefault(lvl(board, dst.anchor, d), []).append(d)
        for s in list(s_left):
            k = lvl(board, src.anchor, s)
            if sig_d.get(k):
                d = sig_d[k].pop(0)
                mapping[s] = d
                s_left.remove(s)
                d_left.remove(d)
    return mapping, d_left


def relative_layout(board: Board, block: Block, positions) -> Dict[str, Tuple[float, float, float]]:
    ax, ay, ar = positions.get(block.anchor) or (board.components[block.anchor].x,
                                                  board.components[block.anchor].y,
                                                  board.components[block.anchor].rot)
    out = {}
    for m in block.members:
        c = board.components[m]
        x, y, r = positions.get(m) or (c.x, c.y, c.rot)
        dx, dy = unrotate(x - ax, y - ay, ar)
        out[m] = (dx, dy, norm_angle(r - ar))
    return out


def replicate(board: Board, src: Block, dst: Block, positions) -> Tuple[Dict[str, tuple], List[str]]:
    """Copy src's arrangement onto dst around dst's anchor (which stays put)."""
    mapping, unmatched = match_members(board, src, dst)
    rel = relative_layout(board, src, positions)
    a = board.components[dst.anchor]
    ax, ay, ar = positions.get(dst.anchor) or (a.x, a.y, a.rot)
    # keep the anchor's orientation equal to the source anchor's relative frame
    moves = {}
    for s, d in mapping.items():
        dx, dy, dr = rel[s]
        rx, ry = rotate(dx, dy, ar)
        moves[d] = (round(ax + rx, 4), round(ay + ry, 4), norm_angle(ar + dr))
    return moves, unmatched


# ------------------------------------------------------------------ templates
def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "template"


def save_template(board: Board, block: Block, positions, folder: str, name: str) -> str:
    os.makedirs(folder, exist_ok=True)
    rel = relative_layout(board, block, positions)
    a = board.components[block.anchor]
    data = {
        "name": name,
        "anchor": {"value": a.value, "footprint": a.footprint},
        "members": [
            {"value": board.components[m].value, "footprint": board.components[m].footprint,
             "anchor_pins": sorted(_anchor_pins(board, block.anchor, m)),
             "dx": rel[m][0], "dy": rel[m][1], "drot": rel[m][2], "is_anchor": m == block.anchor}
            for m in block.members
        ],
    }
    path = os.path.join(folder, _safe(name) + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    return path


def list_templates(folder: str) -> List[dict]:
    out = []
    if not os.path.isdir(folder):
        return out
    for fn in sorted(os.listdir(folder)):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(folder, fn), encoding="utf-8") as f:
                    d = json.load(f)
                out.append({"file": fn, "name": d.get("name", fn), "anchor": d.get("anchor", {}),
                            "parts": len(d.get("members", []))})
            except Exception:
                pass
    return out


def apply_template(board: Board, block: Block, folder: str, fn: str, positions):
    with open(os.path.join(folder, os.path.basename(fn)), encoding="utf-8") as f:
        t = json.load(f)
    a = board.components[block.anchor]
    ax, ay, ar = positions.get(block.anchor) or (a.x, a.y, a.rot)
    warn = []
    if t["anchor"]["value"].upper() != a.value.upper():
        warn.append(f"Anchor value differs: template {t['anchor']['value']} vs {a.value}")
    left = [m for m in block.members if m != block.anchor]
    moves = {}
    for tm in t["members"]:
        if tm.get("is_anchor"):
            continue
        cand = None
        for lvl in range(3):
            for m in left:
                c = board.components[m]
                ok = c.footprint.split(":")[-1] == tm["footprint"].split(":")[-1]
                if lvl <= 1:
                    ok = ok and c.value.upper() == tm["value"].upper()
                if lvl == 0:
                    ok = ok and sorted(_anchor_pins(board, block.anchor, m)) == tm["anchor_pins"]
                if ok:
                    cand = m
                    break
            if cand:
                break
        if cand:
            left.remove(cand)
            rx, ry = rotate(tm["dx"], tm["dy"], ar)
            moves[cand] = (round(ax + rx, 4), round(ay + ry, 4), norm_angle(ar + tm["drot"]))
    if left:
        warn.append("Not in template: " + ", ".join(left))
    return moves, warn
