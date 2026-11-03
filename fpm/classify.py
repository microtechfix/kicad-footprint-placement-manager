"""Circuit-block identification.

Evidence is used in priority order:
  1. explicit assignments (CircuitBlock / PlaceNear fields, manual overrides)
  2. schematic hierarchy (sheet) and component classes
  3. connectivity patterns (decoupling caps, series parts, crystals, feedback dividers ...)
  4. heuristics on reference prefix, value, footprint and net names

Every assignment carries a confidence (0..1) and a short explanation; anything
below REVIEW_THRESHOLD is listed for review.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .model import Board, Component, Region
from .netrules import net_kind

REVIEW_THRESHOLD = 0.6

BLOCK_FIELD_KEYS = ("circuitblock", "circuit_block", "placementblock", "block")
NEAR_FIELD_KEYS = ("placenear", "place_near")
PRIO_FIELD_KEYS = ("placementpriority", "placement_priority")

PALETTE = ["#4f8cff", "#ff8a3d", "#2ec4a6", "#e45f9b", "#a77bff", "#f2c94c", "#5cc8ff",
           "#ff5d5d", "#8bd450", "#d98cff", "#3dd6d0", "#ffb36b", "#9aa7ff", "#f07fb5",
           "#6fdc8c", "#ffd36b"]

KIND_LABEL = {
    "mcu": "Microcontroller", "power": "Power supply", "motor": "Motor driver",
    "comm": "Communication", "analog": "Analog", "connector": "Connector",
    "power_in": "Power input", "ic": "IC", "discrete": "Discrete", "clock": "Clock",
    "mechanical": "Mechanical", "user": "Block", "sheet": "Sheet", "misc": "Unassigned",
}

KIND_RULES: List[Tuple[str, re.Pattern]] = [
    ("mcu", re.compile(r"stm32|atmega|attiny|atsamd?|samd\d|esp32|esp8266|rp2040|rp2350|pic\d|pic1[0-9]|pic24|dspic|"
                       r"nrf5|msp430|gd32|ch32|lpc\d|mk\d\d|efm32|cc2[56]|ra4m|r7f|xmega|mcu|micro.?controller", re.I)),
    ("motor", re.compile(r"drv8|a4988|a498|tmc2|tmc5|l298|l293|bts79|vnh|tb6612|tb67|uln200|ir21|irs2|"
                         r"mp6550|stspin|lv8|gate.?driver|h.?bridge|motor", re.I)),
    ("comm", re.compile(r"max3\d\d|max48|sn65|sn75|ch340|ch34|ft232|ft2232|cp210|tja10|mcp25|mcp26|"
                        r"w5500|enc28|lan87|ksz|rtl8|adm3|iso15|thvd|usblc|tusb|fe1\.1|max14|"
                        r"rs485|rs232|can.?trans|transceiver|phy|nrf24|sx12|rfm9", re.I)),
    ("power", re.compile(r"ams1117|lm1117|ld1117|78\d\d|79\d\d|lm317|lm2596|lm2576|mp1584|mp2|tps5|tps6|"
                         r"icl76|lt1054|max66|78[lm]\d\d|tps7|tlv7|ap21|ap63|ap73|ap22|xc62|ht7|mcp17|mic5|rt9|sy8|lt30|lt86|lmr|"
                         r"buck|boost|ldo|regulator|dc.?dc|xl40|xl60|mt36|sgm2|me62|aoz|tp4056|bq24|ip5", re.I)),
    ("analog", re.compile(r"lm358|lm324|tl07|tl08|ne5532|opa\d|ad8\d|mcp60|mcp6|lmv3|tlv2|ina\d|ads1|"
                          r"mcp3\d|max11|op.?amp|amplifier|comparator|lm393|lm339|ad7|pga|cs53|pcm18", re.I)),
    ("clock", re.compile(r"si5351|cdce|oscillator|tcxo|vcxo", re.I)),
]

CONNECTOR_PREFIX = {"J", "P", "CN", "CON", "X", "USB", "JP"}
IC_PREFIX = {"U", "IC", "M", "MOD", "A", "N"}
MECH_PREFIX = {"H", "MH", "FID", "LOGO", "MK", "REF", "G", "KIBUZZARD", "NT"}


@dataclass
class Assignment:
    ref: str
    block: str
    confidence: float
    evidence: str
    reason: str

    def to_json(self):
        return {"ref": self.ref, "block": self.block, "confidence": round(self.confidence, 2),
                "evidence": self.evidence, "reason": self.reason,
                "review": self.confidence < REVIEW_THRESHOLD}


@dataclass
class Block:
    id: str
    name: str
    kind: str
    anchor: str = ""
    members: List[str] = field(default_factory=list)
    color: str = "#888"
    frozen: bool = False             # keep its current arrangement untouched
    region: Optional[Region] = None  # user/KiCad placement restriction
    source: str = "connectivity"

    def to_json(self):
        return {"id": self.id, "name": self.name, "kind": self.kind, "anchor": self.anchor,
                "members": self.members, "color": self.color, "frozen": self.frozen,
                "region": self.region.to_json() if self.region else None, "source": self.source}

    @classmethod
    def from_json(cls, d):
        r = d.get("region")
        return cls(id=d["id"], name=d["name"], kind=d.get("kind", "user"), anchor=d.get("anchor", ""),
                   members=list(d.get("members", [])), color=d.get("color", "#888"),
                   frozen=bool(d.get("frozen")), source=d.get("source", "user"),
                   region=Region(**r) if r else None)


# ----------------------------------------------------------------------------
def field_value(c: Component, keys) -> str:
    for k, v in c.fields.items():
        if k.lower().replace(" ", "") in keys and v.strip():
            return v.strip()
    return ""


def role(c: Component) -> str:
    """Part type. Cached on the component (pads, value and footprint don't change after loading)."""
    r = c.__dict__.get("_role")
    if r is None:
        r = c.__dict__["_role"] = _role(c)
    return r


def _role(c: Component) -> str:
    p = c.prefix
    fp = c.footprint.lower()
    if not c.nets and (p in MECH_PREFIX or "mounting" in fp or "fiducial" in fp or "logo" in fp or not c.pads):
        return "mechanical"
    if p in ("Y", "XTAL", "XT", "Q") and re.search(r"crystal|xtal|resonator|oscillator", fp + " " + c.value, re.I):
        return "crystal"
    if p == "X" and re.search(r"crystal|xtal|resonator|oscillator", fp, re.I):
        return "crystal"
    if p in ("Y", "XTAL", "XT"):
        return "crystal"
    if p in CONNECTOR_PREFIX or re.search(r"connector|header|usb_|jst|terminal|pinheader|pinsocket|rj45|barrel|dc_jack|molex",
                                          fp, re.I):
        if p in ("JP",) and c.pin_count <= 3:
            return "passive"
        return "connector"
    if p in IC_PREFIX or (c.pin_count >= 6 and p not in ("RN", "SW", "D", "LED", "K", "Q", "T", "RV")):
        return "ic"
    if p in ("Q", "T") and c.pin_count >= 3:
        return "transistor"
    return "passive"


def ic_kind(c: Component, nets_of_ic: List[str]) -> str:
    text = f"{c.value} {c.footprint}"
    for kind, rx in KIND_RULES:
        if rx.search(text):
            return kind
    names = " ".join(nets_of_ic).upper()
    if re.search(r"\bFB\b|_FB|SW\b|/SW|VOUT|BOOT", names) and c.pin_count <= 10:
        return "power"
    if c.pin_count >= 28 and re.search(r"XTAL|OSC|NRST|RESET|SWDIO|SWCLK|BOOT0|PA\d|PB\d", names):
        return "mcu"
    return "ic"


def _on_board(board: Board, c: Component) -> bool:
    if not board.outline:
        return True
    o = board.outline
    return o.x0 <= c.x <= o.x1 and o.y0 <= c.y <= o.y1


def _dist(a: Component, b: Component) -> float:
    return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5


# ----------------------------------------------------------------------------
def _is_decap(c: Component) -> bool:
    if c.prefix not in ("C", "CP", "CE") or c.pin_count != 2:
        return False
    kinds = [net_kind(n) for n in c.nets]
    return "gnd" in kinds and "pwr" in kinds and "sig" not in kinds


def classify(board: Board, overrides: Optional[Dict[str, str]] = None,
             user_blocks: Optional[Dict[str, Block]] = None,
             schematic_groups: Optional[Dict[str, tuple]] = None):
    """Return (blocks: Dict[id, Block], assignments: Dict[ref, Assignment]).

    schematic_groups: {ref: (group id, name, confidence, reason)} from fpm.schematic.
    When given, it is the main grouping evidence after explicit assignments."""
    overrides = overrides or {}
    comps = board.components
    nets = board.net_members()
    roles = {r: role(c) for r, c in comps.items()}

    def fanout(n):
        return len(nets.get(n, ()))

    blocks: Dict[str, Block] = {}
    assign: Dict[str, Assignment] = {}

    def new_block(bid, name, kind, anchor="", source="connectivity"):
        if bid not in blocks:
            blocks[bid] = Block(id=bid, name=name, kind=kind, anchor=anchor, source=source)
        return blocks[bid]

    def put(ref, bid, conf, evidence, reason):
        assign[ref] = Assignment(ref, bid, conf, evidence, reason)

    # ---- 1. explicit: manual overrides and CircuitBlock fields -----------
    deferred = {}
    for ref, c in comps.items():
        if ref in overrides:
            bid = overrides[ref]
            if user_blocks and bid in user_blocks:
                ub = user_blocks[bid]
                new_block(bid, ub.name, ub.kind, ub.anchor, "user")
                put(ref, bid, 1.0, "manual", "Assigned manually in the review step")
            else:
                deferred[ref] = bid          # target is a detected block: resolve at the end
            continue
        fv = field_value(c, BLOCK_FIELD_KEYS)
        if fv:
            bid = "f:" + fv
            new_block(bid, fv, "user", source="field")
            put(ref, bid, 1.0, "field", f"CircuitBlock field = '{fv}'")

    # PlaceNear: follow the named part into its block (resolved after anchors exist)
    place_near = {}
    for ref, c in comps.items():
        pn = field_value(c, NEAR_FIELD_KEYS)
        if pn and ref not in assign:
            place_near[ref] = pn.split(":")[0].strip()

    # ---- 1b. schematic grouping (drawn boxes, sheets, layout) ------------
    if schematic_groups:
        sg = defaultdict(list)
        for ref, (gid, gname, gconf, why) in schematic_groups.items():
            if ref in comps and ref not in assign and ref not in deferred:
                sg[gid].append(ref)
        names_seen = defaultdict(int)
        for gid, refs in sg.items():
            gname, gconf, why = schematic_groups[refs[0]][1:]
            if len(refs) < 2 and not why.startswith("inside the drawn box"):
                continue                       # lone symbol: let connectivity decide
            ics = [r for r in refs if roles[r] == "ic"]
            decaps = [r for r in refs if _is_decap(comps[r])]
            if len(decaps) == len(refs):
                continue                       # a row of decoupling caps: attach each to its IC
            rails_of_ics = {n for r in ics for n in comps[r].nets}
            keep = [r for r in refs if not (r in decaps and not (set(comps[r].nets) & rails_of_ics))]
            if not keep or (len(keep) < 2 and not why.startswith("inside the drawn box")):
                continue
            cand = ics or [r for r in keep if roles[r] == "connector"] or keep
            anchor = max(cand, key=lambda r: (comps[r].pin_count, comps[r].area))
            if roles[anchor] == "ic":
                kind = ic_kind(comps[anchor], comps[anchor].nets)
            elif roles[anchor] == "connector":
                kind = "connector"
            elif all(roles[r] == "mechanical" for r in keep):
                kind = "mechanical"
            else:
                kind = "discrete"
            label = KIND_LABEL.get(kind, "IC")
            auto = ("Mechanical / fixtures" if kind == "mechanical" else
                    f"{label} · {anchor}" + (f" ({comps[anchor].value})" if comps[anchor].value and kind != "connector" else ""))
            name = gname or auto
            names_seen[name] += 1
            bid = "s:" + gid
            new_block(bid, name, kind, anchor=anchor, source="schematic")
            for r in keep:
                put(r, bid, gconf, "schematic", why[0].upper() + why[1:])
        # make repeated names (several areas of one sheet) distinguishable
        for b in blocks.values():
            if b.source == "schematic" and names_seen[b.name] > 1:
                b.name = f"{b.name} · {b.anchor}"

    # ---- 2. hierarchy partitions -----------------------------------------
    def partition_of(c: Component) -> str:
        if c.component_class:
            return "class:" + c.component_class
        s = (c.sheet or "").strip()
        if s in ("", "/", "Root", "Racine") or s.lower() == "root":
            return ""
        return "sheet:" + s

    partitions = {r: partition_of(c) for r, c in comps.items()}
    distinct_parts = {p for p in partitions.values() if p}
    use_hierarchy = len(distinct_parts) >= 1 and len(distinct_parts) < max(2, len(comps) // 2)
    if schematic_groups:
        use_hierarchy = False          # the schematic step already used the sheets

    # ---- 3. anchors -------------------------------------------------------
    anchors = [r for r in comps if roles[r] in ("ic", "connector") and r not in assign]
    # ICs/connectors already placed in a schematic block still attract their passives
    targets = [r for r in comps if roles[r] in ("ic", "connector")
               and (r in anchors or (r in assign and assign[r].evidence == "schematic"))]
    anchor_kind = {}
    for r in targets:
        c = comps[r]
        if roles[r] == "connector":
            sig = [n for n in c.nets if net_kind(n) != "gnd"]
            only_power = sig and all(net_kind(n) == "pwr" for n in sig)
            anchor_kind[r] = "power_in" if only_power else "connector"
        else:
            anchor_kind[r] = ic_kind(c, c.nets)

    # Transistors with no IC around them can lead their own discrete block later.
    def block_id_for_anchor(r):
        return "a:" + r

    for r in anchors:
        c = comps[r]
        k = anchor_kind[r]
        label = KIND_LABEL.get(k, "IC")
        new_block(block_id_for_anchor(r), f"{label} · {r}" + (f" ({c.value})" if c.value and k != "connector" else ""),
                  k, anchor=r)
        part = partitions[r]
        why = f"{label} anchor ({roles[r]}"
        why += f", value '{c.value}')" if c.value else ")"
        put(r, block_id_for_anchor(r), 0.9 if k not in ("ic", "connector") else 0.8, "heuristic", why)

    anchor_pins = defaultdict(set)   # net -> anchors with a pin on it
    for r in targets:
        for n in comps[r].nets:
            anchor_pins[n].add(r)

    # ---- 4. connectivity attachment for everything else ------------------
    pending = [r for r in comps if r not in assign]
    scores: Dict[str, Dict[str, float]] = {}
    reasons: Dict[str, Dict[str, str]] = {}

    def add(ref, a, s, why):
        if partitions[ref] and use_hierarchy and partitions.get(a) and partitions[a] != partitions[ref]:
            s *= 0.15        # crossing a sheet boundary is unlikely to be the same block
        scores.setdefault(ref, {}).setdefault(a, 0.0)
        scores[ref][a] += s
        rs = reasons.setdefault(ref, {})
        if a not in rs or s > 0.5:
            rs[a] = why

    for ref in pending:
        c = comps[ref]
        rl = roles[ref]
        if rl == "mechanical":
            continue
        cn = c.nets
        kinds = {n: net_kind(n) for n in cn}
        has_gnd = any(k == "gnd" for k in kinds.values())
        pwr_nets = [n for n, k in kinds.items() if k == "pwr"]
        sig_nets = [n for n, k in kinds.items() if k == "sig"]

        # Decoupling / bulk capacitor: between a supply rail and ground.
        if c.prefix in ("C", "CP", "CE") and c.pin_count == 2 and has_gnd and pwr_nets and not sig_nets:
            rail = pwr_nets[0]
            ics = [a for a in anchor_pins.get(rail, ()) if roles[a] == "ic"]
            if len(ics) == 1:
                add(ref, ics[0], 4.0, f"Decoupling cap on {rail}; {ics[0]} is the only IC on that rail")
            elif ics:
                placed = [a for a in ics if _on_board(board, comps[a])] if _on_board(board, c) else []
                if placed:
                    near = min(placed, key=lambda a: _dist(c, comps[a]))
                    for a in ics:
                        add(ref, a, 2.5 if a == near else 0.6,
                            f"Decoupling cap on {rail}; nearest of {len(ics)} ICs on that rail")
                else:
                    # prefer regulators for bulk caps, otherwise spread equally
                    for a in ics:
                        bonus = 1.2 if anchor_kind.get(a) == "power" else 1.0
                        add(ref, a, bonus / len(ics) * 2,
                            f"Capacitor on shared rail {rail} ({len(ics)} ICs) — ambiguous")
            else:
                conns = [a for a in anchor_pins.get(rail, ())]
                for a in conns:
                    add(ref, a, 1.0, f"Capacitor on {rail}, next to power entry {a}")
            continue

        # Signal-net attachment: shared nets with anchors, weighted by fan-out.
        for n in sig_nets:
            fo = fanout(n)
            if fo < 2:
                continue
            w = 1.0 / (fo - 1) ** 0.7
            for a in anchor_pins.get(n, ()):
                bonus = 1.0
                ak = anchor_kind.get(a)
                if rl == "crystal" and roles[a] == "ic":
                    bonus = 3.0
                if ak in ("connector", "power_in") and c.prefix in ("D", "TVS", "F", "FB", "L") :
                    bonus = 1.6     # protection / filtering sits at the connector
                if re.search(r"FB|FEEDBACK|ADJ|SENSE", n, re.I) and ak == "power":
                    bonus = 2.5
                add(ref, a, w * bonus, f"Shares net {n} with {a}")
        # Power-path parts (inductor, diode, fuse) between rails -> regulator / power entry
        if not sig_nets and pwr_nets:
            for n in pwr_nets:
                for a in anchor_pins.get(n, ()):
                    ak = anchor_kind.get(a)
                    w = 0.8 if ak in ("power", "power_in") else 0.15
                    add(ref, a, w, f"On supply net {n} with {a}")

    # PlaceNear overrides scoring
    for ref, target in place_near.items():
        if target in comps:
            scores.setdefault(ref, {})[target] = 1e6
            reasons.setdefault(ref, {})[target] = f"PlaceNear field = {target}"

    def resolve_target(a):
        """An attachment target may be an anchor or an already-assigned part."""
        if a in assign:
            return assign[a].block
        return block_id_for_anchor(a) if a in anchors else None

    for ref, sc in scores.items():
        if ref in assign or not sc:
            continue
        ranked = sorted(sc.items(), key=lambda kv: -kv[1])
        best, bs = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        margin = (bs - second) / bs if bs > 0 else 0
        bid = resolve_target(best)
        if bid is None:
            continue
        if bs >= 1e6:
            put(ref, bid, 1.0, "field", reasons[ref][best])
            continue
        strength = min(1.0, bs / 1.5)
        conf = 0.35 + 0.55 * margin * (0.5 + 0.5 * strength)
        if partitions[ref] and use_hierarchy and partitions[ref] == partitions.get(best):
            conf = min(0.95, conf + 0.1)
        ev = "connectivity"
        why = reasons[ref][best]
        if len(ranked) > 1 and margin < 0.35:
            why += f"; also connected to {ranked[1][0]}"
        put(ref, bid, conf, ev, why)

    # ---- 5. propagate through passive chains (RC filters, dividers ...) ---
    for _ in range(4):
        changed = False
        for ref in comps:
            if ref in assign or roles[ref] == "mechanical":
                continue
            c = comps[ref]
            votes = defaultdict(float)
            via = {}
            for n in c.nets:
                if net_kind(n) != "sig" or fanout(n) < 2:
                    continue
                for (other, _p) in nets[n]:
                    if other != ref and other in assign:
                        w = 0.6 / (fanout(n) - 1) ** 0.5
                        votes[assign[other].block] += w * assign[other].confidence
                        via[assign[other].block] = (other, n)
            if votes:
                ranked = sorted(votes.items(), key=lambda kv: -kv[1])
                bid, bs = ranked[0]
                second = ranked[1][1] if len(ranked) > 1 else 0
                margin = (bs - second) / bs
                o, n = via[bid]
                put(ref, bid, 0.3 + 0.4 * margin, "connectivity",
                    f"Connected through {o} (net {n})")
                changed = True
        if not changed:
            break

    # ---- 6. leftovers: discrete clusters & mechanical ---------------------
    leftovers = [r for r in comps if r not in assign]
    seen = set()
    for r in leftovers:
        if r in seen:
            continue
        if roles[r] == "mechanical":
            new_block("mech", "Mechanical / fixtures", "mechanical", source="heuristic")
            put(r, "mech", 0.9, "heuristic", "No electrical connections (mounting hole, fiducial, logo)")
            seen.add(r)
            continue
        # BFS over signal nets among leftovers
        comp_set, stack = [], [r]
        seen.add(r)
        while stack:
            x = stack.pop()
            comp_set.append(x)
            for n in comps[x].nets:
                if net_kind(n) != "sig":
                    continue
                for (o, _p) in nets.get(n, ()):
                    if o in leftovers and o not in seen and roles[o] != "mechanical":
                        seen.add(o)
                        stack.append(o)
        lead = sorted(comp_set, key=lambda q: (roles[q] != "transistor", -comps[q].pin_count))[0]
        if len(comp_set) == 1 and not comps[lead].nets:
            continue
        bid = "d:" + lead
        kind = "discrete" if len(comp_set) > 1 else "misc"
        new_block(bid, (f"Discrete · {lead}" if len(comp_set) > 1 else "Unassigned"), kind, anchor=lead,
                  source="connectivity")
        if kind == "misc":
            bid = "misc"
            new_block("misc", "Unassigned", "misc", source="heuristic")
        for q in comp_set:
            put(q, bid, 0.45 if len(comp_set) > 1 else 0.2,
                "connectivity" if len(comp_set) > 1 else "heuristic",
                f"Connected group around {lead}, no IC nearby" if len(comp_set) > 1
                else "Only power/ground connections — no clear block")
    for r in comps:
        if r not in assign:
            new_block("misc", "Unassigned", "misc", source="heuristic")
            put(r, "misc", 0.1, "heuristic", "No usable evidence")

    # ---- 7. merge connector blocks into communication ICs ----------------
    for r in anchors:
        if anchor_kind[r] != "connector":
            continue
        cb = block_id_for_anchor(r)
        members_sig = set()
        for ref, a in assign.items():
            if a.block == cb:
                members_sig.update(n for n in comps[ref].nets if net_kind(n) == "sig")
        comm_ics = defaultdict(int)
        for n in members_sig:
            for a in anchor_pins.get(n, ()):
                if anchor_kind.get(a) == "comm":
                    comm_ics[a] += 1
        if comm_ics:
            ic = max(comm_ics, key=comm_ics.get)
            target = assign[ic].block
            if target != cb:
                for ref, a in list(assign.items()):
                    if a.block == cb:
                        a.block = target
                        a.reason += f"; connector merged into communication block of {ic}"
                blocks[target].name = f"Communication · {ic} + {r}"

    # ---- 8. hierarchy: a sheet with a single IC is one functional block ---
    if use_hierarchy:
        native = defaultdict(set)            # partition -> blocks anchored inside it
        for b in blocks.values():
            if b.anchor and partitions.get(b.anchor) and b.source not in ("user", "field"):
                native[partitions[b.anchor]].add(b.id)
        for part, bids in native.items():
            sheet = part.split(":", 1)[1]
            label = sheet.strip("/") or sheet
            ics = [b for b in bids if blocks[b].kind not in ("connector", "power_in", "discrete", "misc")]
            if len(ics) <= 1:
                keep = ics[0] if ics else sorted(bids)[0]
                b = blocks[keep]
                b.name = f"{label} ({KIND_LABEL.get(b.kind, b.kind)})"
                b.source = "sheet"
                for ref, a in assign.items():
                    if partitions[ref] == part and a.evidence not in ("manual", "field"):
                        if a.block != keep:
                            a.reason = f"Same schematic sheet as {b.anchor}; was: " + a.reason
                        a.block = keep
                        a.confidence = max(a.confidence, 0.85)
                        if not a.reason.startswith("Schematic sheet"):
                            a.reason = f"Schematic sheet '{sheet}'; " + a.reason
            else:
                for b in bids:
                    blocks[b].name = f"{label} / {blocks[b].name}"

    # ---- manual reassignments into detected blocks ------------------------
    for ref, bid in deferred.items():
        if bid not in blocks:
            new_block(bid, bid.split(":", 1)[-1], "user", source="user")
        put(ref, bid, 1.0, "manual", "Assigned manually in the review step")

    # ---- finalize ---------------------------------------------------------
    for b in blocks.values():
        b.members = []
    for ref, a in assign.items():
        blocks[a.block].members.append(ref)
    blocks = {k: v for k, v in blocks.items() if v.members}
    for i, b in enumerate(sorted(blocks.values(), key=lambda b: (b.kind == "misc", b.id))):
        b.color = PALETTE[i % len(PALETTE)]
        b.members.sort(key=_ref_key)
        if b.anchor and b.anchor not in b.members:
            b.anchor = b.members[0]
        # KiCad placement rule area for this sheet / class becomes the region
        for pa in board.placement_areas:
            src = pa.source.strip("/")
            if src and any(partitions.get(m, "").split(":", 1)[-1].strip("/") == src or
                           comps[m].component_class == pa.source for m in b.members):
                b.region = Region(pa.x0, pa.y0, pa.x1, pa.y1, name=pa.name or src, source="kicad-rule-area")
                break
    return blocks, assign


def _ref_key(r: str):
    m = re.match(r"([A-Za-z_#]*)(\d*)", r)
    return (m.group(1), int(m.group(2)) if m.group(2) else 0, r)


def signature(board: Board, block: Block) -> tuple:
    """Order-independent fingerprint used to find repeated channels."""
    comps = board.components
    items = sorted(("" if role(comps[m]) == "connector" else comps[m].value.upper(),
                    comps[m].footprint.split(":")[-1]) for m in block.members if m in comps)
    return tuple(items)


def repeated_groups(board: Board, blocks: Dict[str, Block], min_similarity: float = 0.75) -> List[List[str]]:
    """Blocks whose parts lists (value + footprint) are near-identical: repeated channels."""
    from collections import Counter
    cands = [b for b in blocks.values() if b.kind not in ("misc", "mechanical") and len(b.members) >= 2
             and b.anchor in board.components]
    sigs = {b.id: Counter(signature(board, b)) for b in cands}
    afp = {b.id: board.components[b.anchor].footprint for b in cands}
    groups: List[List[str]] = []
    used = set()
    for b in sorted(cands, key=lambda b: -len(b.members)):
        if b.id in used:
            continue
        grp = [b.id]
        for o in cands:
            if o.id == b.id or o.id in used or afp[o.id] != afp[b.id]:
                continue
            inter = sum((sigs[b.id] & sigs[o.id]).values())
            union = sum((sigs[b.id] | sigs[o.id]).values())
            if union and inter / union >= min_similarity:
                grp.append(o.id)
        if len(grp) > 1:
            used.update(grp)
            groups.append(sorted(grp, key=lambda x: blocks[x].name))
    return groups
