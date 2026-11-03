"""Placement quality estimates: ratsnest length, crossings, RUDY congestion, rule violations."""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from .model import Board
from .netrules import NetRules, net_kind


def _mst(points: List[Tuple[float, float]]) -> List[Tuple[int, int]]:
    n = len(points)
    if n < 2:
        return []
    inside = [False] * n
    best = [math.inf] * n
    parent = [-1] * n
    best[0] = 0
    edges = []
    for _ in range(n):
        u = -1
        bu = math.inf
        for i in range(n):
            if not inside[i] and best[i] < bu:
                u, bu = i, best[i]
        inside[u] = True
        if parent[u] >= 0:
            edges.append((parent[u], u))
        ux, uy = points[u]
        for v in range(n):
            if not inside[v]:
                d = math.hypot(points[v][0] - ux, points[v][1] - uy)
                if d < best[v]:
                    best[v], parent[v] = d, u
    return edges


def ratsnest(board: Board, positions: Optional[Dict[str, Tuple[float, float, float]]] = None,
             include_power=False):
    """Return {net: [(x1,y1,x2,y2), ...]} using a Euclidean MST per net."""
    positions = positions or {}
    out = {}
    for net, pins in board.net_members().items():
        if len(pins) < 2:
            continue
        if not include_power and net_kind(net) != "sig":
            continue
        if len(pins) > 60:
            continue
        pts = []
        for ref, pad in pins:
            c = board.components[ref]
            x, y, r = positions.get(ref, (c.x, c.y, c.rot))
            pts.append(c.pad_pos(pad, x, y, r))
        out[net] = [(pts[a][0], pts[a][1], pts[b][0], pts[b][1]) for a, b in _mst(pts)]
    return out


def _seg_cross(a, b):
    (x1, y1, x2, y2), (x3, y3, x4, y4) = a, b

    def orient(ax, ay, bx, by, cx, cy):
        v = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
        return 0 if abs(v) < 1e-9 else (1 if v > 0 else -1)
    if max(x1, x2) < min(x3, x4) or max(x3, x4) < min(x1, x2) or \
       max(y1, y2) < min(y3, y4) or max(y3, y4) < min(y1, y2):
        return False
    shared = {(round(x1, 3), round(y1, 3)), (round(x2, 3), round(y2, 3))} & \
             {(round(x3, 3), round(y3, 3)), (round(x4, 3), round(y4, 3))}
    if shared:
        return False
    d1 = orient(x3, y3, x4, y4, x1, y1)
    d2 = orient(x3, y3, x4, y4, x2, y2)
    d3 = orient(x1, y1, x2, y2, x3, y3)
    d4 = orient(x1, y1, x2, y2, x4, y4)
    return d1 * d2 < 0 and d3 * d4 < 0


def crossings(rats) -> int:
    segs = [(net, s) for net, ss in rats.items() for s in ss]
    # bucket by coarse grid to avoid O(n^2) on large boards
    cell = 5.0
    grid = {}
    for i, (_, s) in enumerate(segs):
        x0, x1 = sorted((s[0], s[2]))
        y0, y1 = sorted((s[1], s[3]))
        for gx in range(int(x0 // cell), int(x1 // cell) + 1):
            for gy in range(int(y0 // cell), int(y1 // cell) + 1):
                grid.setdefault((gx, gy), []).append(i)
    seen = set()
    count = 0
    for idx in grid.values():
        for a in range(len(idx)):
            for b in range(a + 1, len(idx)):
                i, j = idx[a], idx[b]
                key = (i, j) if i < j else (j, i)
                if key in seen:
                    continue
                seen.add(key)
                if segs[i][0] != segs[j][0] and _seg_cross(segs[i][1], segs[j][1]):
                    count += 1
    return count


def congestion(board: Board, positions=None, cell=2.0, pitch=0.5, layers=2):
    """RUDY wire-density estimate. Returns (max_util, p95_util, overflow_cells, grid_info)."""
    positions = positions or {}
    o = board.outline
    pts_all = []
    nets = []
    for net, pins in board.net_members().items():
        if len(pins) < 2 or net_kind(net) == "gnd" or len(pins) > 60:
            continue
        pts = []
        for ref, pad in pins:
            c = board.components[ref]
            x, y, r = positions.get(ref, (c.x, c.y, c.rot))
            pts.append(c.pad_pos(pad, x, y, r))
        pts_all += pts
        nets.append(pts)
    if not pts_all:
        return {"max": 0, "p95": 0, "overflow": 0}
    if o:
        x0, y0, x1, y1 = o.x0, o.y0, o.x1, o.y1
    else:
        x0 = min(p[0] for p in pts_all); x1 = max(p[0] for p in pts_all)
        y0 = min(p[1] for p in pts_all); y1 = max(p[1] for p in pts_all)
    nx = max(1, int(math.ceil((x1 - x0) / cell)))
    ny = max(1, int(math.ceil((y1 - y0) / cell)))
    if nx * ny > 250000:
        cell *= math.sqrt(nx * ny / 250000)
        nx = max(1, int(math.ceil((x1 - x0) / cell)))
        ny = max(1, int(math.ceil((y1 - y0) / cell)))
    dem = [[0.0] * nx for _ in range(ny)]
    for pts in nets:
        bx0 = min(p[0] for p in pts); bx1 = max(p[0] for p in pts)
        by0 = min(p[1] for p in pts); by1 = max(p[1] for p in pts)
        w = max(bx1 - bx0, cell * 0.5)
        h = max(by1 - by0, cell * 0.5)
        density = (w + h) / (w * h)          # wire length per unit area
        i0 = max(0, int((bx0 - x0) // cell)); i1 = min(nx - 1, int((bx1 - x0) // cell))
        j0 = max(0, int((by0 - y0) // cell)); j1 = min(ny - 1, int((by1 - y0) // cell))
        for j in range(j0, j1 + 1):
            row = dem[j]
            for i in range(i0, i1 + 1):
                row[i] += density * cell * cell
    capacity = cell / pitch * layers * cell     # wire mm that fit in a cell
    utils = sorted(v / capacity for row in dem for v in row)
    p95 = utils[int(len(utils) * 0.95)] if utils else 0
    return {"max": round(utils[-1], 3), "p95": round(p95, 3),
            "overflow": sum(1 for u in utils if u > 1.0),
            "cell": cell, "nx": nx, "ny": ny, "x0": x0, "y0": y0,
            "grid": [[round(v / capacity, 2) for v in row] for row in dem] if nx * ny <= 20000 else None}


def overlaps(board: Board, positions=None, refs=None, clearance=0.0) -> List[Tuple[str, str]]:
    positions = positions or {}
    boxes = []
    for r, c in board.components.items():
        x, y, rot = positions.get(r, (c.x, c.y, c.rot))
        boxes.append((r, c.side, c.tht, c.bbox(x, y, rot, clearance / 2)))
    boxes.sort(key=lambda b: b[3][0])
    out = []
    for i in range(len(boxes)):
        ri, si, ti, bi = boxes[i]
        for j in range(i + 1, len(boxes)):
            rj, sj, tj, bj = boxes[j]
            if bj[0] >= bi[2]:
                break
            if si != sj and not (ti or tj):
                continue
            if bj[1] < bi[3] - 0.02 and bj[3] > bi[1] + 0.02 and bj[0] < bi[2] - 0.02:
                if refs is None or ri in refs or rj in refs:
                    out.append((ri, rj))
    return out


def out_of_board(board: Board, positions=None) -> List[str]:
    if not board.outline:
        return []
    positions = positions or {}
    o = board.outline
    res = []
    for r, c in board.components.items():
        x, y, rot = positions.get(r, (c.x, c.y, c.rot))
        b = c.bbox(x, y, rot)
        if not o.contains(*b, tol=0.01):
            res.append(r)
    return res


def evaluate(board: Board, rules: NetRules, positions=None, label="") -> dict:
    positions = positions or {}
    rats = ratsnest(board, positions)
    rats_all = ratsnest(board, positions, include_power=True)
    length = sum(math.hypot(s[2] - s[0], s[3] - s[1]) for ss in rats.values() for s in ss)
    length_all = sum(math.hypot(s[2] - s[0], s[3] - s[1]) for ss in rats_all.values() for s in ss)
    cong = congestion(board, positions)
    crit = []
    for net, ss in rats.items():
        rule = rules.rule(board.netclass(net))
        if rule.max_length > 0:
            L = sum(math.hypot(s[2] - s[0], s[3] - s[1]) for s in ss)
            if L > rule.max_length:
                crit.append({"net": net, "length": round(L, 1), "target": rule.max_length})
    ov = overlaps(board, positions)
    return {
        "label": label,
        "signal_ratsnest_mm": round(length, 1),
        "all_ratsnest_mm": round(length_all, 1),
        "crossings": crossings(rats),
        "congestion_max": cong["max"], "congestion_p95": cong["p95"],
        "congestion_overflow_cells": cong["overflow"],
        "overlaps": len(ov), "overlap_pairs": ov[:50],
        "out_of_board": out_of_board(board, positions),
        "critical_over_target": sorted(crit, key=lambda c: -c["length"] / c["target"])[:30],
    }
