"""AI placement assistance.

Two independent sources produce the same kind of reviewable *suggestions*
(near / edge / zone / apart / master / name). Enabled suggestions become
placement hints for the normal placer, so geometry, legality and the DRC loop
stay deterministic; the AI only supplies design intent.

* Claude (online): the netlist, schematic pin names and blocks are summarised
  and sent to the Anthropic Messages API. Claude answers through a forced tool
  call with a structured placement plan.
* Learned (offline): every layout you teach it (``Learn from this board``)
  and every manual correction you make after applying a proposal is stored
  in ``~/.fpm/learned.json``: where each small part sits relative to the IC pin
  it serves, which parts live on the board edge, and where block kinds go.
"""
from __future__ import annotations

import json
import math
import os
import re
import statistics
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from typing import List, Optional

from .classify import role
from .model import Board, Component, norm_angle, unrotate
from .netrules import net_kind

CONFIG_DIR = os.environ.get("FPM_HOME") or os.path.join(os.path.expanduser("~"), ".fpm")
MODELS = ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"]
API_URL = "https://api.anthropic.com/v1/messages"
ZONES = {
    "top-left": (0.15, 0.15), "top": (0.5, 0.12), "top-right": (0.85, 0.15),
    "left": (0.12, 0.5), "center": (0.5, 0.5), "right": (0.88, 0.5),
    "bottom-left": (0.15, 0.85), "bottom": (0.5, 0.88), "bottom-right": (0.85, 0.85),
}


# ============================================================== user config
def _cfg_path():
    return os.path.join(CONFIG_DIR, "config.json")


def load_config() -> dict:
    try:
        with open(_cfg_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(_cfg_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1)


def api_key() -> str:
    return (load_config().get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()


# ============================================================== helpers
def _short_fp(fp: str) -> str:
    return fp.split(":")[-1] if fp else ""


def _norm_val(v: str) -> str:
    return re.sub(r"\s+", "", (v or "").upper())


def _small(c: Component) -> bool:
    return role(c) not in ("ic", "connector", "mechanical") and c.pin_count <= 8


def _is_master_like(c: Component) -> bool:
    return role(c) in ("ic", "connector") or c.pin_count >= 8


def master_link(board: Board, r: str, blocks=None, assign=None):
    """For a small part: (master ref, master pad, own pad, net) of the IC pin it serves, or None."""
    C = board.components
    c = C[r]
    members = getattr(board, "_fpm_members", None) or board.net_members()
    best = None
    for pad in c.pads:
        if not pad.net:
            continue
        kind = net_kind(pad.net)
        pins = members.get(pad.net, [])
        if kind == "gnd" or len(pins) > 30:
            continue
        for o, op in pins:
            if o == r or not _is_master_like(C[o]):
                continue
            same_block = bool(assign and r in assign and o in assign and assign[r].block == assign[o].block)
            score = (2 if kind == "sig" else 1) + (2 if same_block else 0) + (1 if len(pins) <= 3 else 0) + \
                0.001 * C[o].pin_count
            if best is None or score > best[0]:
                best = (score, o, op, pad, pad.net)
    return best[1:] if best else None


def _pin_label(pin_names, ref, num):
    return (pin_names.get(ref) or {}).get(num, "")


def _board_box(board: Board):
    o = board.outline
    if o:
        return o.x0, o.y0, o.x1, o.y1
    xs = [c.x for c in board.components.values()] or [0]
    ys = [c.y for c in board.components.values()] or [0]
    return min(xs), min(ys), max(xs), max(ys)


# ============================================================== suggestions
def suggestion(kind, data, text, reason, source, refs=()):
    sid = f"{source[0]}-{kind}-" + "-".join(str(x) for x in refs)[:60]
    return {"id": sid, "kind": kind, "data": data, "text": text, "reason": reason,
            "source": source, "refs": list(refs), "enabled": True}


def resolve_block(members: List[str], assign) -> Optional[str]:
    """Block id that holds most of the given parts (block ids change between analyses)."""
    cnt = Counter(assign[m].block for m in members if m in assign)
    return cnt.most_common(1)[0][0] if cnt else None


def to_hints(suggestions, assign, blocks):
    """Enabled suggestions -> (placer hints, {block: master ref}, {block: name})."""
    hints = {"near": [], "edge": [], "zones": {}, "apart": []}
    masters, names = {}, {}
    for s in suggestions:
        if not s.get("enabled", True):
            continue
        d, k = s["data"], s["kind"]
        if k == "near":
            hints["near"].append(d)
        elif k == "edge":
            hints["edge"].append(d)
        elif k == "zone":
            bid = resolve_block(d.get("block_refs", []), assign)
            if bid:
                hints["zones"][bid] = d["at"]
        elif k == "apart":
            a = resolve_block(d.get("a_refs", []), assign)
            b = resolve_block(d.get("b_refs", []), assign)
            if a and b and a != b:
                hints["apart"].append({"a": a, "b": b, "gap": d.get("gap", 3.0)})
        elif k == "master":
            bid = resolve_block(d.get("block_refs", []), assign)
            if bid and d.get("ref") in blocks.get(bid, type("", (), {"members": []})).members:
                masters[bid] = d["ref"]
        elif k == "name":
            bid = resolve_block(d.get("block_refs", []), assign)
            if bid and d.get("name"):
                names[bid] = d["name"]
    return hints, masters, names


# ============================================================== Claude
def describe(board: Board, blocks, assign, pin_names, fixed: set, max_net_pins=14) -> str:
    """Compact text description of the design for the model."""
    C = board.components
    x0, y0, x1, y1 = _board_box(board)
    lines = [f"BOARD {board.name}: {x1 - x0:.1f} x {y1 - y0:.1f} mm, {board.copper_layers} copper layers, "
             f"{len(C)} footprints. Coordinates: x right, y down, origin top-left of the board.",
             "", "BLOCKS (id | name | kind | master | members):"]
    for b in sorted(blocks.values(), key=lambda b: b.id):
        lines.append(f"{b.id} | {b.name} | {b.kind} | {b.anchor or '-'} | {' '.join(sorted(b.members))}")
    lines += ["", "PARTS (ref | value | footprint | pins | side | fixed position if fixed):"]
    for r in sorted(C):
        c = C[r]
        fx = ""
        if r in fixed:
            fx = f"FIXED at ({c.x - x0:.1f}, {c.y - y0:.1f})"
        lines.append(f"{r} | {c.value} | {_short_fp(c.footprint)} | {c.pin_count} | {c.side} | {fx}")
    lines += ["", "IC PINS (ref: number=name->net), ground pins omitted:"]
    for r in sorted(C):
        c = C[r]
        if not _is_master_like(c):
            continue
        items = []
        for p in sorted(c.pads, key=lambda p: (len(p.number), p.number)):
            if not p.net or net_kind(p.net) == "gnd":
                continue
            nm = _pin_label(pin_names, r, p.number)
            items.append(f"{p.number}{'=' + nm if nm else ''}->{p.net}")
        if items:
            lines.append(f"{r}: " + ", ".join(dict.fromkeys(items)))
    lines += ["", f"NETS (non-ground, up to {max_net_pins} pins; ref.pin):"]
    for net, pins in sorted(board.net_members().items()):
        if net_kind(net) == "gnd" or len(pins) < 2:
            continue
        if len(pins) > max_net_pins:
            lines.append(f"{net}: {len(pins)} pins (rail)")
            continue
        lines.append(f"{net}: " + " ".join(f"{r}.{p.number}" for r, p in pins))
    return "\n".join(lines)


PLAN_TOOL = {
    "name": "submit_placement_plan",
    "description": "Submit placement intent for the PCB. Geometry is solved by a separate optimizer; "
                   "give relationships, not coordinates.",
    "input_schema": {
        "type": "object",
        "properties": {
            "blocks": {"type": "array", "description": "One entry per block id you want to rename or re-master.",
                       "items": {"type": "object", "properties": {
                           "block_id": {"type": "string"}, "name": {"type": "string"},
                           "master": {"type": "string", "description": "ref of the part the block is built around"},
                           "function": {"type": "string"}},
                           "required": ["block_id"]}},
            "near": {"type": "array", "description": "Parts that must sit close to a specific pin of another part.",
                     "items": {"type": "object", "properties": {
                         "ref": {"type": "string"}, "target": {"type": "string"},
                         "pin": {"type": "string", "description": "pin NUMBER on target, optional"},
                         "max_mm": {"type": "number"}, "reason": {"type": "string"}},
                         "required": ["ref", "target", "reason"]}},
            "edge": {"type": "array", "description": "Parts that belong on the board edge (connectors, antennas, "
                                                     "switches, LEDs meant to be seen).",
                     "items": {"type": "object", "properties": {
                         "ref": {"type": "string"},
                         "side": {"type": "string", "enum": ["any", "left", "right", "top", "bottom"]},
                         "reason": {"type": "string"}}, "required": ["ref", "reason"]}},
            "zones": {"type": "array", "description": "Preferred board area for a block.",
                      "items": {"type": "object", "properties": {
                          "block_id": {"type": "string"},
                          "area": {"type": "string", "enum": list(ZONES)},
                          "reason": {"type": "string"}}, "required": ["block_id", "area", "reason"]}},
            "apart": {"type": "array", "description": "Blocks that must be kept apart (noise, RF, heat).",
                      "items": {"type": "object", "properties": {
                          "a": {"type": "string"}, "b": {"type": "string"}, "gap_mm": {"type": "number"},
                          "reason": {"type": "string"}}, "required": ["a", "b", "reason"]}},
            "notes": {"type": "string", "description": "Short advice for the designer (2-6 sentences)."},
        },
        "required": ["near", "edge", "zones", "apart", "notes"],
    },
}

SYSTEM = (
    "You are a senior PCB layout engineer. You receive a KiCad design summary and return placement intent "
    "through the submit_placement_plan tool. Rules: decoupling capacitors next to the supply pin they serve; "
    "crystals and their load caps within a few mm of the oscillator pins; RF matching parts in a short chain "
    "between the RF pin and the antenna/connector; switching regulator input cap, inductor, catch diode and "
    "feedback divider tight around the SW/VIN/FB pins; connectors, antennas, buttons and indicator LEDs on "
    "the board edge (keep fixed parts where they are); keep switching power and digital clocks apart from RF "
    "and analog audio. Only use refs, pin numbers and block ids that appear in the summary. Prefer fewer, "
    "high-value hints over listing every part; the optimizer already keeps connected parts short. "
    "Do not add near hints for ground connections."
)


def claude_plan(desc: str, key: str, model: str = MODELS[0], timeout: int = 240, log=None) -> dict:
    if not key:
        raise ValueError("No Anthropic API key. Enter it on the AI page (it is stored in ~/.fpm/config.json).")
    body = {
        "model": model, "max_tokens": 12000, "system": SYSTEM,
        "tools": [PLAN_TOOL], "tool_choice": {"type": "tool", "name": PLAN_TOOL["name"]},
        "messages": [{"role": "user", "content": "Design summary:\n\n" + desc}],
    }
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            msg = ""
        raise RuntimeError(f"Claude API error {e.code}: {msg or e.reason}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach the Claude API: {e.reason}") from None
    if log:
        u = out.get("usage", {})
        log(f"Claude ({model}) answered in {time.time() - t0:.0f}s; "
            f"{u.get('input_tokens', '?')} tokens in, {u.get('output_tokens', '?')} out")
    for blk in out.get("content", []):
        if blk.get("type") == "tool_use":
            return blk.get("input") or {}
    raise RuntimeError("Claude did not return a placement plan.")


def plan_to_suggestions(plan: dict, board: Board, blocks, fixed: set) -> List[dict]:
    C = board.components
    out = []
    src = "claude"
    for b in plan.get("blocks", []) or []:
        bl = blocks.get(b.get("block_id"))
        if not bl:
            continue
        refs = list(bl.members)
        if b.get("name") and b["name"] != bl.name:
            out.append(suggestion("name", {"block_refs": refs, "name": b["name"][:40]},
                                  f"Name block '{bl.name}' as '{b['name'][:40]}'", b.get("function", ""), src, [bl.id]))
        m = b.get("master")
        if m in bl.members and m != bl.anchor:
            out.append(suggestion("master", {"block_refs": refs, "ref": m},
                                  f"Build '{bl.name}' around {m} (instead of {bl.anchor or 'none'})",
                                  b.get("function", ""), src, [m]))
    for h in plan.get("near", []) or []:
        r, t = h.get("ref"), h.get("target")
        if r not in C or t not in C or r == t:
            continue
        pin = str(h.get("pin") or "")
        if pin and not any(p.number == pin for p in C[t].pads):
            pin = ""
        mx = float(h.get("max_mm") or 0) or 5.0
        w = 3.0 + min(6.0, 10.0 / max(1.0, mx))
        out.append(suggestion("near", {"ref": r, "target": t, "pin": pin, "weight": round(w, 2)},
                              f"Keep {r} within {mx:g} mm of {t}" + (f" pin {pin}" if pin else ""),
                              h.get("reason", ""), src, [r, t]))
    for h in plan.get("edge", []) or []:
        r = h.get("ref")
        if r not in C or r in fixed:
            continue
        side = h.get("side") or "any"
        out.append(suggestion("edge", {"ref": r, "side": side, "weight": 12.0},
                              f"Put {r} on the {'board edge' if side == 'any' else side + ' edge'}",
                              h.get("reason", ""), src, [r]))
    for h in plan.get("zones", []) or []:
        bl = blocks.get(h.get("block_id"))
        at = ZONES.get(h.get("area", ""))
        if bl and at:
            out.append(suggestion("zone", {"block_refs": list(bl.members), "at": list(at)},
                                  f"Place '{bl.name}' at the {h['area']} of the board", h.get("reason", ""), src,
                                  [bl.id]))
    for h in plan.get("apart", []) or []:
        a, b = blocks.get(h.get("a")), blocks.get(h.get("b"))
        if a and b and a.id != b.id:
            g = float(h.get("gap_mm") or 3.0)
            out.append(suggestion("apart", {"a_refs": list(a.members), "b_refs": list(b.members), "gap": g},
                                  f"Keep '{a.name}' and '{b.name}' at least {g:g} mm apart", h.get("reason", ""),
                                  src, [a.id, b.id]))
    return out


# ============================================================== learned (offline)
class Library:
    """Placement habits learned from your own layouts, stored per user."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or os.path.join(CONFIG_DIR, "learned.json")
        self.d = {"version": 1, "boards": {}, "rel": {}, "edge": {}, "zone": {}}
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("version") == 1:
                self.d.update(d)
        except (OSError, ValueError):
            pass

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.d, f)
        os.replace(tmp, self.path)

    def clear(self):
        self.d = {"version": 1, "boards": {}, "rel": {}, "edge": {}, "zone": {}}
        self.save()

    def stats(self) -> str:
        nb = len(self.d["boards"])
        nr = sum(len(v) for v in self.d["rel"].values())
        if not nb:
            return "Nothing learned yet. Open a board you laid out by hand and press 'Learn from this board'."
        return (f"Learned from {nb} layout(s): {nr} part-to-pin placements, {len(self.d['edge'])} edge habits, "
                f"{len(self.d['zone'])} block-area habits.")

    # ---- keys
    @staticmethod
    def _keys(board, r, link, pin_names):
        C = board.components
        p, (m, mpad, _ppad, _net) = C[r], link
        mc = C[m]
        pname = _pin_label(pin_names, m, mpad.number).upper()
        k = [f"x|{_norm_val(mc.value)}|{mpad.number}|{p.prefix}|{_norm_val(p.value)}",
             f"p|{_norm_val(mc.value)}|{mpad.number}|{p.prefix}"]
        if pname:
            for tok in re.split(r"[/ ,]", pname):
                if tok and not re.fullmatch(r"P\d+\.\d+|\d+", tok):
                    k.append(f"n|{tok}|{p.prefix}")
        return k

    @staticmethod
    def _sample(board, r, link, wt, tag):
        C = board.components
        p, (m, mpad, ppad, _net) = C[r], link
        mc = C[m]
        dx, dy = unrotate(p.x - mc.x, p.y - mc.y, mc.rot)
        if mc.side == "B":
            dx = -dx
        ax, ay = mc.pad_pos(mpad)
        bx, by = p.pad_pos(ppad)
        return [round(dx, 3), round(dy, 3), norm_angle(p.rot - mc.rot), round(math.hypot(ax - bx, ay - by), 3),
                wt, tag]

    def learn(self, board: Board, blocks, assign, pin_names, refs=None, weight=1.0, tag=None) -> int:
        C = board.components
        tag = tag or board.name
        rel = self.d["rel"]
        if refs is None:                                  # re-learning a whole board replaces its samples
            for k in list(rel):
                rel[k] = [s for s in rel[k] if s[5] != tag]
                if not rel[k]:
                    del rel[k]
        n = 0
        setattr(board, "_fpm_members", board.net_members())
        todo = [r for r in (refs if refs is not None else C) if r in C]
        for r in todo:
            if not _small(C[r]):
                continue
            link = master_link(board, r, blocks, assign)
            if not link:
                continue
            smp = self._sample(board, r, link, weight, tag)
            if smp[3] > 25:                               # not really "next to" anything: no habit to learn
                continue
            for k in self._keys(board, r, link, pin_names):
                lst = rel.setdefault(k, [])
                lst.append(smp)
                del lst[:-24]
            n += 1
        # edges
        x0, y0, x1, y1 = _board_box(board)
        if board.outline and refs is None:
            for r in C:
                c = C[r]
                if role(c) not in ("connector", "switch", "led") and c.prefix not in ("J", "P", "SW", "ANT", "AE", "D", "LED", "BT", "USB"):
                    continue
                bx = c.bbox()
                d = {"left": bx[0] - x0, "right": x1 - bx[2], "top": bx[1] - y0, "bottom": y1 - bx[3]}
                side = min(d, key=lambda k: d[k])
                at = d[side] < 1.5
                e = self.d["edge"].setdefault(f"{c.prefix}|{_short_fp(c.footprint)}", {"hit": 0, "n": 0, "side": {}})
                e["n"] += 1
                if at:
                    e["hit"] += 1
                    e["side"][side] = e["side"].get(side, 0) + 1
        # block areas
        if refs is None and x1 > x0 and y1 > y0:
            for b in blocks.values():
                ms = [m for m in b.members if m in C]
                if len(ms) < 3 or b.kind in ("misc", "mechanical", "user"):
                    continue
                fx = (sum(C[m].x for m in ms) / len(ms) - x0) / (x1 - x0)
                fy = (sum(C[m].y for m in ms) / len(ms) - y0) / (y1 - y0)
                z = self.d["zone"].setdefault(b.kind, [])
                z[:] = [q for q in z if q[2] != tag] + [[round(fx, 3), round(fy, 3), tag]]
        if refs is None:
            self.d["boards"][tag] = {"when": time.strftime("%Y-%m-%d %H:%M"), "parts": n}
        else:
            self.d["boards"].setdefault(tag, {"when": time.strftime("%Y-%m-%d %H:%M"), "parts": 0})
        self.save()
        return n

    def suggest(self, board: Board, blocks, assign, pin_names, movable: set) -> List[dict]:
        C = board.components
        rel = self.d["rel"]
        src = "learned"
        out = []
        setattr(board, "_fpm_members", board.net_members())
        # parts on this board that share a key compete for the learned samples
        exact_users = defaultdict(list)
        links = {}
        for r in sorted(movable):
            if r not in C or not _small(C[r]):
                continue
            link = master_link(board, r, blocks, assign)
            if not link:
                continue
            links[r] = link
            k = self._keys(board, r, link, pin_names)
            if k[0] in rel:
                exact_users[k[0]].append(r)
        for r, link in links.items():
            m, mpad, ppad, net = link
            keys = self._keys(board, r, link, pin_names)
            pname = _pin_label(pin_names, m, mpad.number)
            pin_txt = f"pin {mpad.number}" + (f" ({pname})" if pname else "")
            if keys[0] in rel:
                smp = sorted(rel[keys[0]], key=lambda s: s[3])
                users = exact_users[keys[0]]
                s = smp[users.index(r) % len(smp)]
                nb = len({q[5] for q in smp})
                w = min(8.0, 3.0 + 1.5 * len(smp)) * (1.5 if s[4] > 1 else 1.0)
                out.append(suggestion("near", {"ref": r, "target": m, "offset": [s[0], s[1]], "weight": round(w, 2)},
                                      f"Put {r} ({C[r].value}) where you usually put it beside {m} {pin_txt}",
                                      f"learned from {len(smp)} placement(s) on {nb} layout(s)"
                                      + (", including your manual corrections" if any(q[4] > 1 for q in smp) else ""),
                                      src, [r, m]))
                continue
            for k in keys[1:]:
                if k in rel and len(rel[k]) >= (1 if k.startswith("p|") else 2):
                    dist = statistics.median(q[3] for q in rel[k])
                    w = 3.0 + min(3.0, 6.0 / max(0.5, dist))
                    out.append(suggestion("near", {"ref": r, "target": m, "pin": mpad.number, "weight": round(w, 2)},
                                          f"Keep {r} about {dist:.1f} mm from {m} {pin_txt}",
                                          f"learned from {len(rel[k])} similar placement(s)", src, [r, m]))
                    break
        # edges
        for r in sorted(movable):
            c = C.get(r)
            if not c:
                continue
            e = self.d["edge"].get(f"{c.prefix}|{_short_fp(c.footprint)}")
            if e and e["n"] and e["hit"] / e["n"] >= 0.6:
                side = "any"
                if e["side"]:
                    s, cnt = max(e["side"].items(), key=lambda kv: kv[1])
                    if cnt / max(1, e["hit"]) >= 0.75 and e["hit"] >= 2:
                        side = s
                out.append(suggestion("edge", {"ref": r, "side": side, "weight": 12.0},
                                      f"Put {r} on the {'board edge' if side == 'any' else side + ' edge'}",
                                      f"you placed this footprint on the edge {e['hit']} of {e['n']} time(s)", src, [r]))
        # block areas
        for b in blocks.values():
            z = self.d["zone"].get(b.kind)
            if not z or len(z) < 2 or b.kind in ("misc", "mechanical", "user"):
                continue
            fx = statistics.mean(q[0] for q in z)
            fy = statistics.mean(q[1] for q in z)
            spread = max(statistics.pstdev([q[0] for q in z]), statistics.pstdev([q[1] for q in z]))
            if spread <= 0.18 and any(m in movable for m in b.members):
                out.append(suggestion("zone", {"block_refs": list(b.members), "at": [round(fx, 2), round(fy, 2)]},
                                      f"Place '{b.name}' around {fx * 100:.0f}% across, {fy * 100:.0f}% down",
                                      f"your {b.kind} blocks sat there on {len(z)} layouts", src, [b.id]))
        return out
