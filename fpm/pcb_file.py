"""Load a .kicad_pcb file (plus its .kicad_pro net classes) into the board model.

Used for offline analysis, testing and previews without a running KiCad.
Live placement in KiCad goes through kicad_ipc.py instead.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re

from . import sexpr
from .sexpr import child, children, num
from .model import Board, Component, Pad, Region

COURTYARD = {"F.CrtYd", "B.CrtYd"}


def _layer(node) -> str:
    lay = child(node, "layer")
    return str(lay[1]) if lay and len(lay) > 1 else ""


def _xy(node, name):
    n = child(node, name)
    if n and len(n) >= 3:
        return num(n[1]), num(n[2])
    return None


def _shape_points(node):
    """Return the characteristic points of a graphic primitive."""
    h = node[0]
    pts = []
    if h.endswith("_poly"):
        p = child(node, "pts")
        if p:
            for xy in children(p, "xy"):
                pts.append((num(xy[1]), num(xy[2])))
    elif h.endswith("_circle"):
        c = _xy(node, "center")
        e = _xy(node, "end")
        if c and e:
            r = ((c[0] - e[0]) ** 2 + (c[1] - e[1]) ** 2) ** 0.5
            pts += [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)]
    else:
        for k in ("start", "mid", "end"):
            v = _xy(node, k)
            if v:
                pts.append(v)
    return pts


def _bbox(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _net_name(pad_node) -> str:
    n = child(pad_node, "net")
    if not n or len(n) < 2:
        return ""
    return str(n[-1])           # (net 3 "GND") in KiCad <=9, (net "GND") in KiCad 10


def _load_footprint(fp) -> Component:
    at = child(fp, "at")
    x, y = num(at[1]), num(at[2])
    rot = num(at[3]) if len(at) > 3 else 0.0
    side = "B" if _layer(fp).startswith("B.") else "F"
    locked = any(a == "locked" for a in fp[1:] if isinstance(a, str))
    lk = child(fp, "locked")
    if lk is not None and (len(lk) == 1 or str(lk[1]) == "yes"):
        locked = True
    uid_n = child(fp, "uuid") or child(fp, "tstamp")
    uid = str(uid_n[1]) if uid_n else ""

    fields = {}
    for p in children(fp, "property"):
        if len(p) >= 3:
            fields[str(p[1])] = str(p[2])
    # KiCad 5/6 style
    for t in children(fp, "fp_text"):
        if len(t) >= 3 and t[1] in ("reference", "value"):
            fields.setdefault("Reference" if t[1] == "reference" else "Value", str(t[2]))

    attr = child(fp, "attr")
    attrs = [str(a) for a in attr[1:]] if attr else []
    sheet_n = child(fp, "sheetname")
    cc = child(fp, "component_classes")
    cclass = ""
    if cc:
        cls = children(cc, "class")
        cclass = ",".join(str(c[1]) for c in cls if len(c) > 1)

    comp = Component(
        ref=fields.get("Reference", "?"), uid=uid, value=fields.get("Value", ""),
        footprint=str(fp[1]), x=x, y=y, rot=rot, side=side, locked=locked,
        tht="through_hole" in attrs, dnp="dnp" in attrs,
        fields={k: v for k, v in fields.items()
                if k not in ("Reference", "Value", "Datasheet", "Description", "Footprint")
                and not k.startswith("ki_")},
        sheet=str(sheet_n[1]) if sheet_n else "", component_class=cclass,
    )

    crt, allpts = [], []
    for g in fp[1:]:
        if isinstance(g, list) and g and g[0] in ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc"):
            pts = _shape_points(g)
            if _layer(g) in COURTYARD:
                crt += pts
            elif _layer(g).endswith(".Fab"):
                allpts += pts

    for pd in children(fp, "pad"):
        pat = child(pd, "at")
        size = child(pd, "size")
        typ = str(pd[2]) if len(pd) > 2 else "smd"
        if typ == "np_thru_hole":
            pass
        w, h = (num(size[1], 0.5), num(size[2], 0.5)) if size else (0.5, 0.5)
        pa = num(pat[3]) if pat is not None and len(pat) > 3 else 0.0
        # pad (at) angle in the file includes the footprint rotation
        local_rot = (pa - rot) % 180
        if 45 < local_rot < 135:
            w, h = h, w
        pad = Pad(number=str(pd[1]), net=_net_name(pd),
                  lx=num(pat[1]) if pat else 0.0, ly=num(pat[2]) if pat else 0.0,
                  w=w, h=h, smd=(typ in ("smd", "connect")))
        comp.pads.append(pad)
        if typ in ("thru_hole", "np_thru_hole"):
            comp.tht = True
        allpts += [(pad.lx - w / 2, pad.ly - h / 2), (pad.lx + w / 2, pad.ly + h / 2)]

    if crt:
        comp.xmin, comp.ymin, comp.xmax, comp.ymax = _bbox(crt)
        # a pad sticking out of the courtyard still occupies board space
        for pd in comp.pads:
            comp.xmin = min(comp.xmin, pd.lx - pd.w / 2)
            comp.xmax = max(comp.xmax, pd.lx + pd.w / 2)
            comp.ymin = min(comp.ymin, pd.ly - pd.h / 2)
            comp.ymax = max(comp.ymax, pd.ly + pd.h / 2)
    elif allpts:
        a, b, c, d = _bbox(allpts)
        comp.xmin, comp.ymin, comp.xmax, comp.ymax = a - 0.25, b - 0.25, c + 0.25, d + 0.25
    return comp


def _netclasses_from_project(pro_path: str, nets):
    mapping, classes = {}, ["Default"]
    if not os.path.exists(pro_path):
        return mapping, classes
    try:
        with open(pro_path, encoding="utf-8") as f:
            ns = json.load(f).get("net_settings", {})
    except Exception:
        return mapping, classes
    classes = [c.get("name", "Default") for c in ns.get("classes", [])] or ["Default"]
    pats = ns.get("netclass_patterns") or []
    assigns = ns.get("netclass_assignments") or {}
    for net in nets:
        if isinstance(assigns, dict) and net in assigns:
            v = assigns[net]
            mapping[net] = v[0] if isinstance(v, list) and v else v
            continue
        for p in pats:
            pat, cls = p.get("pattern", ""), p.get("netclass", "Default")
            ok = fnmatch.fnmatchcase(net, pat)
            if not ok:
                try:
                    ok = re.fullmatch(pat, net) is not None
                except re.error:
                    ok = False
            if ok:
                mapping[net] = cls
                break
    return mapping, classes


def load(path: str) -> Board:
    with open(path, encoding="utf-8") as f:
        root = sexpr.parse(f.read())
    board = Board(name=os.path.splitext(os.path.basename(path))[0], path=path)

    for fp in children(root, "footprint") + children(root, "module"):
        c = _load_footprint(fp)
        ref = c.ref
        n = 2
        while ref in board.components:          # duplicate refs (e.g. unannotated)
            ref = f"{c.ref}#{n}"
            n += 1
        c.ref = ref
        board.components[ref] = c

    pts = []
    for g in root[1:]:
        if isinstance(g, list) and g and g[0] in ("gr_line", "gr_rect", "gr_arc", "gr_circle", "gr_poly"):
            if _layer(g) == "Edge.Cuts":
                p = _shape_points(g)
                pts += p
                if g[0] == "gr_rect" and len(p) == 2:
                    (a, b), (c, d) = p
                    board.outline_segments += [(a, b, c, b), (c, b, c, d), (c, d, a, d), (a, d, a, b)]
                elif len(p) >= 2:
                    for i in range(len(p) - 1):
                        board.outline_segments.append((p[i][0], p[i][1], p[i + 1][0], p[i + 1][1]))
                    if g[0] == "gr_poly":
                        board.outline_segments.append((p[-1][0], p[-1][1], p[0][0], p[0][1]))
    if pts:
        board.outline = Region(*_bbox(pts), name="Edge.Cuts")

    for z in children(root, "zone"):
        poly = child(z, "polygon")
        if not poly:
            continue
        zp = [(num(xy[1]), num(xy[2])) for xy in children(child(poly, "pts") or [], "xy")]
        if not zp:
            continue
        nm = child(z, "name")
        name = str(nm[1]) if nm else ""
        ko = child(z, "keepout")
        if ko and any(isinstance(k, list) and k[0] == "footprints" and str(k[1]) == "not_allowed" for k in ko[1:]):
            board.keepouts.append(Region(*_bbox(zp), name=name or "keepout"))
        pl = child(z, "placement")
        if pl:
            en = child(pl, "enabled")
            if en and str(en[1]) == "yes":
                src = child(pl, "sheetname") or child(pl, "component_class") or child(pl, "group")
                board.placement_areas.append(Region(*_bbox(zp), name=name,
                                                    source=str(src[1]) if src else ""))

    lay = child(root, "layers")
    if lay:
        cu = [l for l in lay[1:] if isinstance(l, list) and len(l) > 1 and str(l[1]).endswith(".Cu")]
        board.copper_layers = max(1, len(cu))
    board.track_count = len(children(root, "segment")) + len(children(root, "arc")) + len(children(root, "via"))
    board.zone_count = len(children(root, "zone"))

    nets = {p.net for c in board.components.values() for p in c.pads if p.net}
    pro = os.path.splitext(path)[0] + ".kicad_pro"
    board.netclass_of, board.netclasses = _netclasses_from_project(pro, nets)
    return board
