"""Read the project schematic and turn it into circuit-block evidence.

Designers group a schematic by function, so the schematic is the best record of
which parts belong together. For every sheet instance this module uses, in
order:

  1. drawn boxes: graphic rectangles and text boxes. Symbols inside the
     smallest box belong together, and the box is named after its text.
  2. hierarchical sheets: a sheet is a natural block. Sheets with several
     separated areas are split by layout.
  3. layout: symbols drawn close together form a cluster. Gaps of empty space
     separate clusters, and each cluster is named after the nearest heading text.

The result maps each reference to (group id, group name, confidence, reason).
"""
from __future__ import annotations

import math
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import sexpr
from .sexpr import child, children, num


@dataclass
class SchSymbol:
    ref: str
    lib_id: str
    value: str
    x: float
    y: float
    unit: int = 1
    on_board: bool = True
    bbox: Tuple[float, float, float, float] = (0, 0, 0, 0)
    pins: List[Tuple[float, float]] = field(default_factory=list)
    nets: set = field(default_factory=set)        # wire-network ids touching its pins
    pin_names: Dict[str, str] = field(default_factory=dict)   # pin number -> pin name (from the library)


@dataclass
class SchText:
    text: str
    x: float
    y: float
    size: float = 1.27
    bold: bool = False


@dataclass
class SchBox:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str = ""          # text_box contents

    def contains(self, x, y, tol=0.0):
        return self.x0 - tol <= x <= self.x1 + tol and self.y0 - tol <= y <= self.y1 + tol

    @property
    def area(self):
        return (self.x1 - self.x0) * (self.y1 - self.y0)


@dataclass
class SheetInstance:
    path: str               # instance path, e.g. /root/sheetuuid
    name: str               # human sheet name, "" for root
    file: str
    symbols: List[SchSymbol] = field(default_factory=list)
    texts: List[SchText] = field(default_factory=list)
    boxes: List[SchBox] = field(default_factory=list)
    wires: List[Tuple[float, float, float, float]] = field(default_factory=list)


@dataclass
class SchGroup:
    id: str
    name: str
    refs: List[str]
    source: str              # box | sheet | layout
    confidence: float
    sheet: str = ""


# ----------------------------------------------------------------------------- parsing
def _prop(node, name) -> str:
    for p in children(node, "property"):
        if len(p) >= 3 and str(p[1]) == name:
            return str(p[2])
    return ""


def _font(node) -> Tuple[float, bool]:
    eff = child(node, "effects")
    font = child(eff, "font") if eff else None
    if not font:
        return 1.27, False
    size = child(font, "size")
    bold = child(font, "bold")
    b = bool(bold) and (len(bold) == 1 or str(bold[1]) == "yes")
    if not b:
        b = any(isinstance(a, str) and a == "bold" for a in font[1:])
    return (num(size[1], 1.27) if size else 1.27), b


def _parse_file(path: str, cache: dict):
    if path not in cache:
        with open(path, encoding="utf-8") as f:
            cache[path] = sexpr.parse(f.read())
    return cache[path]


def _lib_geometry(root) -> dict:
    """lib_id -> {unit: (pins[(x,y)], pts[(x,y)])} in library coordinates (y up). Unit 0 = all units."""
    out = {}
    ls = child(root, "lib_symbols")
    if not ls:
        return out
    for s in children(ls, "symbol"):
        units = defaultdict(lambda: ([], []))
        for u in children(s, "symbol"):
            parts = str(u[1]).rsplit("_", 2)
            unit = int(parts[1]) if len(parts) == 3 and parts[1].isdigit() else 0
            pins, pts = units[unit]
            for g in u[1:]:
                if not isinstance(g, list) or not g:
                    continue
                if g[0] == "pin":
                    at = child(g, "at")
                    if at:
                        pins.append((num(at[1]), num(at[2])))
                        pts.append((num(at[1]), num(at[2])))
                elif g[0] in ("rectangle", "polyline", "circle", "arc", "bezier"):
                    for k in ("start", "end", "mid", "center"):
                        v = child(g, k)
                        if v:
                            pts.append((num(v[1]), num(v[2])))
                    pp = child(g, "pts")
                    if pp:
                        pts += [(num(xy[1]), num(xy[2])) for xy in children(pp, "xy")]
        out[str(s[1])] = dict(units)
    return out


def _lib_pin_names(root) -> dict:
    """lib_id -> {pin number: pin name} from the schematic's embedded library symbols."""
    out = {}
    ls = child(root, "lib_symbols")
    if not ls:
        return out
    for s in children(ls, "symbol"):
        names = {}
        for u in children(s, "symbol"):
            for g in u[1:]:
                if isinstance(g, list) and g and g[0] == "pin":
                    nm, nb = child(g, "name"), child(g, "number")
                    if nb and len(nb) > 1:
                        name = str(nm[1]) if nm and len(nm) > 1 else ""
                        if name and name != "~":
                            names[str(nb[1])] = name
        out[str(s[1])] = names
    return out


def _xf(lx, ly, x, y, rot, mirror):
    """Library point -> sheet point (library y is up; rotation as KiCad, mirror applied first)."""
    px, py = lx, -ly
    if mirror == "x":
        py = -py
    elif mirror == "y":
        px = -px
    a = math.radians(rot)
    c, s_ = round(math.cos(a), 6), round(math.sin(a), 6)
    return x + px * c + py * s_, y - px * s_ + py * c


def _wire_networks(si: "SheetInstance"):
    """Assign wire-network ids to every symbol from pins touching wires."""
    wires = si.wires
    parent = list(range(len(wires)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        a, b = find(a), find(b)
        if a != b:
            parent[a] = b

    cell = 10.0
    grid = defaultdict(list)
    for i, (x0, y0, x1, y1) in enumerate(wires):
        for gx in range(int(min(x0, x1) // cell), int(max(x0, x1) // cell) + 1):
            for gy in range(int(min(y0, y1) // cell), int(max(y0, y1) // cell) + 1):
                grid[(gx, gy)].append(i)

    def on_seg(px, py, w, tol=0.02):
        x0, y0, x1, y1 = w
        if px < min(x0, x1) - tol or px > max(x0, x1) + tol or py < min(y0, y1) - tol or py > max(y0, y1) + tol:
            return False
        dx, dy = x1 - x0, y1 - y0
        L = math.hypot(dx, dy)
        if L < 1e-9:
            return math.hypot(px - x0, py - y0) <= tol
        return abs((px - x0) * dy - (py - y0) * dx) / L <= tol

    def touching(px, py):
        return [i for i in grid.get((int(px // cell), int(py // cell)), ()) if on_seg(px, py, wires[i])]

    for i, (x0, y0, x1, y1) in enumerate(wires):
        for px, py in ((x0, y0), (x1, y1)):
            for j in touching(px, py):
                union(i, j)
    for sym in si.symbols:
        for px, py in sym.pins:
            for j in touching(px, py):
                sym.nets.add(find(j))
    # pins placed directly on another symbol's pin (no wire between them)
    pin_at = defaultdict(list)
    for k, sym in enumerate(si.symbols):
        for px, py in sym.pins:
            pin_at[(round(px, 2), round(py, 2))].append(k)
    nid = len(wires)
    for ks in pin_at.values():
        if len(ks) > 1:
            nid += 1
            for k in ks:
                si.symbols[k].nets.add(("p", nid))


def load(root_sch: str) -> List[SheetInstance]:
    """Walk the hierarchy from the root schematic and return every sheet instance."""
    cache: dict = {}
    geom_cache: dict = {}
    pin_cache: dict = {}
    out: List[SheetInstance] = []
    base = os.path.dirname(os.path.abspath(root_sch))
    root = _parse_file(root_sch, cache)
    ru = child(root, "uuid")
    root_path = "/" + str(ru[1]) if ru else ""

    def walk(node, file, inst_path, name, depth):
        if depth > 12:
            return
        si = SheetInstance(path=inst_path, name=name, file=file)
        geo = geom_cache.setdefault(file, _lib_geometry(node))
        pnames = pin_cache.setdefault(file, _lib_pin_names(node))
        for w in children(node, "wire"):
            pts = [(num(xy[1]), num(xy[2])) for xy in children(child(w, "pts") or [], "xy")]
            for a, b in zip(pts, pts[1:]):
                si.wires.append((a[0], a[1], b[0], b[1]))
        for s in children(node, "symbol"):
            if not child(s, "lib_id"):
                continue
            ref = ""
            unit = 1
            inst = child(s, "instances")
            if inst:
                for proj in children(inst, "project"):
                    for p in children(proj, "path"):
                        if str(p[1]) == inst_path or (not inst_path and len(p) > 1):
                            r = child(p, "reference")
                            u = child(p, "unit")
                            if r:
                                ref = str(r[1])
                                unit = int(num(u[1], 1)) if u else 1
            if not ref:
                ref = _prop(s, "Reference")
                u = child(s, "unit")
                unit = int(num(u[1], 1)) if u else 1
            if not ref or ref.startswith("#"):
                continue                     # power symbols, flags
            ob = child(s, "on_board")
            at = child(s, "at")
            sx, sy = num(at[1]), num(at[2])
            rot = num(at[3]) if len(at) > 3 else 0.0
            mr = child(s, "mirror")
            mirror = str(mr[1]) if mr else ""
            lid = str(child(s, "lib_id")[1])
            ln = child(s, "lib_name")
            g = geo.get(str(ln[1]) if ln else lid) or geo.get(lid) or {}
            pins, pts = [], []
            for uk, (up, ut) in g.items():
                if uk in (0, unit):
                    pins += [_xf(px, py, sx, sy, rot, mirror) for px, py in up]
                    pts += [_xf(px, py, sx, sy, rot, mirror) for px, py in ut]
            if not pts:
                pts = [(sx - 2.54, sy - 2.54), (sx + 2.54, sy + 2.54)]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            si.symbols.append(SchSymbol(
                ref=ref, lib_id=lid, value=_prop(s, "Value"),
                x=sx, y=sy, unit=unit, on_board=not (ob and str(ob[1]) == "no"),
                bbox=(min(xs), min(ys), max(xs), max(ys)), pins=pins,
                pin_names=pnames.get(str(ln[1]) if ln else lid) or pnames.get(lid) or {}))
        for t in children(node, "text"):
            at = child(t, "at")
            if at and len(t) > 1:
                size, bold = _font(t)
                si.texts.append(SchText(str(t[1]).replace("\\n", "\n"), num(at[1]), num(at[2]), size, bold))
        for r in children(node, "rectangle"):
            a, b = child(r, "start"), child(r, "end")
            if a and b:
                si.boxes.append(SchBox(min(num(a[1]), num(b[1])), min(num(a[2]), num(b[2])),
                                       max(num(a[1]), num(b[1])), max(num(a[2]), num(b[2]))))
        for tb in children(node, "text_box"):
            txt = str(tb[1]).replace("\\n", "\n") if len(tb) > 1 else ""
            at, sz = child(tb, "at"), child(tb, "size")
            a, b = child(tb, "start"), child(tb, "end")
            if at and sz:
                x0, y0 = num(at[1]), num(at[2])
                x1, y1 = x0 + num(sz[1]), y0 + num(sz[2])
            elif a and b:
                x0, y0, x1, y1 = num(a[1]), num(a[2]), num(b[1]), num(b[2])
            else:
                continue
            si.boxes.append(SchBox(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1), text=txt))
        _wire_networks(si)
        out.append(si)

        for sh in children(node, "sheet"):
            sname = _prop(sh, "Sheetname") or _prop(sh, "Sheet name")
            sfile = _prop(sh, "Sheetfile") or _prop(sh, "Sheet file")
            su = child(sh, "uuid")
            if not sfile or not su:
                continue
            p = os.path.join(base, sfile)
            if not os.path.exists(p):
                continue
            try:
                sub = _parse_file(p, cache)
            except Exception:
                continue
            walk(sub, p, f"{inst_path}/{su[1]}", (name + "/" if name else "") + sname, depth + 1)

    walk(root, root_sch, root_path, "", 0)
    return out


def find_root_schematic(board_path: str, project_dir: str = "") -> Optional[str]:
    """<board>.kicad_sch, else the schematic named after the .kicad_pro in the folder."""
    cands = []
    if board_path:
        cands.append(os.path.splitext(board_path)[0] + ".kicad_sch")
    d = project_dir or (os.path.dirname(board_path) if board_path else "")
    if d and os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if f.endswith(".kicad_pro"):
                cands.append(os.path.join(d, f[:-len(".kicad_pro")] + ".kicad_sch"))
    for c in cands:
        if os.path.exists(c):
            return c
    return None


# ----------------------------------------------------------------------------- grouping
_IC_PREFIX = re.compile(r"^(U|IC|M|MOD|A|N)\d", re.I)


def _is_heading(t: SchText, base_size: float) -> bool:
    s = t.text.strip()
    if not s or len(s) > 60 or "\n" in s.strip():
        return False
    if re.fullmatch(r"[A-Z0-9_+\-/.]{1,6}", s):        # pin-like notes: RTS, TXD, 3V3
        return False
    return t.bold or t.size >= base_size * 1.3 or len(s.split()) >= 2


def _box_title(box: SchBox, texts: List[SchText]) -> str:
    if box.text.strip():
        return box.text.strip().splitlines()[0][:50]
    inside = [t for t in texts if box.contains(t.x, t.y, tol=1.0) and t.text.strip()]
    if not inside:
        return ""
    # biggest / boldest, then top-most
    t = sorted(inside, key=lambda t: (-t.size, not t.bold, t.y, t.x))[0]
    return t.text.strip().splitlines()[0][:50]


def _gap(a, b) -> float:
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def _auto_eps(syms: List[SchSymbol]) -> float:
    """Typical spacing between neighbouring symbol outlines on this sheet."""
    if len(syms) < 3:
        return 8.0
    gaps = []
    for i, a in enumerate(syms):
        gaps.append(min(_gap(a.bbox, b.bbox) for j, b in enumerate(syms) if j != i))
    gaps.sort()
    med = gaps[len(gaps) // 2]
    return max(3.0, min(12.0, med * 2.2 + 2.0))


def _link_clusters(syms: List[SchSymbol], eps: float, rail_size: int) -> List[List[SchSymbol]]:
    """Symbols drawn within eps of each other, or wired together within 4*eps, share a cluster.
    Wire networks touching more than rail_size symbols are treated as rails and don't link."""
    n = len(syms)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    count = defaultdict(int)
    for sym in syms:
        for net in sym.nets:
            count[net] += 1
    local = [{net for net in sym.nets if count[net] <= rail_size} for sym in syms]
    for i in range(n):
        for j in range(i + 1, n):
            g = _gap(syms[i].bbox, syms[j].bbox)
            if g <= eps or (g <= 4 * eps and local[i] & local[j]):
                a, b = find(i), find(j)
                if a != b:
                    parent[a] = b
    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(syms[i])
    return list(groups.values())


def _layout_groups(syms: List[SchSymbol], eps: float, depth=0) -> List[List[SchSymbol]]:
    out = []
    for grp in _link_clusters(syms, eps, rail_size=8):
        ics = {s.ref for s in grp if _IC_PREFIX.match(s.ref)}
        # a very large cluster holding several ICs is probably several blocks drawn
        # close together: try a tighter split
        if depth < 2 and len(grp) > 30 and len(ics) > 1:
            sub = _layout_groups(grp, eps * 0.6, depth + 1)
            if len(sub) > 1:
                out.extend(sub)
                continue
        out.append(grp)
    return out


def _nearest_heading(grp: List[SchSymbol], texts: List[SchText], base: float) -> str:
    xs = [s.bbox[0] for s in grp] + [s.bbox[2] for s in grp]
    ys = [s.bbox[1] for s in grp] + [s.bbox[3] for s in grp]
    x0, x1, y0, y1 = min(xs) - 10, max(xs) + 10, min(ys) - 15, max(ys) + 5
    best, bd = "", 1e9
    cx, cy = sum(xs) / len(xs), min(ys)
    for t in texts:
        if not (x0 <= t.x <= x1 and y0 <= t.y <= y1) or not _is_heading(t, base):
            continue
        d = math.hypot(t.x - cx, (t.y - cy) * 1.5) - (8 if t.bold else 0) - 4 * (t.size - base)
        if d < bd:
            best, bd = t.text.strip().splitlines()[0][:50], d
    return best


_CONT = re.compile(r"\s*[\(\[]?\s*(cont(inued)?\.?|contd\.?)\s*[\)\]]?\s*$", re.I)


def _heading_sections(syms: List[SchSymbol], texts: List[SchText]) -> Optional[List[Tuple[str, List[SchSymbol]]]]:
    """Section-heading layout: a heading text with its symbols below it in the same column
    (typical of generated or tidy column-style schematics). '(cont.)' headings continue a section."""
    long_x = [t.x for t in texts if len(t.text.strip()) > 6 and "\n" not in t.text.strip()]
    heads = []
    for t in texts:
        s = t.text.strip()
        if not s or "\n" in s or len(s) > 60:
            continue
        # short all-caps notes (RTS, TXD) are pin labels unless they line up with other headings
        if re.fullmatch(r"[A-Z0-9_+\-/.]{1,6}", s) and not any(abs(t.x - x) < 2.0 for x in long_x):
            continue
        heads.append(t)
    if len(heads) < 2:
        return None
    groups: Dict[str, List[SchSymbol]] = {}
    order: List[str] = []
    assigned = 0
    for sym in syms:
        left = [h for h in heads if h.x <= sym.bbox[0] + 2.0 and h.y < sym.y]
        if not left:
            continue
        col_x = max(h.x for h in left)
        if sym.x - col_x > 250:                       # too far to belong to that column
            continue
        col = [h for h in left if abs(h.x - col_x) < 2.0]
        h = max(col, key=lambda h: h.y)
        name = _CONT.sub("", h.text.strip()).strip() or h.text.strip()
        if name not in groups:
            groups[name] = []
            order.append(name)
        groups[name].append(sym)
        assigned += 1
    if assigned < 0.8 * len(syms) or len(groups) < 2:
        return None
    return [(n, groups[n]) for n in order]


def group(sheets: List[SheetInstance]) -> Tuple[Dict[str, Tuple[str, str, float, str]], List[SchGroup], dict]:
    """Return ({ref: (gid, name, conf, reason)}, groups, stats)."""
    groups: List[SchGroup] = []
    stats = {"sheets": len(sheets), "boxes": 0, "layout": 0, "symbols": 0}
    n = 0
    for si in sheets:
        syms = [s for s in si.symbols if s.on_board]
        stats["symbols"] += len({s.ref for s in syms})
        if not syms:
            continue
        sizes = sorted(t.size for t in si.texts) or [1.27]
        base = sizes[len(sizes) // 2]
        sheet_label = si.name or "Root"
        # 1. drawn boxes (ignore tiny ones and page-sized frames)
        boxes = [b for b in si.boxes
                 if (b.x1 - b.x0) > 12 and (b.y1 - b.y0) > 12
                 and sum(1 for s in syms if b.contains(s.x, s.y)) < max(4, len(syms) * 0.9)]
        boxed = {}
        for s in syms:
            inside = [b for b in boxes if b.contains((s.bbox[0] + s.bbox[2]) / 2, (s.bbox[1] + s.bbox[3]) / 2)]
            if inside:
                boxed.setdefault(id(min(inside, key=lambda b: b.area)), []).append(s)
        box_by_id = {id(b): b for b in boxes}
        used = set()
        for bid, grp in boxed.items():
            b = box_by_id[bid]
            title = _box_title(b, si.texts) or f"Box {len(groups) + 1}"
            n += 1
            refs = sorted({s.ref for s in grp})
            used.update(refs)
            groups.append(SchGroup(f"sch{n}", (f"{si.name}/" if si.name else "") + title, refs, "box", 0.95,
                                   sheet_label))
            stats["boxes"] += 1
        rest = [s for s in syms if s.ref not in used]
        if not rest:
            continue
        # 2./3. sheet and layout
        eps = _auto_eps(rest)
        clusters = _layout_groups(rest, eps)
        singles = sum(1 for c in clusters if len({s.ref for s in c}) == 1)
        if singles > 0.5 * len({s.ref for s in rest}):
            # symbols aren't drawn in clusters (e.g. a generated, label-connected schematic):
            # fall back to section headings with their symbols listed below them
            sections = _heading_sections(rest, si.texts)
            if sections:
                for name, grp in sections:
                    n += 1
                    nm = f"{si.name}/{name}" if si.name else name
                    groups.append(SchGroup(f"sch{n}", nm, sorted({s.ref for s in grp}), "heading", 0.85,
                                           sheet_label))
                    stats["layout"] += 1
                stats["headings"] = stats.get("headings", 0) + len(sections)
                continue
        ics_total = {s.ref for s in rest if _IC_PREFIX.match(s.ref)}
        if si.name and (len(clusters) == 1 or len(ics_total) <= 1):
            n += 1
            groups.append(SchGroup(f"sch{n}", si.name, sorted({s.ref for s in rest}), "sheet", 0.9, sheet_label))
            continue
        for grp in clusters:
            refs = sorted({s.ref for s in grp})
            head = _nearest_heading(grp, si.texts, base)
            n += 1
            nm = head or ""
            if si.name:
                nm = f"{si.name}/{nm}" if nm else si.name
            conf = 0.8 if len(refs) > 1 else 0.45
            groups.append(SchGroup(f"sch{n}", nm, refs, "layout", conf, sheet_label))
            stats["layout"] += 1

    # a multi-unit symbol may appear in several groups: keep the group holding most units
    votes: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for g in groups:
        for r in g.refs:
            votes[r][g.id] += 1
    owner = {r: max(v, key=v.get) for r, v in votes.items()}
    out = {}
    for g in groups:
        g.refs = [r for r in g.refs if owner.get(r) == g.id]
        why = {"box": f"inside the drawn box '{g.name}' on the schematic",
               "heading": f"listed under the heading '{g.name}' on the schematic",
               "sheet": f"on schematic sheet '{g.name}'",
               "layout": "drawn together on the schematic" + (f" under '{g.name}'" if g.name else "")}[g.source]
        for r in g.refs:
            out[r] = (g.id, g.name, g.confidence, why)
    groups = [g for g in groups if g.refs]
    return out, groups, stats
