"""KiCad-independent board model used by every stage of the placement manager.

Units are millimetres, angles are degrees, and the coordinate system matches
KiCad (x to the right, y downwards, positive rotation is counter-clockwise on
screen).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


def rotate(x: float, y: float, deg: float) -> Tuple[float, float]:
    """Rotate a local footprint vector into board space (KiCad convention)."""
    if deg == 0:
        return x, y
    q = deg % 360.0                      # exact quarter turns are by far the most common
    if q == 90.0:
        return y, -x
    if q == 180.0:
        return -x, -y
    if q == 270.0:
        return -y, x
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return x * c + y * s, -x * s + y * c


def unrotate(x: float, y: float, deg: float) -> Tuple[float, float]:
    return rotate(x, y, -deg)


def norm_angle(deg: float) -> float:
    deg = deg % 360.0
    if deg > 180.0:
        deg -= 360.0
    return round(deg, 4)


@dataclass
class Pad:
    number: str
    net: str            # "" when unconnected
    lx: float           # offset from footprint origin, unrotated footprint frame
    ly: float
    w: float = 0.5
    h: float = 0.5
    smd: bool = True


@dataclass
class Component:
    ref: str
    uid: str
    value: str = ""
    footprint: str = ""
    x: float = 0.0
    y: float = 0.0
    rot: float = 0.0
    side: str = "F"                      # "F" or "B"
    locked: bool = False
    tht: bool = False
    dnp: bool = False
    # Courtyard extents in the unrotated footprint frame
    xmin: float = -0.5
    xmax: float = 0.5
    ymin: float = -0.5
    ymax: float = 0.5
    pads: List[Pad] = field(default_factory=list)
    fields: Dict[str, str] = field(default_factory=dict)
    sheet: str = ""                      # hierarchical sheet path/name, "" or "/" for root
    component_class: str = ""

    # ---- derived helpers -------------------------------------------------
    @property
    def prefix(self) -> str:
        m = re.match(r"[A-Za-z_#]+", self.ref)
        return m.group(0).upper() if m else ""

    @property
    def nets(self) -> List[str]:
        return [p.net for p in self.pads if p.net]

    @property
    def pin_count(self) -> int:
        return len({p.number for p in self.pads if p.number})

    def extents(self, rot: Optional[float] = None) -> Tuple[float, float, float, float]:
        """Courtyard bbox relative to the origin, for the given rotation."""
        rot = self.rot if rot is None else rot
        q = rot % 360.0
        if q == 0.0:
            return self.xmin, self.ymin, self.xmax, self.ymax
        if q == 90.0:
            return self.ymin, -self.xmax, self.ymax, -self.xmin
        if q == 180.0:
            return -self.xmax, -self.ymax, -self.xmin, -self.ymin
        if q == 270.0:
            return -self.ymax, self.xmin, -self.ymin, self.xmax
        pts = [rotate(px, py, rot) for px, py in (
            (self.xmin, self.ymin), (self.xmax, self.ymin),
            (self.xmin, self.ymax), (self.xmax, self.ymax))]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)

    def bbox(self, x=None, y=None, rot=None, margin=0.0):
        x = self.x if x is None else x
        y = self.y if y is None else y
        a, b, c, d = self.extents(rot)
        return x + a - margin, y + b - margin, x + c + margin, y + d + margin

    @property
    def area(self) -> float:
        return max(0.01, (self.xmax - self.xmin) * (self.ymax - self.ymin))

    def pad_pos(self, pad: Pad, x=None, y=None, rot=None) -> Tuple[float, float]:
        x = self.x if x is None else x
        y = self.y if y is None else y
        rot = self.rot if rot is None else rot
        dx, dy = rotate(pad.lx, pad.ly, rot)
        return x + dx, y + dy

    def to_json(self) -> dict:
        return {
            "ref": self.ref, "uid": self.uid, "value": self.value, "footprint": self.footprint,
            "x": round(self.x, 4), "y": round(self.y, 4), "rot": self.rot, "side": self.side,
            "locked": self.locked, "tht": self.tht, "dnp": self.dnp,
            "ext": [self.xmin, self.ymin, self.xmax, self.ymax],
            "pads": [[p.number, p.net, round(p.lx, 4), round(p.ly, 4), round(p.w, 3), round(p.h, 3)]
                     for p in self.pads],
            "fields": self.fields, "sheet": self.sheet, "cclass": self.component_class,
        }


@dataclass
class Region:
    """Axis-aligned rectangle in board coordinates."""
    x0: float
    y0: float
    x1: float
    y1: float
    name: str = ""
    source: str = ""     # sheet name / component class for KiCad placement rule areas

    def contains(self, bx0, by0, bx1, by1, tol=0.0) -> bool:
        return (bx0 >= self.x0 - tol and by0 >= self.y0 - tol and
                bx1 <= self.x1 + tol and by1 <= self.y1 + tol)

    def intersects(self, bx0, by0, bx1, by1) -> bool:
        return not (bx1 <= self.x0 or bx0 >= self.x1 or by1 <= self.y0 or by0 >= self.y1)

    def to_json(self):
        return {"x0": self.x0, "y0": self.y0, "x1": self.x1, "y1": self.y1,
                "name": self.name, "source": self.source}


@dataclass
class Board:
    name: str = "board"
    path: str = ""
    components: Dict[str, Component] = field(default_factory=dict)   # by ref
    outline: Optional[Region] = None
    outline_segments: List[Tuple[float, float, float, float]] = field(default_factory=list)
    keepouts: List[Region] = field(default_factory=list)
    placement_areas: List[Region] = field(default_factory=list)     # KiCad rule areas with placement
    netclass_of: Dict[str, str] = field(default_factory=dict)        # net -> class name
    netclasses: List[str] = field(default_factory=lambda: ["Default"])
    copper_layers: int = 2
    track_count: int = 0          # existing track segments, arcs and vias (board already routed?)
    zone_count: int = 0

    # --- connectivity -----------------------------------------------------
    def net_members(self) -> Dict[str, List[Tuple[str, Pad]]]:
        nets: Dict[str, List[Tuple[str, Pad]]] = {}
        for c in self.components.values():
            for p in c.pads:
                if p.net:
                    nets.setdefault(p.net, []).append((c.ref, p))
        return nets

    def netclass(self, net: str) -> str:
        return self.netclass_of.get(net, "Default")

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "components": [c.to_json() for c in self.components.values()],
            "outline": self.outline.to_json() if self.outline else None,
            "outline_segments": self.outline_segments,
            "keepouts": [k.to_json() for k in self.keepouts],
            "placement_areas": [k.to_json() for k in self.placement_areas],
            "netclass_of": self.netclass_of,
            "netclasses": self.netclasses,
        }

    def positions(self) -> Dict[str, Tuple[float, float, float]]:
        return {r: (c.x, c.y, c.rot) for r, c in self.components.items()}
