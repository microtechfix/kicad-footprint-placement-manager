"""Ground fix: stitch a copper zone's pieces together and connect its stranded pads.

After routing, a ground pour on the outer layers is usually cut into pieces by the tracks.
KiCad's DRC then reports the same root cause three ways: "isolated copper fill", "missing
connection between items" (zone piece to zone piece, or pad to zone) and "thermal relief
connection incomplete". This module reads the board file with its zone fills and plans the
smallest set of additions that repairs it:

  * one stitching via per zone piece, placed where the piece overlaps the zone's fill on
    another layer, until every piece is joined to the largest one (the trunk);
  * a via and a short track for every pad of the zone's net that no fill reaches;
  * optionally a solid zone connection for pads that only got one thermal spoke.

Nothing is moved or deleted. The plan is a list of vias, tracks and pad changes that the
caller writes to a copy of the file (write) or pushes through the KiCad API.

The fills in the file must be up to date: refill the zones (B) and save before planning.
"""
from __future__ import annotations

import json
import math
import os
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import sexpr
from .model import rotate
from .sexpr import child, children, num


# ----------------------------------------------------------------------------- data
@dataclass
class GroundRules:
    clearance: float = 0.2          # copper to copper of another net
    hole_clearance: float = 0.25    # hole edge to copper of another net
    hole_to_hole: float = 0.25
    edge_clearance: float = 0.3     # copper to board edge
    via_size: float = 0.6
    via_drill: float = 0.3
    track_width: float = 0.25
    min_track: float = 0.15
    min_spokes: int = 2


@dataclass
class Fix:
    kind: str                       # via | track | solid
    net: str = ""
    x: float = 0.0
    y: float = 0.0
    x2: float = 0.0
    y2: float = 0.0
    width: float = 0.0              # track width, or via diameter
    drill: float = 0.0
    layer: str = ""
    ref: str = ""                   # footprint (for solid, and as the reason for a via/track)
    pad: str = ""
    why: str = ""

    def to_json(self):
        return {k: v for k, v in self.__dict__.items() if v not in ("", 0.0)}


@dataclass
class GroundPlan:
    net: str = ""
    fixes: List[Fix] = field(default_factory=list)
    unsolved: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    pieces: int = 0                 # zone fill pieces found
    groups_before: int = 0          # separate groups of pieces before the fix
    groups_after: int = 0
    stranded_pads: int = 0
    starved_pads: int = 0

    @property
    def vias(self):
        return [f for f in self.fixes if f.kind == "via"]

    @property
    def tracks(self):
        return [f for f in self.fixes if f.kind == "track"]

    @property
    def solids(self):
        return [f for f in self.fixes if f.kind == "solid"]

    def summary(self) -> str:
        if not self.net or not self.pieces:
            return self.notes[0] if self.notes else "No filled copper zone found."
        s = (f"Ground fix for {self.net}: {self.pieces} fill piece(s) in {self.groups_before} group(s), "
             f"{self.stranded_pads} pad(s) not reached by the fill, {self.starved_pads} with too few spokes. "
             f"Plan: {len(self.vias)} via(s), {len(self.tracks)} track(s)")
        if self.solids:
            s += f", {len(self.solids)} pad(s) set to a solid zone connection"
        s += f"; {self.groups_after} group(s) afterwards"
        if self.unsolved:
            s += f"; {len(self.unsolved)} item(s) need a manual fix"
        return s + "."

    def to_json(self):
        return {"net": self.net, "summary": self.summary(), "fixes": [f.to_json() for f in self.fixes],
                "unsolved": self.unsolved, "notes": self.notes}


@dataclass
class _Pad:
    ref: str
    num: str
    net: str
    x: float
    y: float
    w: float            # size in the pad's own frame
    h: float
    rot: float          # absolute angle
    layers: Tuple[str, ...]
    drill: float
    smd: bool
    connect: int        # -1 inherit, 0 none, 1 thermal, 2 solid

    def local(self, px, py):
        """Board point in the pad's own frame."""
        r = math.radians(self.rot)
        c, s = math.cos(r), math.sin(r)
        dx, dy = px - self.x, py - self.y
        return dx * c - dy * s, dx * s + dy * c

    def world(self, lx, ly):
        r = math.radians(self.rot)
        c, s = math.cos(r), math.sin(r)
        return self.x + lx * c + ly * s, self.y - lx * s + ly * c

    def crossed_by(self, x1, y1, x2, y2) -> bool:
        """Does the line from (x1, y1) to (x2, y2) pass over the pad?"""
        ax, ay = self.local(x1, y1)
        bx, by = self.local(x2, y2)
        t0, t1 = 0.0, 1.0
        for d, lo, hi, a in ((bx - ax, -self.w / 2, self.w / 2, ax), (by - ay, -self.h / 2, self.h / 2, ay)):
            if abs(d) < 1e-12:
                if a < lo or a > hi:
                    return False
                continue
            u, v = (lo - a) / d, (hi - a) / d
            if u > v:
                u, v = v, u
            t0, t1 = max(t0, u), min(t1, v)
            if t0 > t1:
                return False
        return True

    def dist(self, px, py) -> float:
        lx, ly = self.local(px, py)
        dx, dy = abs(lx) - self.w / 2, abs(ly) - self.h / 2
        if dx <= 0 and dy <= 0:
            return max(dx, dy)
        return math.hypot(max(dx, 0.0), max(dy, 0.0))


@dataclass
class _Seg:
    x1: float
    y1: float
    x2: float
    y2: float
    w: float
    layer: str
    net: str


@dataclass
class _Via:
    x: float
    y: float
    size: float
    drill: float
    net: str
    layers: Tuple[str, str]


@dataclass
class _Poly:
    layer: str
    pts: List[Tuple[float, float]]
    island: bool
    box: Tuple[float, float, float, float] = (0, 0, 0, 0)
    area: float = 0.0
    idx: int = 0

    _bands: Optional[dict] = None

    BAND = 0.5

    def contains(self, x, y) -> bool:
        b = self.box
        if x < b[0] or x > b[2] or y < b[1] or y > b[3]:
            return False
        if len(self.pts) < 64:
            return _pip(self.pts, x, y)
        if self._bands is None:                       # edges bucketed by y, so big fills stay fast
            bands = defaultdict(list)
            pts, n, h = self.pts, len(self.pts), self.BAND
            for i in range(n):
                x1, y1 = pts[i]
                x2, y2 = pts[i - 1]
                if y1 == y2:
                    continue
                for k in range(int(math.floor(min(y1, y2) / h)), int(math.floor(max(y1, y2) / h)) + 1):
                    bands[k].append((x1, y1, x2, y2))
            self._bands = bands
        inside = False
        for x1, y1, x2, y2 in self._bands.get(int(math.floor(y / self.BAND)), ()):
            if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                inside = not inside
        return inside


def _pip(pts, x, y) -> bool:
    """Even-odd point in polygon; works for KiCad's fractured (keyhole) fill outlines."""
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _pt_seg(px, py, x1, y1, x2, y2) -> float:
    dx, dy = x2 - x1, y2 - y1
    L = dx * dx + dy * dy
    if L <= 1e-12:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _seg_seg(a, b) -> float:
    """Distance between two segments (x1,y1,x2,y2)."""
    def orient(ax, ay, bx, by, cx, cy):
        return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    o1 = orient(a[0], a[1], a[2], a[3], b[0], b[1])
    o2 = orient(a[0], a[1], a[2], a[3], b[2], b[3])
    o3 = orient(b[0], b[1], b[2], b[3], a[0], a[1])
    o4 = orient(b[0], b[1], b[2], b[3], a[2], a[3])
    if o1 * o2 < 0 and o3 * o4 < 0:
        return 0.0
    return min(_pt_seg(a[0], a[1], *b), _pt_seg(a[2], a[3], *b), _pt_seg(b[0], b[1], *a), _pt_seg(b[2], b[3], *a))


# ----------------------------------------------------------------------------- reading
def load_rules(pro_path: str) -> GroundRules:
    r = GroundRules()
    try:
        with open(pro_path, encoding="utf-8") as f:
            pro = json.load(f)
    except Exception:
        return r
    ds = (pro.get("board") or {}).get("design_settings") or {}
    rules = ds.get("rules") or {}
    classes = (pro.get("net_settings") or {}).get("classes") or []
    default = next((c for c in classes if c.get("name") == "Default"), classes[0] if classes else {})
    cl = [float(c.get("clearance", 0) or 0) for c in classes] + [float(rules.get("min_clearance", 0) or 0)]
    r.clearance = max(cl + [0.1])
    r.hole_clearance = float(rules.get("min_hole_clearance", r.hole_clearance) or r.hole_clearance)
    r.hole_to_hole = float(rules.get("min_hole_to_hole", r.hole_to_hole) or r.hole_to_hole)
    r.edge_clearance = max(float(rules.get("min_copper_edge_clearance", 0) or 0), 0.2)
    r.via_size = max(float(default.get("via_diameter", r.via_size) or r.via_size),
                     float(rules.get("min_via_diameter", 0) or 0))
    r.via_drill = max(float(default.get("via_drill", r.via_drill) or r.via_drill),
                      float(rules.get("min_through_hole_diameter", 0) or 0))
    ann = float(rules.get("min_via_annular_width", 0.05) or 0.05)
    r.via_size = max(r.via_size, r.via_drill + 2 * ann)
    r.min_track = float(rules.get("min_track_width", r.min_track) or r.min_track)
    r.track_width = max(float(default.get("track_width", r.track_width) or r.track_width), r.min_track)
    r.min_spokes = int(rules.get("min_resolved_spokes", r.min_spokes) or r.min_spokes)
    return r


def _net(node, names: Optional[Dict[str, str]] = None) -> str:
    """Net name of an item. KiCad 10 writes (net "GND"); KiCad 9 and older write (net 7 "GND") on
    pads and a bare (net 7) on tracks, vias and zones, with the names in a table at the top."""
    nn = child(node, "net_name")
    if nn and len(nn) > 1 and str(nn[1]):
        return str(nn[1])
    n = child(node, "net")
    if not n or len(n) < 2:
        return ""
    v = n[-1]
    if len(n) == 2 and not isinstance(v, sexpr.Str) and names is not None:
        return names.get(str(v), "")
    return str(v)


def _copper_layers(root) -> List[str]:
    out = []
    lay = child(root, "layers")
    for l in (lay[1:] if lay else []):
        if isinstance(l, list) and len(l) > 1 and str(l[1]).endswith(".Cu"):
            out.append(str(l[1]))
    return out or ["F.Cu", "B.Cu"]


class _World:
    """Everything on the board that a new via or track has to respect."""

    def __init__(self, text: str):
        root = sexpr.parse(text)
        self.root = root
        self.layers = _copper_layers(root)
        self.pads: List[_Pad] = []
        self.segs: List[_Seg] = []
        self.vias: List[_Via] = []
        self.holes: List[Tuple[float, float, float]] = []            # x, y, radius
        self.keepouts: List[Tuple[Tuple[str, ...], List[Tuple[float, float]]]] = []
        self.zones = []                                              # (net, layers, [polys], clearance, name)
        self.edges: List[Tuple[float, float, float, float]] = []
        # KiCad <= 9 net table: (net 7 "GND"); empty in KiCad 10 files
        self.net_ids: Dict[str, str] = {}
        for n in children(root, "net"):
            if len(n) >= 3:
                self.net_ids[str(n[1])] = str(n[2])
        self.net_num = {v: k for k, v in self.net_ids.items()}
        for fp in children(root, "footprint") + children(root, "module"):
            self._footprint(fp)
        for s in children(root, "segment"):
            a, b, w, lay = child(s, "start"), child(s, "end"), child(s, "width"), child(s, "layer")
            if a and b and lay:
                self.segs.append(_Seg(num(a[1]), num(a[2]), num(b[1]), num(b[2]), num(w[1]) if w else 0.2,
                                      str(lay[1]), _net(s, self.net_ids)))
        for s in children(root, "arc"):                               # track arcs: two chords are close enough
            a, m, b, w, lay = (child(s, k) for k in ("start", "mid", "end", "width", "layer"))
            if a and m and b and lay:
                ww = num(w[1]) if w else 0.2
                self.segs.append(_Seg(num(a[1]), num(a[2]), num(m[1]), num(m[2]), ww, str(lay[1]), _net(s, self.net_ids)))
                self.segs.append(_Seg(num(m[1]), num(m[2]), num(b[1]), num(b[2]), ww, str(lay[1]), _net(s, self.net_ids)))
        for v in children(root, "via"):
            at, sz, dr, lay = child(v, "at"), child(v, "size"), child(v, "drill"), child(v, "layers")
            if at:
                ls = (str(lay[1]), str(lay[2])) if lay and len(lay) > 2 else ("F.Cu", "B.Cu")
                via = _Via(num(at[1]), num(at[2]), num(sz[1]) if sz else 0.6, num(dr[1]) if dr else 0.3, _net(v, self.net_ids), ls)
                self.vias.append(via)
                self.holes.append((via.x, via.y, via.drill / 2))
        for z in children(root, "zone"):
            self._zone(z)
        for g in root[1:]:
            if isinstance(g, list) and g and g[0] in ("gr_line", "gr_rect", "gr_arc", "gr_circle", "gr_poly"):
                lay = child(g, "layer")
                if lay and str(lay[1]) == "Edge.Cuts":
                    self._edge(g)
        self._index()

    # -- parsing helpers
    def _footprint(self, fp):
        at = child(fp, "at")
        fx, fy = num(at[1]), num(at[2])
        frot = num(at[3]) if len(at) > 3 else 0.0
        ref = "?"
        for p in children(fp, "property"):
            if len(p) >= 3 and str(p[1]) == "Reference":
                ref = str(p[2])
        for p in children(fp, "fp_text"):
            if len(p) >= 3 and p[1] == "reference" and ref == "?":
                ref = str(p[2])
        for pd in children(fp, "pad"):
            pat, size, lay = child(pd, "at"), child(pd, "size"), child(pd, "layers")
            typ = str(pd[2]) if len(pd) > 2 else "smd"
            lx, ly = (num(pat[1]), num(pat[2])) if pat else (0.0, 0.0)
            prot = num(pat[3]) if pat is not None and len(pat) > 3 else 0.0
            dx, dy = rotate(lx, ly, frot)
            w, h = (num(size[1], 0.5), num(size[2], 0.5)) if size else (0.5, 0.5)
            names = [str(l) for l in lay[1:]] if lay else []
            if any(n == "*.Cu" for n in names):
                layers = tuple(self.layers)
            elif any(n == "F&B.Cu" for n in names):
                layers = (self.layers[0], self.layers[-1])
            else:
                layers = tuple(n for n in names if n.endswith(".Cu"))
            drn = child(pd, "drill")
            drill = 0.0
            if drn:
                vals = [num(v) for v in drn[1:] if not isinstance(v, list) and re.match(r"^-?[\d.]+$", str(v))]
                drill = max(vals) if vals else 0.0
            zc = child(pd, "zone_connect")
            pad = _Pad(ref, str(pd[1]), _net(pd, self.net_ids), fx + dx, fy + dy, w, h, prot, layers, drill,
                       typ in ("smd", "connect"), int(num(zc[1], -1)) if zc else -1)
            if typ == "np_thru_hole":
                pad.layers = ()
            self.pads.append(pad)
            if drill > 0:
                self.holes.append((pad.x, pad.y, drill / 2))
        for z in children(fp, "zone"):                                # footprint keep-outs: board coordinates
            self._zone(z)

    def _zone(self, z):
        lay = child(z, "layers") or child(z, "layer")
        layers = tuple(str(l) for l in lay[1:]) if lay else ()
        if any(l == "*.Cu" for l in layers):
            layers = tuple(self.layers)
        ko = child(z, "keepout")
        if ko is not None:
            v = child(ko, "vias")
            t = child(ko, "tracks")
            if (v and str(v[1]) == "not_allowed") or (t and str(t[1]) == "not_allowed"):
                poly = child(z, "polygon")
                pts = child(poly, "pts") if poly else None
                if pts:
                    self.keepouts.append((layers, [(num(p[1]), num(p[2])) for p in children(pts, "xy")]))
            return
        net = _net(z, self.net_ids)
        if not net:
            return
        polys = []
        for fpoly in children(z, "filled_polygon"):
            l = child(fpoly, "layer")
            pts = child(fpoly, "pts")
            if not l or not pts:
                continue
            p = [(num(q[1]), num(q[2])) for q in children(pts, "xy")]
            if len(p) < 3:
                continue
            isl = child(fpoly, "island")
            poly = _Poly(str(l[1]), p, isl is not None and (len(isl) == 1 or str(isl[1]) == "yes"))
            xs, ys = [q[0] for q in p], [q[1] for q in p]
            poly.box = (min(xs), min(ys), max(xs), max(ys))
            poly.area = abs(sum(p[i][0] * p[(i + 1) % len(p)][1] - p[(i + 1) % len(p)][0] * p[i][1]
                                for i in range(len(p)))) / 2
            polys.append(poly)
        cp = child(z, "connect_pads")
        mode = 1
        if cp is not None:
            words = [str(a) for a in cp[1:] if not isinstance(a, list)]
            mode = 2 if "yes" in words else 0 if "no" in words else 3 if "thru_hole_only" in words else 1
        name = child(z, "name")
        uid = child(z, "uuid")
        self.zones.append({"net": net, "layers": layers, "polys": polys, "mode": mode,
                           "name": str(name[1]) if name else "", "uuid": str(uid[1]) if uid else ""})

    def _edge(self, g):
        h = g[0]
        pt = lambda k: (num(child(g, k)[1]), num(child(g, k)[2])) if child(g, k) else None
        if h == "gr_line" and pt("start") and pt("end"):
            self.edges.append((*pt("start"), *pt("end")))
        elif h == "gr_rect" and pt("start") and pt("end"):
            (x1, y1), (x2, y2) = pt("start"), pt("end")
            self.edges += [(x1, y1, x2, y1), (x2, y1, x2, y2), (x2, y2, x1, y2), (x1, y2, x1, y1)]
        elif h == "gr_arc" and pt("start") and pt("mid") and pt("end"):
            self.edges += [(*pt("start"), *pt("mid")), (*pt("mid"), *pt("end"))]
        elif h == "gr_circle" and pt("center") and pt("end"):
            (cx, cy), e = pt("center"), pt("end")
            r = math.hypot(e[0] - cx, e[1] - cy)
            ring = [(cx + r * math.cos(a * math.pi / 12), cy + r * math.sin(a * math.pi / 12)) for a in range(25)]
            self.edges += [(*ring[i], *ring[i + 1]) for i in range(24)]
        elif h == "gr_poly":
            p = child(g, "pts")
            q = [(num(a[1]), num(a[2])) for a in children(p, "xy")] if p else []
            self.edges += [(*q[i], *q[(i + 1) % len(q)]) for i in range(len(q))]

    # -- spatial index
    CELL = 2.0

    def _index(self):
        self.grid = defaultdict(list)
        for i, p in enumerate(self.pads):
            r = math.hypot(p.w, p.h) / 2
            self._put(("pad", i), p.x - r, p.y - r, p.x + r, p.y + r)
        for i, s in enumerate(self.segs):
            self._put(("seg", i), min(s.x1, s.x2) - s.w, min(s.y1, s.y2) - s.w, max(s.x1, s.x2) + s.w,
                      max(s.y1, s.y2) + s.w)
        for i, v in enumerate(self.vias):
            self._put(("via", i), v.x - v.size, v.y - v.size, v.x + v.size, v.y + v.size)
        self.hole_grid = defaultdict(list)
        for i, (x, y, r) in enumerate(self.holes):
            self.hole_grid[(int(x // self.CELL), int(y // self.CELL))].append(i)

    def _put(self, key, x0, y0, x1, y1):
        c = self.CELL
        for gx in range(int(x0 // c), int(x1 // c) + 1):
            for gy in range(int(y0 // c), int(y1 // c) + 1):
                self.grid[(gx, gy)].append(key)

    def near(self, x0, y0, x1, y1):
        c = self.CELL
        seen = set()
        for gx in range(int(x0 // c), int(x1 // c) + 1):
            for gy in range(int(y0 // c), int(y1 // c) + 1):
                for k in self.grid.get((gx, gy), ()):
                    if k not in seen:
                        seen.add(k)
                        yield k

    def add_via(self, v: _Via):
        self.vias.append(v)
        self._put(("via", len(self.vias) - 1), v.x - v.size, v.y - v.size, v.x + v.size, v.y + v.size)
        self.holes.append((v.x, v.y, v.drill / 2))
        self.hole_grid[(int(v.x // self.CELL), int(v.y // self.CELL))].append(len(self.holes) - 1)

    def add_seg(self, s: _Seg):
        self.segs.append(s)
        self._put(("seg", len(self.segs) - 1), min(s.x1, s.x2) - s.w, min(s.y1, s.y2) - s.w,
                  max(s.x1, s.x2) + s.w, max(s.y1, s.y2) + s.w)

    # -- legality
    def via_ok(self, x, y, net, r: GroundRules, own_pad: Optional[_Pad] = None) -> bool:
        vr = r.via_size / 2
        hr = r.via_drill / 2
        reach = vr + max(r.clearance, r.hole_clearance) + 0.6
        for kind, i in self.near(x - reach, y - reach, x + reach, y + reach):
            if kind == "pad":
                p = self.pads[i]
                if not p.layers:                        # paste-only apertures and bare holes carry no copper
                    continue                            # (holes are checked separately below)
                d = p.dist(x, y)
                if p.net != net or not p.net:
                    if d < vr + r.clearance or d < hr + r.hole_clearance:
                        return False
                elif p.smd and d < vr + 0.05:
                    # not in a small pad of its own net (solder wicks away); a large pad such as a
                    # QFN's exposed pad takes thermal vias, as long as the via is fully inside it
                    if min(p.w, p.h) < 2.5 * r.via_size or d > -(vr + 0.15):
                        return False
            elif kind == "seg":
                s = self.segs[i]
                if s.net == net:
                    continue
                d = _pt_seg(x, y, s.x1, s.y1, s.x2, s.y2) - s.w / 2
                if d < vr + r.clearance or d < hr + r.hole_clearance:
                    return False
            else:
                v = self.vias[i]
                d = math.hypot(x - v.x, y - v.y)
                if v.net != net and d < vr + v.size / 2 + r.clearance:
                    return False
        gx, gy = int(x // self.CELL), int(y // self.CELL)
        for ax in (gx - 1, gx, gx + 1):
            for ay in (gy - 1, gy, gy + 1):
                for i in self.hole_grid.get((ax, ay), ()):
                    hx, hy, rr = self.holes[i]
                    if math.hypot(x - hx, y - hy) < hr + rr + r.hole_to_hole:
                        return False
        for e in self.edges:
            if _pt_seg(x, y, *e) < vr + r.edge_clearance:
                return False
        for _, pts in self.keepouts:
            if _pip(pts, x, y) or any(_pt_seg(x, y, *pts[i], *pts[(i + 1) % len(pts)]) < vr for i in range(len(pts))):
                return False
        return True

    def point_ok(self, x, y, half, layer, net, r: GroundRules) -> bool:
        """Can copper of radius `half` sit at (x, y) on this layer? Used by the maze router."""
        m = half + r.clearance + 0.6
        for kind, i in self.near(x - m, y - m, x + m, y + m):
            if kind == "pad":
                p = self.pads[i]
                if (p.net == net and p.net) or not p.layers:
                    continue
                d = p.dist(x, y)
                if layer in p.layers:
                    if d < half + r.clearance:
                        return False
                elif p.drill and math.hypot(x - p.x, y - p.y) < half + p.drill / 2 + r.hole_clearance:
                    return False
            elif kind == "seg":
                s = self.segs[i]
                if s.net != net and s.layer == layer and \
                        _pt_seg(x, y, s.x1, s.y1, s.x2, s.y2) < half + s.w / 2 + r.clearance:
                    return False
            else:
                v = self.vias[i]
                if v.net != net and math.hypot(x - v.x, y - v.y) < half + v.size / 2 + r.clearance:
                    return False
        for layers, pts in self.keepouts:
            if (not layers or layer in layers) and _pip(pts, x, y):
                return False
        for e in self.edges:
            if _pt_seg(x, y, *e) < half + r.edge_clearance:
                return False
        return True

    def track_ok(self, x1, y1, x2, y2, w, layer, net, r: GroundRules) -> bool:
        m = w / 2 + r.clearance + 0.6
        a = (x1, y1, x2, y2)
        for kind, i in self.near(min(x1, x2) - m, min(y1, y2) - m, max(x1, x2) + m, max(y1, y2) + m):
            if kind == "pad":
                p = self.pads[i]
                if (p.net == net and p.net) or not p.layers:
                    continue
                if layer not in p.layers and not p.drill:
                    continue
                # sample along the track; pads are small, so a fine step is enough
                n = max(2, int(math.hypot(x2 - x1, y2 - y1) / 0.1) + 1)
                need = w / 2 + (r.clearance if layer in p.layers else r.hole_clearance - min(p.w, p.h) / 2 + p.drill / 2)
                for k in range(n + 1):
                    t = k / n
                    if p.dist(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t) < need:
                        return False
            elif kind == "seg":
                s = self.segs[i]
                if s.net == net or s.layer != layer:
                    continue
                if _seg_seg(a, (s.x1, s.y1, s.x2, s.y2)) < w / 2 + s.w / 2 + r.clearance:
                    return False
            else:
                v = self.vias[i]
                if v.net == net:
                    continue
                if _pt_seg(v.x, v.y, *a) < w / 2 + v.size / 2 + r.clearance:
                    return False
        for layers, pts in self.keepouts:
            if layers and layer not in layers:
                continue
            if _pip(pts, x1, y1) or _pip(pts, x2, y2):
                return False
            if any(_seg_seg(a, (*pts[i], *pts[(i + 1) % len(pts)])) < w / 2 for i in range(len(pts))):
                return False
        for e in self.edges:
            if _seg_seg(a, e) < w / 2 + r.edge_clearance:
                return False
        return True


# ----------------------------------------------------------------------------- planning
class _Groups:
    """Union-find over the net's copper: fill pieces are numbered 0..n-1, everything else (pads,
    tracks, vias) joins in under its own key."""

    def __init__(self, n):
        self.n = n
        self.p: Dict[object, object] = {i: i for i in range(n)}

    def find(self, a):
        p = self.p
        if a not in p:
            p[a] = a
            return a
        while p[a] != a:
            p[a] = p[p[a]]
            a = p[a]
        return a

    def join(self, a, b):
        a, b = self.find(a), self.find(b)
        if a != b:
            self.p[b] = a

    def count(self):
        return len({self.find(i) for i in range(self.n)})


def _spokes(pad: _Pad, polys: List[_Poly], layer: str) -> Tuple[int, List[_Poly]]:
    """How many sides of the pad the fill reaches (0 = not connected), and every piece it touches.
    A pad between two cuts can touch two different pieces, and so joins them."""
    a, b, e = pad.w / 2, pad.h / 2, 0.08
    k = (min(a, b) + e) * 0.7071
    best, touched = 0, []
    for group in (((a + e, 0), (-a - e, 0), (0, b + e), (0, -b - e)),       # spokes at 90 degrees
                  ((k, k), (-k, k), (k, -k), (-k, -k))):                    # at 45 (KiCad's choice for round pads)
        n = 0
        for lx, ly in group:
            x, y = pad.world(lx, ly)
            for p in polys:
                if p.layer == layer and p.contains(x, y):
                    n += 1
                    if p not in touched:
                        touched.append(p)
                    break
        best = max(best, n)
    if not best:
        # solid connection: the fill covers the pad (probe just inside its edge, because a
        # through-hole pad has no copper at its centre)
        for lx, ly in ((0, 0), (a - 0.05, 0), (-a + 0.05, 0), (0, b - 0.05), (0, -b + 0.05)):
            x, y = pad.world(lx, ly)
            for p in polys:
                if p.layer == layer and p.contains(x, y):
                    return 4, [p]
    return best, touched


def _ring(cx, cy, r0, r1, step, n=20):
    r = r0
    while r <= r1 + 1e-9:
        k = max(8, int(n * r / max(r0, 0.4)))
        k = min(k, 48)
        for i in range(k):
            a = 2 * math.pi * i / k
            yield cx + r * math.cos(a), cy + r * math.sin(a), r
        r += step


def _maze(w: "_World", r: GroundRules, sx, sy, layer, net, tw, goal, reach: float, step: float = 0.1):
    """Shortest track from (sx, sy) to the first cell where goal(x, y) is true, around other
    nets' copper. Returns (legs, goal value) or None. goal returns a truthy value when a track
    may end there."""
    import heapq
    half = tw / 2 + 0.04                      # a little spare, because the path is sampled on a grid
    n = int(reach / step)
    free: Dict[Tuple[int, int], bool] = {}

    def ok(i, j):
        v = free.get((i, j))
        if v is None:
            v = free[(i, j)] = w.point_ok(sx + i * step, sy + j * step, half, layer, net, r)
        return v

    dist = {(0, 0): 0.0}
    prev: Dict[Tuple[int, int], Tuple[int, int]] = {}
    heap = [(0.0, 0, 0)]
    moves = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
             (1, 1, 1.4142), (1, -1, 1.4142), (-1, 1, 1.4142), (-1, -1, 1.4142))
    end, val = None, None
    while heap:
        d, i, j = heapq.heappop(heap)
        if d > dist.get((i, j), 1e9):
            continue
        if (i or j) and d * step >= 0.3:
            val = goal(sx + i * step, sy + j * step)
            if val:
                end = (i, j)
                break
        for di, dj, c in moves:
            a, b = i + di, j + dj
            if abs(a) > n or abs(b) > n or not ok(a, b):
                continue
            if di and dj and not (ok(i + di, j) and ok(i, j + dj)):
                continue                       # no squeezing diagonally between two obstacles
            nd = d + c
            if nd < dist.get((a, b), 1e9):
                dist[(a, b)] = nd
                prev[(a, b)] = (i, j)
                heapq.heappush(heap, (nd, a, b))
    if end is None:
        return None
    cells = [end]
    while cells[-1] != (0, 0):
        cells.append(prev[cells[-1]])
    cells.reverse()
    pts = [(sx + i * step, sy + j * step) for i, j in cells]
    # merge runs in the same direction, then cut corners where the straight line is legal
    keep = [pts[0]]
    for k in range(1, len(pts) - 1):
        ax, ay = pts[k][0] - pts[k - 1][0], pts[k][1] - pts[k - 1][1]
        bx, by = pts[k + 1][0] - pts[k][0], pts[k + 1][1] - pts[k][1]
        if abs(ax * by - ay * bx) > 1e-9:
            keep.append(pts[k])
    keep.append(pts[-1])
    out, k = [keep[0]], 0
    while k < len(keep) - 1:
        m = len(keep) - 1
        while m > k + 1 and not w.track_ok(*keep[k], *keep[m], tw, layer, net, r):
            m -= 1
        out.append(keep[m])
        k = m
    legs = [(*out[q], *out[q + 1]) for q in range(len(out) - 1)]
    if not all(w.track_ok(*leg, tw, layer, net, r) for leg in legs):
        return None
    return legs, val


def zone_nets(text: str) -> List[str]:
    """Nets that have a filled copper zone, largest fill first."""
    w = _World(text)
    area = defaultdict(float)
    for z in w.zones:
        area[z["net"]] += sum(p.area for p in z["polys"])
    return sorted(area, key=lambda n: -area[n])


def plan(text: str, rules: Optional[GroundRules] = None, net: Optional[str] = None,
         solid_starved: bool = True, max_reach: float = 6.0) -> GroundPlan:
    """Work out the vias, tracks and pad changes that make the zone net one connected piece."""
    r = rules or GroundRules()
    w = _World(text)
    out = GroundPlan()
    nets = defaultdict(float)
    for z in w.zones:
        nets[z["net"]] += sum(p.area for p in z["polys"]) + 1e-6
    if not nets:
        out.notes.append("The board has no copper zone with a net, so there is nothing to fix.")
        return out
    net = net or ("GND" if "GND" in nets else max(nets, key=nets.get))
    if net not in nets:
        out.notes.append(f"No copper zone on net {net}.")
        return out
    out.net = net
    zones = [z for z in w.zones if z["net"] == net]
    polys: List[_Poly] = [p for z in zones for p in z["polys"]]
    for i, p in enumerate(polys):
        p.idx = i
    out.pieces = len(polys)
    if not polys:
        out.notes.append(f"The {net} zone is not filled. Press B in the PCB editor to fill the zones, save, and try again.")
        return out
    zone_mode = zones[0]["mode"]
    zlayers = sorted({p.layer for p in polys}, key=lambda l: w.layers.index(l) if l in w.layers else 99)

    def pieces_at(x, y):
        return [p for p in polys if p.contains(x, y)]

    # --- what is connected already: fill pieces, pads, tracks and vias of the net as one graph
    g = _Groups(len(polys))
    net_pads = [p for p in w.pads if p.net == net and p.layers]
    pad_no = {id(p): i for i, p in enumerate(net_pads)}
    pad_piece: Dict[int, List[_Poly]] = {}
    pad_spokes: Dict[int, int] = {}
    for i, p in enumerate(net_pads):
        found, least = [], 9
        for lay in p.layers:
            if lay not in zlayers:
                continue
            n, touched = _spokes(p, polys, lay)
            if touched:
                found += [q for q in touched if q not in found]
                least = min(least, n)
        pad_piece[i] = found
        pad_spokes[i] = least if found else 0
        for q in found:                               # a plated through-hole pad joins the layers
            g.join(("p", i), q.idx)
    by_ref = defaultdict(list)                        # a tab built from overlapping pads is one piece of copper
    for i, p in enumerate(net_pads):
        by_ref[p.ref].append(i)
    for ids in by_ref.values():
        for a in range(len(ids)):
            for b in range(a + 1, len(ids)):
                pa, pb = net_pads[ids[a]], net_pads[ids[b]]
                if set(pa.layers) & set(pb.layers) and (pa.dist(pb.x, pb.y) <= 0 or pb.dist(pa.x, pa.y) <= 0):
                    g.join(("p", ids[a]), ("p", ids[b]))
    for k, v in enumerate(w.vias):
        if v.net != net:
            continue
        for q in pieces_at(v.x, v.y):
            g.join(("v", k), q.idx)
        for kind, i in w.near(v.x - 0.1, v.y - 0.1, v.x + 0.1, v.y + 0.1):
            if kind == "pad" and id(w.pads[i]) in pad_no and w.pads[i].dist(v.x, v.y) <= 0:
                g.join(("v", k), ("p", pad_no[id(w.pads[i])]))
    for k, sg in enumerate(w.segs):
        if sg.net != net:
            continue
        node = ("s", k)
        for x, y in ((sg.x1, sg.y1), (sg.x2, sg.y2)):
            for q in polys:
                if q.layer == sg.layer and q.contains(x, y):
                    g.join(node, q.idx)
            for kind, i in w.near(x - 0.1, y - 0.1, x + 0.1, y + 0.1):
                if kind == "pad":
                    p = w.pads[i]
                    if id(p) in pad_no and sg.layer in p.layers and p.dist(x, y) <= 0.01:
                        g.join(node, ("p", pad_no[id(p)]))
                elif kind == "via":
                    v = w.vias[i]
                    if v.net == net and math.hypot(v.x - x, v.y - y) <= v.size / 2:
                        g.join(node, ("v", i))
                elif i != k:
                    o = w.segs[i]                     # shared end points and T-junctions
                    if o.net == net and o.layer == sg.layer and \
                            _pt_seg(x, y, o.x1, o.y1, o.x2, o.y2) <= max(o.w, sg.w) / 2:
                        g.join(node, ("s", i))
    for i, p in enumerate(net_pads):                  # a track passing over a pad without ending on it
        m = max(p.w, p.h) / 2
        for kind, k in w.near(p.x - m, p.y - m, p.x + m, p.y + m):
            if kind == "seg":
                sg = w.segs[k]
                if sg.net == net and sg.layer in p.layers and p.crossed_by(sg.x1, sg.y1, sg.x2, sg.y2):
                    g.join(("p", i), ("s", k))
    out.groups_before = g.count()
    if len(zlayers) == 1 and out.groups_before > 1:
        out.notes.append(f"The {net} zone is filled on {zlayers[0]} only, so there is no second layer to stitch "
                         f"its pieces through. Add the zone to another copper layer (zone properties), refill "
                         f"and run the fix again.")

    trunk = max(polys, key=lambda p: p.area)

    def add_via(x, y, why, ref="", pad=""):
        out.fixes.append(Fix("via", net, round(x, 4), round(y, 4), width=r.via_size, drill=r.via_drill,
                             ref=ref, pad=pad, why=why))
        w.add_via(_Via(x, y, r.via_size, r.via_drill, net, (w.layers[0], w.layers[-1])))
        hit = pieces_at(x, y)
        node = ("v", len(w.vias) - 1)
        for a in hit:
            g.join(node, a.idx)
        return node

    # --- 1. stitch every piece to the trunk, biggest pieces first
    def stitch(piece: _Poly) -> bool:
        """Find a via position inside this piece that also lands in a piece of another group."""
        me = g.find(piece.idx)
        b = piece.box
        others = [p for p in polys if p.layer != piece.layer and g.find(p.idx) != me and
                  not (p.box[2] < b[0] or p.box[0] > b[2] or p.box[3] < b[1] or p.box[1] > b[3])]
        if not others:
            return False
        tr = g.find(trunk.idx)
        others.sort(key=lambda p: (g.find(p.idx) != tr, -p.area))
        step = 0.25 if (b[2] - b[0]) * (b[3] - b[1]) < 400 else 0.5
        best = None
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        y = b[1] + step / 2
        while y < b[3]:
            x = b[0] + step / 2
            while x < b[2]:
                if piece.contains(x, y):
                    tgt = next((o for o in others if o.contains(x, y)), None)
                    if tgt is not None and w.via_ok(x, y, net, r):
                        # prefer the trunk's group, then a spot well inside both pieces
                        inner = sum(1 for dx, dy in ((0.5, 0), (-0.5, 0), (0, 0.5), (0, -0.5))
                                    if piece.contains(x + dx, y + dy) and tgt.contains(x + dx, y + dy))
                        score = (g.find(tgt.idx) == tr, inner, -math.hypot(x - cx, y - cy))
                        if best is None or score > best[0]:
                            best = (score, x, y)
                            if score[0] and inner == 4 and step >= 0.5:
                                break
                x += step
            y += step
        if best is None:
            return False
        add_via(best[1], best[2], "joins a separate piece of the fill to the rest")
        return True

    progress = True
    while progress and g.count() > 1:
        progress = False
        tr = g.find(trunk.idx)
        for piece in sorted(polys, key=lambda p: -p.area):
            if g.find(piece.idx) != tr and stitch(piece):
                progress = True
    # --- 2. pads the fill does not reach, or reaches only inside a piece that is still cut off
    width = max(r.min_track, min(r.track_width, 0.3))
    for i, p in enumerate(net_pads):
        tr = g.find(trunk.idx)
        pieces = pad_piece[i]
        if g.find(("p", i)) == tr:
            continue
        if zone_mode == 0:
            continue
        out.stranded_pads += 1
        layer = p.layers[0]
        tw = max(r.min_track, min(width, min(p.w, p.h)))

        def route(x, y):
            """A straight or one-bend track from the pad to (x, y), or None."""
            if w.track_ok(p.x, p.y, x, y, tw, layer, net, r):
                return [(p.x, p.y, x, y)]
            for bx, by in ((x, p.y), (p.x, y)):
                if (w.track_ok(p.x, p.y, bx, by, tw, layer, net, r) and
                        w.track_ok(bx, by, x, y, tw, layer, net, r)):
                    return [(p.x, p.y, bx, by), (bx, by, x, y)]
            return None

        found = None
        start = min(p.w, p.h) / 2 + 0.3
        for x, y, rad in _ring(p.x, p.y, start, max_reach, 0.15):
            hit = [q for q in pieces_at(x, y) if g.find(q.idx) == tr]
            if not hit:
                continue
            same = [q for q in hit if q.layer == layer]
            if same and all(same[0].contains(x + dx, y + dy) for dx, dy in
                            ((0.2, 0), (-0.2, 0), (0, 0.2), (0, -0.2))):
                legs = route(x, y)                     # the fill is right there on the pad's own layer
                if legs:
                    found = (legs, None)
                    break
            if p.smd and rad >= min(p.w, p.h) / 2 + r.via_size / 2 + 0.1 and w.via_ok(x, y, net, r):
                legs = route(x, y)
                if legs:
                    found = (legs, (x, y))
                    break
        if found is None:
            # boxed in: thread a track around the other nets to the nearest fill or via position
            def goal(x, y, _layer=layer):
                hit = [q for q in pieces_at(x, y) if g.find(q.idx) == tr]
                if not hit:
                    return None
                same = [q for q in hit if q.layer == _layer]
                if same and all(same[0].contains(x + dx, y + dy) for dx, dy in
                                ((0.2, 0), (-0.2, 0), (0, 0.2), (0, -0.2))):
                    return "fill"
                if p.smd and p.dist(x, y) >= r.via_size / 2 + 0.1 and w.via_ok(x, y, net, r):
                    return "via"
                return None
            for lay in (p.layers if not p.smd else (layer,)):
                if lay not in zlayers and not p.smd:
                    continue
                res = _maze(w, r, p.x, p.y, lay, net, tw, lambda x, y, _l=lay: goal(x, y, _l), max_reach)
                if res:
                    legs, kind = res
                    layer = lay
                    found = (legs, (legs[-1][2], legs[-1][3]) if kind == "via" else None)
                    break
        if found is None:
            out.unsolved.append(f"{p.ref} pad {p.num} at ({p.x:.2f}, {p.y:.2f}): no room for a via or a "
                                f"track to the fill within {max_reach:g} mm")
            continue
        legs, via = found
        for x1, y1, x2, y2 in legs:
            out.fixes.append(Fix("track", net, round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4), width=tw,
                                 layer=layer, ref=p.ref, pad=p.num,
                                 why="connects the pad to its new via" if via else "connects the pad to the fill"))
            w.add_seg(_Seg(x1, y1, x2, y2, tw, layer, net))
        if via:
            add_via(via[0], via[1], "connects a pad the fill does not reach", p.ref, p.num)
        g.join(("p", i), trunk.idx)                   # and with the pad, any cut-off piece it sits in
    # --- 3. pads with too few thermal spokes
    for i, p in enumerate(net_pads):
        if not p.smd or p.connect == 2 or zone_mode != 1:
            continue
        n = pad_spokes[i]
        if 0 < n < r.min_spokes:
            out.starved_pads += 1
            if solid_starved:
                out.fixes.append(Fix("solid", net, round(p.x, 4), round(p.y, 4), ref=p.ref, pad=p.num,
                                     why=f"only {n} thermal spoke(s) fit; a solid connection needs none"))
    out.groups_after = g.count()
    tr = g.find(trunk.idx)
    left = [p for p in polys if g.find(p.idx) != tr]
    used = {q.idx for qs in pad_piece.values() for q in qs}
    slivers = [p for p in left if p.area < 1.5 and p.idx not in used]
    if slivers:
        out.notes.append(f"{len(slivers)} sliver(s) of fill under 1.5 mm² with no pads are too small for a via "
                         f"and were left alone.")
    for p in left:
        if p in slivers:
            continue
        cx, cy = (p.box[0] + p.box[2]) / 2, (p.box[1] + p.box[3]) / 2
        out.unsolved.append(f"fill piece on {p.layer} near ({cx:.1f}, {cy:.1f}), {p.area:.1f} mm²: no legal via "
                            f"position reaches another layer's fill")
    if left:
        out.notes.append("Setting the zone's 'Remove islands' option to 'Always' drops unconnected pieces at "
                         "the next fill.")
    if out.solids:
        out.notes.append("Solid zone connections make hand soldering those pads slower, because the pour draws "
                         "the heat away. Reflow is unaffected.")
    return out


# ----------------------------------------------------------------------------- writing
def _fmt(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def _top_level_end(text: str) -> int:
    i = text.rstrip().rfind(")")
    return i


def _find_block(text: str, start: int) -> int:
    """Index just past the s-expression that opens at text[start] == '('."""
    depth, i, n, q = 0, start, len(text), False
    while i < n:
        c = text[i]
        if q:
            if c == "\\":
                i += 1
            elif c == '"':
                q = False
        elif c == '"':
            q = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def write(text: str, gp: GroundPlan) -> str:
    """Return the board text with the plan's vias, tracks and pad changes added."""
    w = _World(text)
    top, bottom = w.layers[0], w.layers[-1]
    # pad changes first, while the text offsets are still the file's
    if gp.solids:
        want = {(f.ref, f.pad) for f in gp.solids}
        edits = []
        for m in re.finditer(r"\((?:footprint|module)\b", text):
            end = _find_block(text, m.start())
            block = text[m.start():end]
            rm = (re.search(r'\(property "Reference" "((?:[^"\\]|\\.)*)"', block) or
                  re.search(r'\(fp_text reference "?([^"\s)]+)"?', block))
            if not rm:
                continue
            ref = rm.group(1)
            if not any(a == ref for a, _ in want):
                continue
            for pm in re.finditer(r'\(pad "((?:[^"\\]|\\.)*)"', block):
                if (ref, pm.group(1)) not in want:
                    continue
                ps = m.start() + pm.start()
                pe = _find_block(text, ps)
                pad = text[ps:pe]
                if f'(net "{gp.net}")' not in pad and not re.search(r'\(net \d+ "' + re.escape(gp.net) + '"', pad):
                    continue
                if "(zone_connect" in pad:
                    new = re.sub(r"\(zone_connect \d+\)", "(zone_connect 2)", pad)
                else:
                    indent = re.search(r"\n([ \t]*)\(net ", pad)
                    ind = indent.group(1) if indent else "\t\t\t"
                    new = pad[:-1].rstrip() + f"\n{ind}(zone_connect 2)\n{ind[:-1]})"
                edits.append((ps, pe, new))
        for ps, pe, new in sorted(edits, reverse=True):
            text = text[:ps] + new + text[pe:]
    num = w.net_num.get(gp.net)
    net_ref = f"(net {num})" if num is not None else f'(net "{gp.net}")'
    add = []
    for f in gp.fixes:
        if f.kind == "via":
            add.append(f'\t(via\n\t\t(at {_fmt(f.x)} {_fmt(f.y)})\n\t\t(size {_fmt(f.width)})\n\t\t(drill {_fmt(f.drill)})\n'
                       f'\t\t(layers "{top}" "{bottom}")\n\t\t{net_ref}\n\t\t(uuid "{uuid.uuid4()}")\n\t)\n')
        elif f.kind == "track":
            add.append(f'\t(segment\n\t\t(start {_fmt(f.x)} {_fmt(f.y)})\n\t\t(end {_fmt(f.x2)} {_fmt(f.y2)})\n'
                       f'\t\t(width {_fmt(f.width)})\n\t\t(layer "{f.layer}")\n\t\t{net_ref}\n'
                       f'\t\t(uuid "{uuid.uuid4()}")\n\t)\n')
    if add:
        # tracks and vias live before the zones; any top-level position is valid for KiCad
        m = re.search(r"\n\t\(zone\b", text)
        at = m.start() + 1 if m else _top_level_end(text)
        text = text[:at] + "".join(add) + text[at:]
    return text


def fix_file(path: str, out_path: Optional[str] = None, net: Optional[str] = None,
             solid_starved: bool = True) -> Tuple[GroundPlan, str]:
    """Plan and write a fixed COPY of the board. Never overwrites the input."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    rules = load_rules(os.path.splitext(path)[0] + ".kicad_pro")
    gp = plan(text, rules, net=net, solid_starved=solid_starved)
    if not out_path:
        out_path = os.path.splitext(path)[0] + "-gndfix.kicad_pcb"
    if os.path.abspath(out_path) == os.path.abspath(path):
        raise ValueError("The ground fix writes a copy; choose a different output file.")
    if gp.fixes:
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(write(text, gp))
        pro = os.path.splitext(path)[0] + ".kicad_pro"
        dst = os.path.splitext(out_path)[0] + ".kicad_pro"
        if os.path.exists(pro) and not os.path.exists(dst):
            import shutil
            shutil.copy(pro, dst)
    return gp, out_path
