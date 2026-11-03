"""Placement checks: KiCad's own DRC (through kicad-cli) plus routability estimates.

Before routing, KiCad DRC can already catch placement problems: courtyard
overlaps, pad-to-pad clearance, copper-to-edge clearance, hole spacing and parts
in keep-out areas. Whether routing will succeed can only be estimated without an
autorouter, so the built-in checks add a routability test: crowded areas where
the wiring demand exceeds what the layers can carry.

Every violation names the footprints involved so the fixer knows what to move.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

from . import metrics as mt
from .model import Board

PLACEMENT_TYPES = {
    "courtyards_overlap", "clearance", "copper_edge_clearance", "edge_clearance", "hole_clearance",
    "hole_to_hole", "holes_co_located", "items_not_allowed", "solder_mask_bridge", "shorting_items",
    "keepout", "footprint",
}


@dataclass
class Violation:
    type: str
    refs: List[str]
    message: str
    x: float = 0.0
    y: float = 0.0
    source: str = "check"          # check | kicad
    severity: str = "error"

    def to_json(self):
        return asdict(self)


@dataclass
class Rules:
    clearance: Dict[str, float] = field(default_factory=dict)   # net class -> copper clearance
    track: Dict[str, float] = field(default_factory=dict)       # net class -> track width
    min_clearance: float = 0.2
    edge_clearance: float = 0.3
    layers: int = 2


def load_rules(pro_path: str, board: Board) -> Rules:
    r = Rules()
    try:
        with open(pro_path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return r
    ds = (d.get("board") or {}).get("design_settings") or {}
    rules = ds.get("rules") or {}
    r.min_clearance = float(rules.get("min_clearance", r.min_clearance) or r.min_clearance)
    r.edge_clearance = float(rules.get("min_copper_edge_clearance", r.edge_clearance) or r.edge_clearance)
    for c in (d.get("net_settings") or {}).get("classes", []):
        name = c.get("name", "Default")
        if "clearance" in c:
            r.clearance[name] = float(c["clearance"])
        if "track_width" in c:
            r.track[name] = float(c["track_width"])
    return r


# ----------------------------------------------------------------------------- built-in checks
def _pad_box(c, p, pos):
    x, y, rot = pos
    px, py = c.pad_pos(p, x, y, rot)
    a = (rot % 180)
    w, h = (p.h, p.w) if 45 < a < 135 else (p.w, p.h)
    if abs(rot % 90) > 1e-6:                       # odd angle: use the enclosing square
        w = h = math.hypot(p.w, p.h)
    return px - w / 2, py - h / 2, px + w / 2, py + h / 2


def _box_gap(a, b) -> float:
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def _seg_box_dist(seg, b) -> float:
    (x0, y0, x1, y1) = seg
    # sample the segment; boards are small, segments short
    L = math.hypot(x1 - x0, y1 - y0)
    n = max(2, int(L / 0.5) + 1)
    best = 1e9
    for i in range(n + 1):
        t = i / n
        px, py = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
        dx = max(b[0] - px, 0, px - b[2])
        dy = max(b[1] - py, 0, py - b[3])
        best = min(best, math.hypot(dx, dy))
    return best


def builtin_checks(board: Board, positions: Dict[str, Tuple[float, float, float]], rules: Rules,
                   only: Optional[set] = None) -> List[Violation]:
    """Checks that need no KiCad: overlaps, pad clearance, edge clearance, keep-outs, congestion."""
    pos = {r: positions.get(r, (c.x, c.y, c.rot)) for r, c in board.components.items()}
    out: List[Violation] = []

    def relevant(*refs):
        return only is None or any(r in only for r in refs)

    # courtyards
    for a, b in mt.overlaps(board, pos):
        if relevant(a, b):
            ca = board.components[a]
            out.append(Violation("courtyards_overlap", [a, b], f"Courtyards of {a} and {b} overlap",
                                 pos[a][0], pos[a][1]))
    # pad-to-pad clearance (different nets, different footprints, shared copper side)
    pads = []
    for r, c in board.components.items():
        for p in c.pads:
            if not p.smd and not p.net and not p.w:
                continue
            sides = ("F", "B") if not p.smd else (c.side,)
            pads.append((r, p, _pad_box(c, p, pos[r]), sides, board.netclass(p.net) if p.net else "Default"))
    cell = 2.0
    grid: Dict[tuple, list] = {}
    for i, (_, _, b, sides, _) in enumerate(pads):
        for s in sides:
            for gx in range(int(b[0] // cell), int(b[2] // cell) + 1):
                for gy in range(int(b[1] // cell), int(b[3] // cell) + 1):
                    grid.setdefault((s, gx, gy), []).append(i)
    seen = set()
    for key, idx in grid.items():
        for ii in range(len(idx)):
            for jj in range(ii + 1, len(idx)):
                i, j = idx[ii], idx[jj]
                if (i, j) in seen:
                    continue
                seen.add((i, j))
                ra, pa, ba, _, ca = pads[i]
                rb, pb, bb, _, cb = pads[j]
                if ra == rb or (pa.net and pa.net == pb.net) or not relevant(ra, rb):
                    continue
                need = max(rules.clearance.get(ca, rules.min_clearance), rules.clearance.get(cb, rules.min_clearance))
                g = _box_gap(ba, bb)
                if g < need - 1e-6:
                    out.append(Violation("clearance", [ra, rb],
                                         f"Pad {pa.number} of {ra} is {g:.2f} mm from pad {pb.number} of {rb} "
                                         f"(needs {need:.2f})", (ba[0] + ba[2]) / 2, (ba[1] + ba[3]) / 2))
    # copper to board edge
    if board.outline_segments:
        for r, p, b, _, _ in pads:
            if not relevant(r):
                continue
            d = min(_seg_box_dist(s, b) for s in board.outline_segments)
            o = board.outline
            inside = o is None or o.contains(*b)
            if d < rules.edge_clearance - 1e-6 or not inside:
                out.append(Violation("copper_edge_clearance", [r],
                                     f"Pad {p.number} of {r} is {d:.2f} mm from the board edge "
                                     f"(needs {rules.edge_clearance:.2f})", (b[0] + b[2]) / 2, (b[1] + b[3]) / 2))
                break
    # keep-outs
    for r, c in board.components.items():
        if not relevant(r):
            continue
        bb = c.bbox(*pos[r])
        for k in board.keepouts:
            if k.intersects(*bb):
                out.append(Violation("items_not_allowed", [r], f"{r} is inside keep-out '{k.name}'", pos[r][0], pos[r][1]))
    # routability: wiring demand above what the layers can carry
    cong = mt.congestion(board, pos, layers=max(2, rules.layers))
    grid_u = cong.get("grid")
    if grid_u:
        hot = []
        for j, row in enumerate(grid_u):
            for i, u in enumerate(row):
                if u > 1.0:
                    hot.append((cong["x0"] + (i + 0.5) * cong["cell"], cong["y0"] + (j + 0.5) * cong["cell"], u))
        if hot:
            hc = cong["cell"]
            for hx, hy, u in sorted(hot, key=lambda h: -h[2])[:12]:
                refs = [r for r, c in board.components.items()
                        if c.bbox(*pos[r])[0] <= hx + hc / 2 and c.bbox(*pos[r])[2] >= hx - hc / 2
                        and c.bbox(*pos[r])[1] <= hy + hc / 2 and c.bbox(*pos[r])[3] >= hy - hc / 2]
                refs = [r for r in refs if relevant(r)]
                if refs:
                    out.append(Violation("congestion", refs,
                                         f"Wiring demand {u:.0%} of capacity around {', '.join(refs[:4])}"
                                         f"{'…' if len(refs) > 4 else ''}: routing may not fit", hx, hy, "check",
                                         "warning"))
    return out


# ----------------------------------------------------------------------------- KiCad DRC
def find_kicad_cli() -> Optional[str]:
    cands = [shutil.which("kicad-cli"), shutil.which("kicad-cli.exe")]
    for base in (getattr(sys, "_base_executable", ""), sys.executable, sys.base_prefix, sys.prefix):
        if not base:
            continue
        d = base if os.path.isdir(base) else os.path.dirname(base)
        cands += [os.path.join(d, "kicad-cli.exe"), os.path.join(d, "kicad-cli"),
                  os.path.join(d, "bin", "kicad-cli.exe")]
    for v in ("10.0", "9.0", "11.0"):
        cands.append(rf"C:\Program Files\KiCad\{v}\bin\kicad-cli.exe")
    cands += ["/usr/bin/kicad-cli", "/usr/local/bin/kicad-cli",
              "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


_REF_PATTERNS = [re.compile(r"\bof ([^\s,]+)"), re.compile(r"^Footprint ([^\s,]+)"),
                 re.compile(r"\bfootprint ([^\s,]+)", re.I)]


def _refs_in(desc: str, known) -> List[str]:
    out = []
    for rx in _REF_PATTERNS:
        for m in rx.finditer(desc):
            r = m.group(1).strip("'\"()[]")
            if r in known and r not in out:
                out.append(r)
    return out


def parse_report(report: dict, known_refs) -> List[Violation]:
    out = []
    for v in report.get("violations", []):
        typ = v.get("type", "")
        if typ not in PLACEMENT_TYPES:
            continue
        items = v.get("items", [])
        descs = [it.get("description", "") for it in items]
        # moves break existing tracks, and zone fills stay where they were until refilled;
        # neither is a placement problem
        if any(re.match(r"(Track|Via|Arc|Zone|Filled zone|Copper zone|Rule area)\b", d, re.I) for d in descs):
            continue
        refs = []
        for d in descs:
            for r in _refs_in(d, known_refs):
                if r not in refs:
                    refs.append(r)
        p = (items[0].get("pos") or {}) if items else {}
        msg = v.get("description", typ)
        if descs:
            msg += ": " + " / ".join(descs[:2])
        out.append(Violation(typ, refs, msg, float(p.get("x", 0) or 0), float(p.get("y", 0) or 0), "kicad",
                             v.get("severity", "error")))
    return out


def kicad_drc(board_text: str, project_file: str, known_refs, cli: Optional[str] = None,
              timeout: int = 180, keep_report: str = "") -> Tuple[List[Violation], str]:
    """Run kicad-cli DRC on board_text (already holding the proposal). Returns (violations, note)."""
    cli = cli or find_kicad_cli()
    if not cli:
        return [], "kicad-cli not found; used the built-in checks only"
    tmp = tempfile.mkdtemp(prefix="fpm_drc_")
    stem = "fpm_check"
    pcb = os.path.join(tmp, stem + ".kicad_pcb")
    with open(pcb, "w", encoding="utf-8") as f:
        f.write(board_text)
    if project_file and os.path.exists(project_file):
        shutil.copy(project_file, os.path.join(tmp, stem + ".kicad_pro"))
        dru = os.path.splitext(project_file)[0] + ".kicad_dru"
        if os.path.exists(dru):
            shutil.copy(dru, os.path.join(tmp, stem + ".kicad_dru"))
    rep = os.path.join(tmp, "report.json")
    flags = 0x08000000 if os.name == "nt" else 0         # no console window flash
    attempts = [
        [cli, "pcb", "drc", "--format", "json", "--severity-error", "--severity-warning", "--output", rep, pcb],
        [cli, "pcb", "drc", "--format", "json", "--output", rep, pcb],
    ]
    err = ""
    for cmd in attempts:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=flags)
            err = (res.stderr or res.stdout or "").strip()[-400:]
        except Exception as e:                            # pragma: no cover - depends on the machine
            err = str(e)
            continue
        if os.path.exists(rep):
            break
    if not os.path.exists(rep):
        return [], f"KiCad DRC could not run: {err or 'no report produced'}"
    try:
        with open(rep, encoding="utf-8") as f:
            report = json.load(f)
    except Exception as e:
        return [], f"Could not read the DRC report: {e}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    vs = parse_report(report, set(known_refs))
    if keep_report:
        try:
            with open(keep_report, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=1)
        except OSError:
            pass
    total = len(report.get("violations", []))
    return vs, (f"KiCad DRC ({os.path.basename(cli)}): {len(vs)} placement violation(s) "
                f"({total - len(vs)} ignored: old tracks, zone fills, silkscreen, text)")
