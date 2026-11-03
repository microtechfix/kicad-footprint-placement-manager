"""Two-stage placement engine.

Stage A  positions circuit blocks (as soft rectangles) using inter-block
         connectivity, fixed anchors, board outline, keep-outs and regions.
Stage B  builds each block internally, part by part, choosing position and
         rotation from pad-to-pad connections (not footprint centres).
Stage C  refines everything with a repeatable simulated-annealing pass
         (shifts, rotations, swaps) on a weighted cost, then legalizes overlaps.

Hard constraints (board edge, keep-outs, locked/fixed parts, block regions) are
large penalties that the legalizer drives to zero; everything else is a
user-adjustable preference.
"""
from __future__ import annotations

import math
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Set, Tuple

from .model import Board, Region, norm_angle, rotate, unrotate
from .netrules import NetRules, net_kind, diff_partner, critical_factor
from .classify import Block, Assignment, role, field_value, PRIO_FIELD_KEYS, NEAR_FIELD_KEYS

BIG = 400.0          # penalty per mm (or mm^2) for hard-constraint violations


class _OSet(dict):
    """A set of references that iterates in a fixed order. Python's string hashing changes
    between runs, so iterating a plain set made the same click give a different layout."""

    def __init__(self, items=()):
        super().__init__((k, None) for k in sorted(items))

    def add(self, k):
        self[k] = None

    def discard(self, k):
        self.pop(k, None)

    def __or__(self, other):
        return _OSet(list(self) + [k for k in other if k not in self])

    def __sub__(self, other):
        return _OSet(k for k in self if k not in other)

    def __and__(self, other):
        return _OSet(k for k in self if k in other)


@dataclass
class PlaceOptions:
    scope: str = "all"                 # all | blocks | selection | new
    blocks: List[str] = field(default_factory=list)
    refs: List[str] = field(default_factory=list)
    keep_connectors_fixed: bool = True # connectors already on the board are anchors
    allow_rotation: bool = True
    clearance: float = 0.25            # courtyard-to-courtyard minimum (mm)
    routing_space: float = 1.0         # multiplier on per-net-class routing room
    displacement_weight: float = 0.0   # pull towards the existing position
    cohesion_weight: float = 0.15      # pull of a part towards its block
    separation: float = 3.0            # min gap (mm) between noisy and sensitive blocks
    effort: float = 1.0                # iteration multiplier
    time_limit: float = 25.0           # seconds
    seed: int = 1
    master_slave: bool = True          # arrange each block's parts around its master (main IC)
    escape_gap: float = 0.6            # routing channel kept between a master and its slaves (mm)
    slave_pull: float = 1.0            # how strongly a slave keeps its spot facing its master pin
    extra_margin: Dict[str, float] = field(default_factory=dict)   # per-part extra clearance (DRC fix rounds)
    anchor_pull: float = 15.0          # pull of a block towards its own fixed parts (e.g. its antenna connector)
    edge_margin: float = 0.3           # keep movable courtyards this far inside the board edge (mm)
    repair: bool = False               # only nudge the movable parts locally (check-and-fix rounds)
    # AI / learned placement hints (see fpm/ai.py):
    #   near:  [{ref, target, pin?, offset?[dx,dy] in target frame, weight?}]
    #   edge:  [{ref, side: any|left|right|top|bottom, weight?}]
    #   zones: {block id: [fx, fy]}  (0..1 across the board, 0,0 = top left)
    #   apart: [{a: block id, b: block id, gap: mm}]
    hints: Dict = field(default_factory=dict)

    @classmethod
    def from_json(cls, d):
        o = cls()
        for k, v in (d or {}).items():
            if hasattr(o, k):
                cur = getattr(o, k)
                setattr(o, k, v if isinstance(cur, (list, dict)) else type(cur)(v))
        return o

    def to_json(self):
        return asdict(self)


class _Hash:
    def __init__(self, cell=2.0):
        self.cell = cell
        self.cells: Dict[tuple, Set[str]] = defaultdict(set)
        self.where: Dict[str, List[tuple]] = {}

    def _keys(self, layer, b):
        c = self.cell
        return [(layer, i, j) for i in range(int(math.floor(b[0] / c)), int(math.floor(b[2] / c)) + 1)
                for j in range(int(math.floor(b[1] / c)), int(math.floor(b[3] / c)) + 1)]

    def insert(self, ref, layers, b):
        ks = [k for L in layers for k in self._keys(L, b)]
        self.where[ref] = ks
        for k in ks:
            self.cells[k].add(ref)

    def remove(self, ref):
        for k in self.where.pop(ref, ()):
            self.cells[k].discard(ref)

    def query(self, layers, b):
        out = set()
        for L in layers:
            for k in self._keys(L, b):
                s = self.cells.get(k)
                if s:
                    out |= s
        return out


def _ov(a, b) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    if w <= 0:
        return 0.0
    h = min(a[3], b[3]) - max(a[1], b[1])
    if h <= 0:
        return 0.0
    return w * h


def _outside(b, r: Region) -> float:
    return (max(0.0, r.x0 - b[0]) + max(0.0, b[2] - r.x1) +
            max(0.0, r.y0 - b[1]) + max(0.0, b[3] - r.y1))


def _seg_box_gap(sg, b) -> float:
    """Distance between a segment and an axis-aligned box (0 if they touch)."""
    x0, y0, x1, y1 = sg
    if max(x0, x1) >= b[0] and min(x0, x1) <= b[2] and max(y0, y1) >= b[1] and min(y0, y1) <= b[3]:
        # segment bbox overlaps the box: check for a real intersection by clipping
        t0, t1 = 0.0, 1.0
        dx, dy = x1 - x0, y1 - y0
        ok = True
        for p_, q_ in ((-dx, x0 - b[0]), (dx, b[2] - x0), (-dy, y0 - b[1]), (dy, b[3] - y0)):
            if abs(p_) < 1e-12:
                if q_ < 0:
                    ok = False
                    break
            else:
                t = q_ / p_
                if p_ < 0:
                    t0 = max(t0, t)
                else:
                    t1 = min(t1, t)
        if ok and t0 <= t1:
            return 0.0
    best = 1e9
    for px, py in ((b[0], b[1]), (b[2], b[1]), (b[0], b[3]), (b[2], b[3])):
        vx, vy = x1 - x0, y1 - y0
        L = vx * vx + vy * vy
        t = 0.0 if L < 1e-12 else max(0.0, min(1.0, ((px - x0) * vx + (py - y0) * vy) / L))
        best = min(best, math.hypot(px - (x0 + vx * t), py - (y0 + vy * t)))
    for px, py in ((x0, y0), (x1, y1)):
        dx = max(b[0] - px, 0, px - b[2])
        dy = max(b[1] - py, 0, py - b[3])
        best = min(best, math.hypot(dx, dy))
    return best


def _inside_outline(segs, x, y) -> bool:
    """Ray casting against the outline segments (order doesn't matter)."""
    inside = False
    for x0, y0, x1, y1 in segs:
        if (y0 > y) != (y1 > y):
            xc = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
            if xc > x:
                inside = not inside
    return inside


class Placer:
    def __init__(self, board: Board, blocks: Dict[str, Block], assign: Dict[str, Assignment],
                 rules: NetRules, opts: PlaceOptions, locked: Optional[Set[str]] = None,
                 log=None):
        self.b = board
        self.blocks = blocks
        self.assign = assign
        self.rules = rules
        self.o = opts
        self.locked = set(locked or ())
        self.log = log or (lambda *_: None)
        self.rng = random.Random(opts.seed)
        self.C = board.components
        self.pos: Dict[str, List[float]] = {r: [c.x, c.y, c.rot] for r, c in self.C.items()}
        self.orig = {r: tuple(v) for r, v in self.pos.items()}
        self.block_of = {r: a.block for r, a in assign.items()}
        self._prepare()

    # ------------------------------------------------------------------ setup
    def _on_board(self, r) -> bool:
        o = self.b.outline
        if not o:
            return True
        c = self.C[r]
        bx = c.bbox()
        return o.intersects(*bx)

    def _prepare(self):
        o, C = self.o, self.C
        frozen = {m for bl in self.blocks.values() if bl.frozen for m in bl.members}
        scope = set(C)
        if o.scope == "blocks":
            scope = {m for bid in o.blocks if bid in self.blocks for m in self.blocks[bid].members}
        elif o.scope == "selection":
            scope = set(o.refs) & set(C)
        elif o.scope == "new":
            scope = {r for r in C if not self._on_board(r)}

        self.movable = _OSet()
        for r in sorted(scope):
            c = C[r]
            if c.locked or r in self.locked or r in frozen:
                continue
            if role(c) == "mechanical" and self._on_board(r):
                continue
            if o.keep_connectors_fixed and role(c) == "connector" and self._on_board(r) and o.scope != "selection":
                continue
            self.movable.add(r)
        self.fixed = _OSet(r for r in C if r not in self.movable and self._on_board(r))
        # parts neither movable nor on the board (e.g. out-of-scope parts parked off-board) are ignored
        self.active = self.movable | self.fixed

        # ---- outline / working area
        self.edge_segs = []
        if self.b.outline:
            ol, em = self.b.outline, max(0.0, o.edge_margin)
            self.area = Region(ol.x0 + em, ol.y0 + em, ol.x1 - em, ol.y1 - em, name=ol.name)
            # non-rectangular outline: notches, cut-outs and corners become obstacles
            on_box = lambda sg: ((abs(sg[0] - sg[2]) < 1e-6 and min(abs(sg[0] - ol.x0), abs(sg[0] - ol.x1)) < 1e-6) or
                                 (abs(sg[1] - sg[3]) < 1e-6 and min(abs(sg[1] - ol.y0), abs(sg[1] - ol.y1)) < 1e-6))
            if any(not on_box(sg) for sg in self.b.outline_segments):
                self.edge_segs = list(self.b.outline_segments)
        else:
            tot = sum(C[r].area for r in self.active) * 3.0 + 100
            side = math.sqrt(tot)
            xs = [C[r].x for r in self.active] or [0]
            ys = [C[r].y for r in self.active] or [0]
            cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
            self.area = Region(cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2, name="virtual")

        # ---- rotations
        self.rots: Dict[str, List[float]] = {}
        for r in self.movable:
            cur = norm_angle(self.pos[r][2])
            if o.allow_rotation and abs(cur % 90) < 1e-6 and field_value(C[r], ("placementrotate",)).lower() not in ("no", "0", "false"):
                self.rots[r] = [0.0, 90.0, 180.0, -90.0]
            else:
                self.rots[r] = [cur]

        # ---- nets
        members = self.b.net_members()
        self._members = members
        all_nets = list(members)
        prio = {}
        for r, c in C.items():
            pv = field_value(c, PRIO_FIELD_KEYS).lower()
            if pv:
                prio[r] = {"high": 2.0, "critical": 3.0, "low": 0.5, "normal": 1.0}.get(pv, None)
                if prio[r] is None:
                    try:
                        prio[r] = max(0.1, float(pv))
                    except ValueError:
                        prio[r] = 1.0
        self.nets: Dict[str, dict] = {}
        self.nets_of: Dict[str, List[str]] = defaultdict(list)
        self.spacing: Dict[str, float] = defaultdict(float)
        for net, pins in members.items():
            pins = [(r, p) for r, p in pins if r in self.active]
            if len(pins) < 2:
                continue
            kind = net_kind(net)
            rule = self.rules.rule(self.b.netclass(net))
            n = len(pins)
            if kind == "gnd":
                w = self.rules.gnd_weight
            elif kind == "pwr" or n > self.rules.max_fanout:
                w = self.rules.pwr_weight * rule.weight
            else:
                w = rule.weight / ((n - 1) ** 0.5 if self.rules.fanout_damping and n > 2 else 1)
            pf = max((prio.get(r, 1.0) for r, _ in pins), default=1.0)
            w *= pf
            if kind == "sig":
                w *= critical_factor(net)          # RF / antenna, crystal, switch-node nets by name
            if kind != "sig":
                # big rails are handled by the decoupling term; skip in the hot loop
                if n > 12 or w < 0.03:
                    continue
            if not any(r in self.movable for r, _ in pins):
                continue
            self.nets[net] = {"pins": pins, "w": w, "max": rule.max_length, "cost": 0.0}
            for r, _ in pins:
                if net not in self.nets_of[r]:
                    self.nets_of[r].append(net)
                if kind == "sig":
                    self.spacing[r] = max(self.spacing[r], rule.spacing)
        # differential pairs
        self.pairs = []
        seen = set()
        for net in self.nets:
            if net in seen:
                continue
            p = diff_partner(net, all_nets)
            if p and p in self.nets:
                seen |= {net, p}
                pw = self.rules.rule(self.b.netclass(net)).pair_weight
                self.pairs.append((net, p, pw))
        self.pairs_of = defaultdict(list)
        for i, (a, b_, _) in enumerate(self.pairs):
            for net in (a, b_):
                for r, _p in self.nets[net]["pins"]:
                    self.pairs_of[r].append(i)

        # ---- margins: clearance + routing room (denser parts need more)
        self.margin: Dict[str, float] = {}
        for r in self.active:
            c = C[r]
            dens = min(1.2, 0.04 * c.pin_count) if role(c) == "ic" else 0.0
            self.margin[r] = o.clearance + o.routing_space * (self.spacing[r] + dens) * 0.5
            self.margin[r] += 2 * o.extra_margin.get(r, 0.0)
        # Dense boards: shrink routing room so the parts can still fit (never below 0.1 mm).
        self.density_note = ""
        A = self.area
        avail = max(1.0, (A.x1 - A.x0) * (A.y1 - A.y0) - sum((k.x1 - k.x0) * (k.y1 - k.y0) for k in self.b.keepouts))
        for k in [1.0, 0.8, 0.6, 0.4, 0.2, 0.0]:
            worst = 0.0
            for side in ("F", "B"):
                dem = 0.0
                for r in self.active:
                    c = C[r]
                    if c.side == side or c.tht:
                        m = 0.1 + (self.margin[r] - 0.1) * k
                        dem += (c.xmax - c.xmin + m) * (c.ymax - c.ymin + m)
                worst = max(worst, dem / avail)
            if worst <= 0.62 or k == 0.0:
                if k < 1.0:
                    for r in self.active:
                        self.margin[r] = 0.1 + (self.margin[r] - 0.1) * k
                    self.density_note = (f"Board is dense ({worst:.0%} of the area per side is footprints); "
                                         f"routing room reduced to {k:.0%}.")
                self.fill = worst
                break
        self.packed = getattr(self, "fill", 0.0) > 0.75     # almost no free space: blocks must flex

        # ---- decoupling terms: cap supply pad -> nearest IC pin on same rail
        self.decaps = []
        from .classify import _is_decap
        for r, a in self.assign.items():
            if r not in self.active or not _is_decap(C[r]):
                continue
            bl = self.blocks.get(a.block)
            if not bl:
                continue
            rails = {n for n in C[r].nets if net_kind(n) == "pwr"}
            # the IC in the same block that is actually on this cap's rail (anchor preferred)
            ics = [m for m in bl.members if m in C and m != r and role(C[m]) == "ic"
                   and rails & set(C[m].nets)]
            if not ics:
                continue
            ic = bl.anchor if bl.anchor in ics else max(ics, key=lambda m: C[m].pin_count)
            if ic not in self.active:
                continue
            if r not in self.movable and ic not in self.movable:
                continue
            c = C[r]
            for pad in c.pads:
                if not pad.net:
                    continue
                kind = net_kind(pad.net)
                targets = [p for p in C[ic].pads if p.net == pad.net]
                if targets:
                    self.decaps.append((r, pad, ic, targets, self.rules.decap_weight * (1.0 if kind == "pwr" else 0.4)))
        self.decaps_of = defaultdict(list)
        for i, d in enumerate(self.decaps):
            self.decaps_of[d[0]].append(i)
            self.decaps_of[d[2]].append(i)

        # ---- PlaceNear
        self.near = []
        for r in self.movable:
            pn = field_value(C[r], NEAR_FIELD_KEYS)
            if pn:
                tgt, _, pin = pn.partition(":")
                tgt = tgt.strip()
                if tgt in C:
                    tp = [p for p in C[tgt].pads if p.number == pin.strip()] if pin else []
                    self.near.append((r, tgt, tp[0] if tp else None, 3.0, None))
        H = o.hints or {}
        for h in H.get("near", []) or []:
            r, tgt = h.get("ref"), h.get("target")
            if r not in self.active or tgt not in self.active or r == tgt:
                continue
            if r not in self.movable and tgt not in self.movable:
                continue
            pin = str(h.get("pin") or "")
            tp = next((p for p in C[tgt].pads if p.number == pin), None) if pin else None
            off = h.get("offset")
            off = (float(off[0]), float(off[1])) if off and len(off) == 2 else None
            self.near.append((r, tgt, tp, float(h.get("weight", 3.0)), off))
        self.edge_hint: Dict[str, Tuple[str, float]] = {}
        for h in H.get("edge", []) or []:
            r = h.get("ref")
            if r in self.movable:
                side = str(h.get("side") or "any").lower()
                self.edge_hint[r] = (side if side in ("left", "right", "top", "bottom") else "any",
                                     float(h.get("weight", 12.0)))
        self.zone_hint: Dict[str, Tuple[float, float]] = {}
        for bid, f in (H.get("zones") or {}).items():
            try:
                self.zone_hint[bid] = (min(1.0, max(0.0, float(f[0]))), min(1.0, max(0.0, float(f[1]))))
            except (TypeError, ValueError, IndexError):
                pass
        self.apart: Dict[Tuple[str, str], float] = {}
        for h in H.get("apart", []) or []:
            a, b_, g = h.get("a"), h.get("b"), float(h.get("gap", 3.0))
            if a and b_ and a != b_:
                self.apart[(a, b_)] = self.apart[(b_, a)] = max(g, self.apart.get((a, b_), 0.0))
        self.near_of = defaultdict(list)
        for i, n in enumerate(self.near):
            self.near_of[n[0]].append(i)
            self.near_of[n[1]].append(i)
        # master/slave terms, filled in stage B
        self.ms: List[tuple] = []
        self.ms_of = defaultdict(list)
        self.master_of: Dict[str, str] = {}

        # ---- regions per part
        self.region: Dict[str, Region] = {}
        for bl in self.blocks.values():
            if bl.region:
                for m in bl.members:
                    if m in self.movable:
                        self.region[m] = bl.region

        self.layers = {r: (("F", "B") if C[r].tht else (C[r].side,)) for r in self.active}

    # ---------------------------------------------------------------- geometry
    def box(self, r, x=None, y=None, rot=None, inflate=True):
        p = self.pos[r]
        x = p[0] if x is None else x
        y = p[1] if y is None else y
        rot = p[2] if rot is None else rot
        return self.C[r].bbox(x, y, rot, self.margin.get(r, 0.0) / 2 if inflate else 0.0)

    def pad_xy(self, r, pad):
        p = self.pos[r]
        return self.C[r].pad_pos(pad, p[0], p[1], p[2])

    # ------------------------------------------------------------------ costs
    def net_cost(self, net) -> float:
        d = self.nets[net]
        xs, ys = [], []
        for r, pad in d["pins"]:
            x, y = self.pad_xy(r, pad)
            xs.append(x)
            ys.append(y)
        hp = (max(xs) - min(xs)) + (max(ys) - min(ys))
        c = d["w"] * hp
        if d["max"] > 0 and hp > d["max"]:
            c += 2.0 * (hp - d["max"])
        return c

    def pair_cost(self, i) -> float:
        a, b, w = self.pairs[i]

        def cen(net):
            pts = [self.pad_xy(r, p) for r, p in self.nets[net]["pins"]]
            return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
        (ax, ay), (bx, by) = cen(a), cen(b)
        return w * 0.3 * math.hypot(ax - bx, ay - by)

    def decap_cost(self, i) -> float:
        r, pad, ic, targets, w = self.decaps[i]
        x, y = self.pad_xy(r, pad)
        return w * min(math.hypot(x - tx, y - ty) for tx, ty in (self.pad_xy(ic, t) for t in targets))

    def near_cost(self, i) -> float:
        r, tgt, tp, w, off = self.near[i]
        x, y = self.pos[r][0], self.pos[r][1]
        if off is not None:
            dx, dy = off
            if self.C[tgt].side == "B":
                dx = -dx
            ox, oy = rotate(dx, dy, self.pos[tgt][2])
            tx, ty = self.pos[tgt][0] + ox, self.pos[tgt][1] + oy
        elif tp is not None:
            tx, ty = self.pad_xy(tgt, tp)
        else:
            tx, ty = self.pos[tgt][0], self.pos[tgt][1]
        return w * math.hypot(x - tx, y - ty)

    def local_cost(self, r, hashed=True) -> float:
        """Terms that only depend on one part (overlap is counted per part)."""
        b = self.box(r)
        cost = 0.0
        # board edge & keep-outs & regions (hard)
        tight = self.box(r, inflate=False)
        cost += 3 * BIG * _outside(tight, self.area)
        cost += 3 * BIG * self.edge_pen(tight)
        for k in self.b.keepouts:
            cost += BIG * 0.5 * _ov(tight, (k.x0, k.y0, k.x1, k.y1)) ** 0.5
        reg = self.region.get(r)
        if reg:
            cost += BIG * _outside(tight, reg)
        # overlap with neighbours (hard)
        if hashed:
            for o in self.hash.query(self.layers[r], b):
                if o != r:
                    a = _ov(b, self.box(o))
                    if a > 0:
                        cost += BIG * (a + 0.5)
        # cohesion with its block centre
        bid = self.block_of.get(r)
        if bid in self.block_center and self.o.cohesion_weight > 0:
            cx, cy = self.block_center[bid]
            bl = self.blocks.get(bid)
            k = 8.0 if (bl is not None and bl.anchor == r and not self.packed) else 1.0   # masters hold their block's place
            dist = math.hypot(self.pos[r][0] - cx, self.pos[r][1] - cy)
            cost += k * self.o.cohesion_weight * dist
            # refinement rearranges inside a block; it must not drag the block away from where
            # stage A put it (next to its fixed connector, away from noisy neighbours ...)
            bs = getattr(self, "block_size", {}).get(bid)
            rad = bs * 0.75 + 2.0 if bs else 1e9
            if dist > rad and not self.packed:
                cost += 4.0 * (dist - rad)
        # displacement
        if self.o.displacement_weight > 0 and r in self.started_on_board:
            ox, oy, _ = self.orig[r]
            cost += self.o.displacement_weight * math.hypot(self.pos[r][0] - ox, self.pos[r][1] - oy)
        # AI / learned: this part belongs on a board edge
        eh = self.edge_hint.get(r)
        if eh and self.b.outline:
            A = self.area
            d = {"left": tight[0] - A.x0, "right": A.x1 - tight[2], "top": tight[1] - A.y0, "bottom": A.y1 - tight[3]}
            v = min(d.values()) if eh[0] == "any" else d[eh[0]]
            cost += eh[1] * max(0.0, v)
        # connectors like the board edge
        if role(self.C[r]) == "connector" and self.b.outline:
            A = self.area
            x, y = self.pos[r][0], self.pos[r][1]
            cost += 0.3 * min(x - A.x0, A.x1 - x, y - A.y0, A.y1 - y)
        return cost

    def ms_cost(self, i) -> float:
        slave, spad, master, mpad, nlx, nly, off, w = self.ms[i]
        mx, my = self.pad_xy(master, mpad)
        nx, ny = rotate(nlx, nly, self.pos[master][2])
        tx, ty = mx + nx * off, my + ny * off
        sx, sy = self.pad_xy(slave, spad)
        return w * math.hypot(sx - tx, sy - ty)

    def touched_terms(self, refs):
        nets, pairs, decs, nears, mss = set(), set(), set(), set(), set()
        for r in refs:
            nets.update(self.nets_of.get(r, ()))
            pairs.update(self.pairs_of.get(r, ()))
            decs.update(self.decaps_of.get(r, ()))
            nears.update(self.near_of.get(r, ()))
            mss.update(self.ms_of.get(r, ()))
        return nets, pairs, decs, nears, mss

    def terms_cost(self, terms) -> float:
        nets, pairs, decs, nears, mss = terms
        return (sum(self.net_cost(n) for n in nets) + sum(self.pair_cost(i) for i in pairs) +
                sum(self.decap_cost(i) for i in decs) + sum(self.near_cost(i) for i in nears) +
                sum(self.ms_cost(i) for i in mss))

    def total_cost(self) -> float:
        t = (set(self.nets), set(range(len(self.pairs))), set(range(len(self.decaps))), set(range(len(self.near))),
             set(range(len(self.ms))))
        return self.terms_cost(t) + sum(self.local_cost(r) for r in self.movable)

    # --------------------------------------------------------------- stage A
    def stage_a(self):
        C = self.C
        groups = defaultdict(list)
        for r in self.movable:
            groups[self.block_of.get(r, "misc")].append(r)
        self.block_center: Dict[str, Tuple[float, float]] = {}
        self.block_size: Dict[str, float] = {}
        if not groups:
            return
        bids = sorted(groups)
        A = self.area
        for bid in bids:
            tot = sum((C[r].xmax - C[r].xmin + self.margin[r]) * (C[r].ymax - C[r].ymin + self.margin[r])
                      for r in groups[bid])
            self.block_size[bid] = math.sqrt(tot * 1.35)
        # block-to-block and block-to-fixed connection weights
        W = defaultdict(float)
        pull = defaultdict(list)          # bid -> [(x, y, w)]
        for net, d in self.nets.items():
            bs = defaultdict(int)
            fx = []
            for r, pad in d["pins"]:
                if r in self.movable:
                    bs[self.block_of.get(r, "misc")] += 1
                else:
                    fx.append(self.pad_xy(r, pad))
            keys = list(bs)
            w = d["w"]
            if net_kind(net) == "pwr":
                w = max(w, 0.3)
            for i in range(len(keys)):
                for j in range(i + 1, len(keys)):
                    W[(keys[i], keys[j])] += w
                for x, y in fx:
                    pull[keys[i]].append((x, y, w))
        # same-block fixed members (e.g. a fixed connector anchoring its block) pull hard
        for bid in bids:
            bl = self.blocks.get(bid)
            if bl:
                for m in bl.members:
                    if m in self.fixed:
                        pull[bid].append((self.pos[m][0], self.pos[m][1], 4.0 if self.packed else self.o.anchor_pull))
        noisy = {bid for bid in bids if self._is_noisy(bid)}
        sensitive = {bid for bid in bids if self._is_sensitive(bid)}
        # AI / learned: preferred area of the board for a block
        for bid, (fx, fy) in self.zone_hint.items():
            if bid in groups:
                pull[bid].append((A.x0 + fx * (A.x1 - A.x0), A.y0 + fy * (A.y1 - A.y0), 6.0))

        # obstacles = fixed parts
        obst = [self.box(r) for r in self.fixed]
        cen = {}
        for bid in bids:
            rs = groups[bid]
            onb = [r for r in rs if self._on_board(r)]
            bl = self.blocks.get(bid)
            reg = bl.region if bl and bl.region else None
            if reg:
                cen[bid] = [(reg.x0 + reg.x1) / 2, (reg.y0 + reg.y1) / 2]
            elif len(onb) >= max(1, len(rs) // 2):
                cen[bid] = [sum(self.pos[r][0] for r in onb) / len(onb), sum(self.pos[r][1] for r in onb) / len(onb)]
            elif pull[bid]:
                tw = sum(p[2] for p in pull[bid])
                cen[bid] = [sum(p[0] * p[2] for p in pull[bid]) / tw, sum(p[1] * p[2] for p in pull[bid]) / tw]
            else:
                cen[bid] = [self.rng.uniform(A.x0, A.x1), self.rng.uniform(A.y0, A.y1)]

        def rect(bid, x, y):
            h = self.block_size[bid] / 2
            return (x - h, y - h, x + h, y + h)

        def bcost(bid, x, y):
            c = 0.0
            for (a, b_), w in W.items():
                if a == bid or b_ == bid:
                    o = b_ if a == bid else a
                    c += w * math.hypot(x - cen[o][0], y - cen[o][1])
            for px, py, w in pull[bid]:
                c += w * math.hypot(x - px, y - py)
            R = rect(bid, x, y)
            c += 30 * _outside(R, A)
            bl = self.blocks.get(bid)
            if bl and bl.region:
                c += 60 * _outside(R, bl.region)
            for o in bids:
                if o != bid:
                    Ro = rect(o, *cen[o])
                    c += 3.0 * _ov(R, Ro)
                    if (bid in noisy and o in sensitive) or (bid in sensitive and o in noisy):
                        gap = max(Ro[0] - R[2], R[0] - Ro[2], Ro[1] - R[3], R[1] - Ro[3])
                        if gap < self.o.separation:
                            c += 10 * (self.o.separation - gap)
                    ga = self.apart.get((bid, o))
                    if ga:
                        gap = max(Ro[0] - R[2], R[0] - Ro[2], Ro[1] - R[3], R[1] - Ro[3])
                        if gap < ga:
                            c += 10 * (ga - gap)
            for ob in obst:
                c += 2.0 * _ov(R, ob)
            for k in self.b.keepouts:
                c += 5.0 * _ov(R, (k.x0, k.y0, k.x1, k.y1))
            return c

        span = max(A.x1 - A.x0, A.y1 - A.y0)
        iters = int(400 * len(bids) * self.o.effort) + 200
        T = span / 4
        for it in range(iters):
            bid = bids[self.rng.randrange(len(bids))]
            bl = self.blocks.get(bid)
            x, y = cen[bid]
            step = max(0.3, span * 0.25 * (1 - it / iters))
            nx, ny = x + self.rng.gauss(0, step), y + self.rng.gauss(0, step)
            if self.rng.random() < 0.15 and len(bids) > 1:   # swap two blocks
                o = bids[self.rng.randrange(len(bids))]
                if o != bid:
                    before = bcost(bid, x, y) + bcost(o, *cen[o])
                    cen[bid], cen[o] = cen[o], cen[bid]
                    after = bcost(bid, *cen[bid]) + bcost(o, *cen[o])
                    if after > before and self.rng.random() > math.exp(-(after - before) / max(T, 1e-3)):
                        cen[bid], cen[o] = cen[o], cen[bid]
                continue
            d = bcost(bid, nx, ny) - bcost(bid, x, y)
            if d < 0 or self.rng.random() < math.exp(-d / max(T, 1e-3)):
                cen[bid] = [nx, ny]
            T *= 0.995 if it % 10 == 0 else 1.0
        for bid in bids:
            self.block_center[bid] = tuple(cen[bid])
        self.groups = groups

    def _is_noisy(self, bid):
        bl = self.blocks.get(bid)
        if not bl:
            return False
        if bl.kind in ("motor",):
            return True
        if bl.kind == "power" and any(self.C[m].prefix == "L" for m in bl.members if m in self.C):
            return True
        for m in bl.members:
            for n in self.C[m].nets if m in self.C else ():
                if self.rules.rule(self.b.netclass(n)).noisy:
                    return True
        return False

    def _is_sensitive(self, bid):
        bl = self.blocks.get(bid)
        if not bl:
            return False
        if bl.kind in ("analog", "clock"):
            return True
        for m in bl.members:
            for n in self.C[m].nets if m in self.C else ():
                if self.rules.rule(self.b.netclass(n)).sensitive:
                    return True
        return False

    # --------------------------------------------------------------- stage B
    def stage_b(self):
        self.hash = _Hash()
        for r in self.fixed:
            self.hash.insert(r, self.layers[r], self.box(r))
        placed: Set[str] = set(self.fixed)
        order_blocks = sorted(self.groups, key=lambda b: -len(self.groups[b]))
        for bid in order_blocks:
            rs = self.groups[bid]
            cx, cy = self.block_center[bid]
            # anchor first: block anchor if movable, otherwise the biggest part
            bl = self.blocks.get(bid)
            first = bl.anchor if bl and bl.anchor in rs else max(rs, key=lambda r: (self.C[r].pin_count, self.C[r].area))
            order = [first]
            todo = [r for r in rs if r != first]
            while todo:
                # next = strongest connection to what is already placed
                best, bw = None, -1.0
                for r in todo:
                    w = 0.0
                    for net in self.nets_of.get(r, ()):
                        d = self.nets[net]
                        if any(o in placed or o in order for o, _ in d["pins"] if o != r):
                            w += d["w"]
                    for i in self.decaps_of.get(r, ()):
                        w += 2.0
                    w += 0.001 * self.C[r].area
                    if w > bw:
                        best, bw = r, w
                order.append(best)
                todo.remove(best)
            mc = self.C[first]
            if self.o.master_slave and (role(mc) in ("ic", "connector") or mc.pin_count >= 3) and len(rs) + (
                    0 if first in rs else 1) > 1:
                self._ms_block(rs, first, (cx, cy), placed)
                continue
            for r in order:
                self._place_one(r, placed, (cx, cy), first_in_block=(r == first))
                placed.add(r)

    # ----------------------------------------------------------- master / slave
    def _links(self, r, p):
        """(pad of r, pad of p, net) pairs on nets shared by r and p (no ground, no big rails)."""
        from .classify import _is_decap
        out = []
        cr, cp = self.C[r], self.C[p]
        pnets = {pd.net: pd for pd in cp.pads if pd.net}
        for pd in cr.pads:
            if not pd.net or pd.net not in pnets:
                continue
            kind = net_kind(pd.net)
            if kind == "gnd":
                continue
            fan = len(self._members.get(pd.net, ())) if kind != "sig" else 0
            if kind == "pwr" and fan > 12 and not _is_decap(cr):
                continue
            for pp in cp.pads:
                if pp.net == pd.net:
                    out.append((pd, pp, pd.net))
        return out

    @staticmethod
    def _side(c, pad):
        """Outward normal (local frame) of the courtyard side nearest to a pad, and distance to that side."""
        cx, cy = (c.xmin + c.xmax) / 2, (c.ymin + c.ymax) / 2
        hx, hy = max(0.05, (c.xmax - c.xmin) / 2), max(0.05, (c.ymax - c.ymin) / 2)
        dx, dy = (pad.lx - cx) / hx, (pad.ly - cy) / hy
        if abs(dx) >= abs(dy):
            return ((1.0, 0.0), c.xmax - pad.lx) if dx >= 0 else ((-1.0, 0.0), pad.lx - c.xmin)
        return ((0.0, 1.0), c.ymax - pad.ly) if dy >= 0 else ((0.0, -1.0), pad.ly - c.ymin)

    def _ms_block(self, rs, master, center, placed):
        """Place the master, then each slave on the master's side where its pin is, facing that pin,
        with a routing channel in between; parts hanging off a slave go just outside that slave."""
        from .classify import _is_decap
        C = self.C
        if master in self.movable and master not in placed:
            self._place_one(master, placed, center, first_in_block=True)
            placed.add(master)
        mc = C[master]
        limit = max(30.0, 0.8 * mc.area)
        # slaves are the small support parts; big parts (pots, connectors, modules) are placed normally
        members = [r for r in rs if r != master and role(C[r]) not in ("connector", "ic", "mechanical")
                   and C[r].area <= limit]
        others = [r for r in rs if r != master and r not in members]
        # tree: who hangs off whom
        parent: Dict[str, Tuple[str, list]] = {}
        tiers: List[List[str]] = []
        frontier, seen = [master], {master}
        while frontier and len(seen) < len(members) + 1:
            tier = []
            for par in frontier:
                for r in members:
                    if r in seen:
                        continue
                    links = self._links(r, par)
                    if links:
                        parent[r] = (par, links)
                        seen.add(r)
                        tier.append(r)
            if not tier:
                break
            tiers.append(tier)
            frontier = tier

        def weight(r):
            return sum(self.nets[n]["w"] for n in self.nets_of.get(r, ()) if n in self.nets)

        usage: Dict[int, int] = defaultdict(int)
        side_world: Dict[str, Tuple[float, float]] = {}
        gap = self.o.escape_gap
        for depth, tier in enumerate(tiers):
            tier.sort(key=lambda r: (not _is_decap(C[r]), role(C[r]) != "crystal", -weight(r)))
            for r in tier:
                if r not in self.movable or r in placed:
                    continue
                par, links = parent[r]
                sig = [l for l in links if net_kind(l[2]) == "sig"]
                if _is_decap(C[r]) or not sig:
                    # decoupling: spread over the master's supply pins, least used first
                    links = sorted(links, key=lambda l: (usage[id(l[1])], net_kind(l[2]) != "pwr"))
                    spad, ppad, _ = links[0]
                else:
                    spad, ppad, _ = max(sig, key=lambda l: self.nets.get(l[2], {"w": 0})["w"])
                usage[id(ppad)] += 1
                pc = C[par]
                if par == master or par not in side_world:
                    (nlx, nly), edge = self._side(pc, ppad)
                    nwx, nwy = rotate(nlx, nly, self.pos[par][2])
                else:
                    nwx, nwy = side_world[par]
                    nlx, nly = unrotate(nwx, nwy, self.pos[par][2])
                    edge = abs((pc.xmax - pc.xmin) * nlx + (pc.ymax - pc.ymin) * nly) / 2
                off = edge + gap + max(spad.w, spad.h) / 2 + 0.5 * (self.margin.get(par, 0) + self.margin.get(r, 0))
                px, py = self.pad_xy(par, ppad)
                tx, ty = px + nwx * off, py + nwy * off
                tgx, tgy = -nwy, nwx
                c = C[r]
                depth_step = max(c.xmax - c.xmin, c.ymax - c.ymin) + self.o.clearance
                cands = []
                for rot in self.rots.get(r, [self.pos[r][2]]):
                    dx, dy = rotate(spad.lx, spad.ly, rot)
                    for ring in range(4):
                        for k in range(-16, 17):
                            t = k * 0.5
                            ox = tx - dx + tgx * t + nwx * ring * depth_step
                            oy = ty - dy + tgy * t + nwy * ring * depth_step
                            cands.append((ox, oy, rot))
                cands += self._edge_cands(r)
                terms = self.touched_terms([r])
                nets = {n for n in terms[0] if any(o in placed for o, _ in self.nets[n]["pins"] if o != r)}
                decs = {i for i in terms[2]
                        if (self.decaps[i][2] if self.decaps[i][0] == r else self.decaps[i][0]) in placed}
                nears = {i for i in terms[3]
                         if (self.near[i][1] if self.near[i][0] == r else self.near[i][0]) in placed}
                best, bc = self._scan(r, cands, placed, center, nets, decs, nears,
                                      target=(spad, tx, ty, 3.0 * self.o.slave_pull))
                if best is None or bc >= BIG:
                    self._place_one(r, placed, center)
                else:
                    self.pos[r] = list(best)
                    self.hash.insert(r, self.layers[r], self.box(r))
                placed.add(r)
                side_world[r] = (nwx, nwy)
                self.master_of[r] = par
                i = len(self.ms)
                self.ms.append((r, spad, par, ppad, nlx, nly, off, self.o.slave_pull))
                self.ms_of[r].append(i)
                self.ms_of[par].append(i)
        # big parts and anything not hanging off the master go in the usual way
        for r in members + others:
            if r in self.movable and r not in placed:
                self._place_one(r, placed, center)
                placed.add(r)

    def _candidates(self, r, placed, center):
        """Origins that put one of r's pads near a connected, already-placed pad."""
        c = self.C[r]
        out = []
        size = max(c.xmax - c.xmin, c.ymax - c.ymin)
        for net in self.nets_of.get(r, ()):
            d = self.nets[net]
            mine = [p for o, p in d["pins"] if o == r]
            others = [(o, p) for o, p in d["pins"] if o != r and o in placed]
            if not mine or not others:
                continue
            others = others[:6]
            for o, op in others:
                tx, ty = self.pad_xy(o, op)
                for rot in self.rots.get(r, [self.pos[r][2]]):
                    for p in mine[:2]:
                        from .model import rotate as _rot
                        dx, dy = _rot(p.lx, p.ly, rot)
                        for k in (0.6, 1.2, 2.0, 3.0):
                            for ang in range(0, 360, 45):
                                a = math.radians(ang)
                                ox = tx - dx + math.cos(a) * k * (0.5 + size * 0.35)
                                oy = ty - dy + math.sin(a) * k * (0.5 + size * 0.35)
                                out.append((ox, oy, rot))
        # spiral around the block centre (also for unconnected parts / block anchors)
        cx, cy = center
        rots = self.rots.get(r, [self.pos[r][2]])
        for ring in range(0, 14):
            rad = ring * (1.0 + size * 0.5)
            n = 1 if ring == 0 else 8 + ring * 4
            for i in range(n):
                a = 2 * math.pi * i / n
                for rot in rots:
                    out.append((cx + rad * math.cos(a), cy + rad * math.sin(a), rot))
        return out

    def _edge_cands(self, r):
        """Origins along the board edge(s) an AI / learned hint asks this part to sit on."""
        eh = self.edge_hint.get(r)
        if not eh:
            return []
        A = self.region.get(r) or self.area
        out = []
        sides = ("left", "right", "top", "bottom") if eh[0] == "any" else (eh[0],)
        for rot in self.rots.get(r, [self.pos[r][2]]):
            x0, y0, x1, y1 = self.C[r].extents(rot)
            m = self.margin.get(r, 0.0) / 2 + 0.05
            for side in sides:
                if side in ("left", "right"):
                    x = A.x0 - x0 + m if side == "left" else A.x1 - x1 - m
                    lo, hi = A.y0 - y0 + m, A.y1 - y1 - m
                    n = max(2, min(120, int((hi - lo) / 0.75)))
                    out += [(x, lo + (hi - lo) * i / n, rot) for i in range(n + 1)]
                else:
                    y = A.y0 - y0 + m if side == "top" else A.y1 - y1 - m
                    lo, hi = A.x0 - x0 + m, A.x1 - x1 - m
                    n = max(2, min(120, int((hi - lo) / 0.75)))
                    out += [(lo + (hi - lo) * i / n, y, rot) for i in range(n + 1)]
        return out

    def _place_one(self, r, placed, center, first_in_block=False):
        best, bc = None, math.inf
        terms = self.touched_terms([r])
        # only count terms whose other parties are already placed
        nets = {n for n in terms[0] if any(o in placed for o, _ in self.nets[n]["pins"] if o != r)}
        decs = {i for i in terms[2] if (self.decaps[i][2] if self.decaps[i][0] == r else self.decaps[i][0]) in placed}
        nears = {i for i in terms[3] if (self.near[i][1] if self.near[i][0] == r else self.near[i][0]) in placed}
        cands = self._candidates(r, placed, center)
        if len(cands) > 900:
            self.rng.shuffle(cands)
            cands = cands[:900]
        cands += self._edge_cands(r)
        save = list(self.pos[r])
        # a block's master stays where stage A put the block; on a packed board it may give way
        cw = (0.25 if self.packed else 25.0) if first_in_block else 0.25
        best, bc = self._scan(r, cands, placed, center, nets, decs, nears, center_w=cw)
        if bc >= BIG:
            # nothing legal nearby: search the whole working area on a grid
            A = self.region.get(r) or self.area
            c = self.C[r]
            step = max(0.5, min(c.xmax - c.xmin, c.ymax - c.ymin) / 2)
            while ((A.x1 - A.x0) / step) * ((A.y1 - A.y0) / step) > 6000:
                step *= 1.4
            grid = []
            y = A.y0
            while y <= A.y1:
                x = A.x0
                while x <= A.x1:
                    for rot in self.rots.get(r, [save[2]]):
                        grid.append((x, y, rot))
                    x += step
                y += step
            b2, c2 = self._scan(r, grid, placed, center, nets, decs, nears, center_w=cw)
            if c2 < bc:
                best, bc = b2, c2
        self.pos[r] = list(best) if best else save
        self.hash.insert(r, self.layers[r], self.box(r))

    def _scan(self, r, cands, placed, center, nets, decs, nears, target=None, center_w=0.25):
        best, bc = None, math.inf
        for x, y, rot in cands:
            self.pos[r] = [x, y, rot]
            b = self.box(r)
            tight = self.box(r, inflate=False)
            pen = 3 * BIG * (_outside(tight, self.area) + (0.5 if _outside(tight, self.area) > 0 else 0))
            pen += 3 * BIG * self.edge_pen(tight)
            reg = self.region.get(r)
            if reg:
                pen += BIG * _outside(tight, reg)
            for k in self.b.keepouts:
                pen += BIG * _ov(tight, (k.x0, k.y0, k.x1, k.y1))
            for o in self.hash.query(self.layers[r], b):
                if o in placed and o != r:
                    a = _ov(b, self.box(o))
                    if a > 0:
                        pen += BIG * (a + 1)
            if pen >= bc:
                continue
            cost = pen
            cost += sum(self._partial_net_cost(n, placed, r) for n in nets)
            cost += sum(self.decap_cost(i) for i in decs) + sum(self.near_cost(i) for i in nears)
            if target is not None:
                tp, tx, ty, tw = target
                px, py = self.C[r].pad_pos(tp, x, y, rot)
                cost += tw * math.hypot(px - tx, py - ty)
            else:
                cost += center_w * math.hypot(x - center[0], y - center[1])
            if role(self.C[r]) == "connector" and self.b.outline:
                A = self.area
                cost += 0.3 * min(x - A.x0, A.x1 - x, y - A.y0, A.y1 - y)
            if cost < bc:
                bc, best = cost, (x, y, rot)
        return best, bc

    def _partial_net_cost(self, net, placed, r):
        d = self.nets[net]
        xs, ys = [], []
        for o, pad in d["pins"]:
            if o == r or o in placed:
                x, y = self.pad_xy(o, pad)
                xs.append(x)
                ys.append(y)
        if len(xs) < 2:
            return 0.0
        hp = (max(xs) - min(xs)) + (max(ys) - min(ys))
        c = d["w"] * hp
        if d["max"] > 0 and hp > d["max"]:
            c += 2.0 * (hp - d["max"])
        return c

    # --------------------------------------------------------------- stage C
    def stage_c(self, deadline):
        mov = sorted(self.movable)
        if not mov:
            return
        by_block = defaultdict(list)
        for r in mov:
            by_block[self.block_of.get(r, "misc")].append(r)
        A = self.area
        span = max(A.x1 - A.x0, A.y1 - A.y0)
        iters = int(min(80000, 350 * len(mov) * self.o.effort))
        # initial temperature from sampled deltas
        T = 2.0
        step0 = min(span / 6, 8.0)
        if self.o.repair:
            step0 = 1.5
        accepted = 0
        for it in range(iters):
            if it % 256 == 0 and time.time() > deadline:
                self.log(f"stage C stopped at time limit after {it} moves")
                break
            frac = it / iters
            step = max(0.15, step0 * (1 - frac) ** 1.5)
            r = mov[self.rng.randrange(len(mov))]
            mv = self.rng.random()
            refs = [r]
            old = {r: list(self.pos[r])}
            if mv < 0.12 and len(self.rots[r]) > 1:
                rot = self.rots[r][self.rng.randrange(len(self.rots[r]))]
                new = {r: [self.pos[r][0], self.pos[r][1], rot]}
            elif mv < 0.22:
                peers = by_block[self.block_of.get(r, "misc")]
                o = peers[self.rng.randrange(len(peers))]
                if o == r or o not in self.movable or self.C[o].side != self.C[r].side:
                    continue
                if abs(self.C[o].area - self.C[r].area) > 0.6 * max(self.C[o].area, self.C[r].area):
                    continue
                refs = [r, o]
                old[o] = list(self.pos[o])
                new = {r: [old[o][0], old[o][1], old[r][2]], o: [old[r][0], old[r][1], old[o][2]]}
            elif mv < 0.35:
                # jump towards the centroid of connected pads
                pts = []
                for net in self.nets_of.get(r, ()):
                    for o, p in self.nets[net]["pins"]:
                        if o != r:
                            pts.append(self.pad_xy(o, p))
                if not pts:
                    continue
                gx = sum(p[0] for p in pts) / len(pts)
                gy = sum(p[1] for p in pts) / len(pts)
                t = self.rng.uniform(0.2, 0.8)
                x, y = self.pos[r][0], self.pos[r][1]
                new = {r: [x + (gx - x) * t, y + (gy - y) * t, self.pos[r][2]]}
            else:
                x, y, rot = self.pos[r]
                new = {r: [x + self.rng.gauss(0, step), y + self.rng.gauss(0, step), rot]}

            terms = self.touched_terms(refs)
            before = self.terms_cost(terms) + sum(self.local_cost(q) for q in refs)
            for q in refs:
                self.hash.remove(q)
            for q in refs:
                self.pos[q] = new[q]
            for q in refs:
                self.hash.insert(q, self.layers[q], self.box(q))
            after = self.terms_cost(terms) + sum(self.local_cost(q) for q in refs)
            delta = after - before
            if delta <= 0 or self.rng.random() < math.exp(-delta / max(T, 1e-4)):
                accepted += 1
            else:
                for q in refs:
                    self.hash.remove(q)
                    self.pos[q] = old[q]
                    self.hash.insert(q, self.layers[q], self.box(q))
            T = (0.4 if self.o.repair else 2.0) * (0.002 / 2.0) ** frac        # geometric cooling
        self.log(f"stage C: {accepted} accepted moves")

    # ------------------------------------------------------------ legalize
    def legalize(self, rounds=3):
        for _ in range(rounds):
            bad = []
            for r in self.movable:
                b = self.box(r)
                tight = self.box(r, inflate=False)
                hit = (_outside(tight, self.area) > 0.001 or self.edge_pen(tight) > 0.001 or
                       (r in self.region and _outside(tight, self.region[r]) > 0.001))
                if not hit:
                    for o in self.hash.query(self.layers[r], b):
                        if o != r and _ov(b, self.box(o)) > 1e-6:
                            hit = True
                            break
                if not hit:
                    for k in self.b.keepouts:
                        if _ov(tight, (k.x0, k.y0, k.x1, k.y1)) > 0:
                            hit = True
                if hit:
                    bad.append(r)
            if not bad:
                return []
            for r in sorted(bad, key=lambda q: -self.C[q].area):
                self.hash.remove(r)
                placed = set(self.active) - {r}
                c = self.block_center.get(self.block_of.get(r), (self.pos[r][0], self.pos[r][1]))
                self._place_one(r, placed, (self.pos[r][0], self.pos[r][1]) if self._inside(r) else c)
        return bad

    def check_violations(self):
        out = []
        for r in sorted(self.movable):
            tight = self.box(r, inflate=False)
            if _outside(tight, self.area) > 0.01 or self.edge_pen(tight) > 0.01:
                out.append(f"{r} is past the board edge")
            elif r in self.region and _outside(tight, self.region[r]) > 0.01:
                out.append(f"{r} is outside its block region")
            elif any(_ov(tight, (k.x0, k.y0, k.x1, k.y1)) > 0 for k in self.b.keepouts):
                out.append(f"{r} is in a keep-out")
            else:
                for o in self.hash.query(self.layers[r], tight):
                    if o != r and _ov(tight, self.box(o, inflate=False)) > 0.05:
                        out.append(f"{r} overlaps {o}")
                        break
        return out

    def edge_pen(self, tight) -> float:
        """Penalty for a courtyard too close to, or outside, a non-rectangular board edge."""
        if not self.edge_segs:
            return 0.0
        em = max(0.0, self.o.edge_margin)
        pen = 0.0
        for sg in self.edge_segs:
            if (min(sg[0], sg[2]) > tight[2] + em or max(sg[0], sg[2]) < tight[0] - em or
                    min(sg[1], sg[3]) > tight[3] + em or max(sg[1], sg[3]) < tight[1] - em):
                continue
            g = _seg_box_gap(sg, tight)
            if g < em:
                pen += (em - g) + (1.0 if g == 0 else 0.0)
        if not _inside_outline(self.edge_segs, (tight[0] + tight[2]) / 2, (tight[1] + tight[3]) / 2):
            pen += 5.0
        return pen

    def _inside(self, r):
        return _outside(self.box(r, inflate=False), self.area) < 0.001

    # ------------------------------------------------------------------- run
    def run(self):
        t0 = time.time()
        deadline = t0 + self.o.time_limit
        self.started_on_board = {r for r in self.movable if self._on_board(r)}
        self.log(f"{len(self.movable)} movable, {len(self.fixed)} fixed, {len(self.nets)} weighted nets")
        if self.density_note:
            self.log(self.density_note)
        if not self.movable:
            self.log("Nothing to place in this scope (everything is locked, fixed, frozen or already on the board).")
            self.block_center = {}
            return {}
        if self.o.repair:
            # local repair: keep the arrangement, separate the offending parts
            self.block_center = {}
            by = defaultdict(list)
            for r in self.active:
                by[self.block_of.get(r, "misc")].append(r)
            for bid, rs in by.items():
                self.block_center[bid] = (sum(self.pos[r][0] for r in rs) / len(rs),
                                          sum(self.pos[r][1] for r in rs) / len(rs))
            self.groups = by
            self.hash = _Hash()
            for r in self.active:
                self.hash.insert(r, self.layers[r], self.box(r))
            self.stage_c(deadline)
            remaining = self.legalize()
            self.violations = self.check_violations()
            return {r: (round(self.pos[r][0], 3), round(self.pos[r][1], 3), norm_angle(self.pos[r][2]))
                    for r in self.movable if tuple(self.pos[r]) != tuple(self.orig[r])}
        self.stage_a()
        self.stage_b()
        self.log(f"stage A+B done in {time.time() - t0:.1f}s")
        self.stage_c(deadline)
        remaining = self.legalize()
        self.violations = self.check_violations()
        self.log(f"done in {time.time() - t0:.1f}s; {len(remaining)} parts needed legalization")
        for v in self.violations[:20]:
            self.log("No legal spot: " + v)
        out = {}
        for r in self.movable:
            x, y, rot = self.pos[r]
            ox, oy, orot = self.orig[r]
            x, y, rot = round(x, 3), round(y, 3), norm_angle(rot)
            if abs(x - ox) > 1e-3 or abs(y - oy) > 1e-3 or abs(norm_angle(rot - orot)) > 1e-3:
                out[r] = (x, y, rot)
        return out


def propose(board, blocks, assign, rules, opts: PlaceOptions, locked=None, log=None):
    p = Placer(board, blocks, assign, rules, opts, locked, log)
    moves = p.run()
    return moves, {k: list(v) for k, v in p.block_center.items()}
