"""Net classification and net-class-aware placement priorities.

A net class is NOT used to group components. It only tells the optimizer how
much a net's length matters, how much routing room its parts need, and what it
should be kept away from.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict, field
from typing import Dict, Optional

GROUND_RE = re.compile(r"^(/.*/)?(A|D|P|S|CHASSIS_|EARTH_)?(GND|VSS|VEE|GROUND|0V|EARTH|COM)([A-Z0-9_]*)?$|^0$", re.I)
POWER_RE = re.compile(
    r"^(/.*/)?(\+|-)?\d+(\.\d+)?V\d*[A-Z0-9_]*$"         # +3V3, +5V, 12V, -12V
    r"|^(/.*/)?\+.*"                                      # anything starting with +
    r"|^(/.*/)?(V(CC|DD|BUS|IN|BAT|SYS|MOT|M|REF|IO|CORE|ANA|DIG|USB|AA|PP|S|LED)[A-Z0-9_]*)$"
    r"|^(/.*/)?(AVCC|AVDD|DVDD|DVCC|PVDD|VBATT|BAT\+|VPWR|PWR_?IN|V\+|V-)$",
    re.I)
DIFF_RE = re.compile(r"^(.*?)(_?)(P|N|\+|-|DP|DM|D\+|D-)$", re.I)


# Nets whose length matters more than their class says, recognised by name.
CRITICAL_NAMES = [
    (re.compile(r"(^|[/_\-])(RF|ANT|ANTENNA)\d*($|[/_\-\d])|_ANT|ANT_|RF_|_RF", re.I), 6.0),
    (re.compile(r"XTAL|XC\d|XL\d|XIN|XOUT|OSC_?(IN|OUT)|CLK_?(IN|OUT)?$|HSE|LSE", re.I), 3.0),
    (re.compile(r"(^|[/_])SW($|[/_\d])|_SW$|^SW_|(^|[/_])FB($|[/_\d])|_FB$|BOOT$|_BST$|LX$", re.I), 2.0),
]


def critical_factor(net: str) -> float:
    for rx, f in CRITICAL_NAMES:
        if rx.search(net):
            return f
    return 1.0


def net_kind(net: str) -> str:
    """'gnd', 'pwr' or 'sig'."""
    base = net.split("/")[-1] if "/" in net else net
    if GROUND_RE.match(net) or GROUND_RE.match(base):
        return "gnd"
    if POWER_RE.match(net) or POWER_RE.match(base):
        return "pwr"
    return "sig"


def diff_partner(net: str, all_nets) -> Optional[str]:
    """Return the other half of a differential pair, if present."""
    upper = {n.upper(): n for n in all_nets}
    u = net.upper()
    for a, b in (("_DP", "_DM"), ("D+", "D-"), ("DP", "DM"), ("_P", "_N"), ("+", "-"), ("P", "N")):
        for x, y in ((a, b), (b, a)):
            if u.endswith(x):
                cand = upper.get(u[: -len(x)] + y)
                if cand and cand != net:
                    return cand
    return None


@dataclass
class NetClassRule:
    weight: float = 1.0          # importance of short connections (multiplies wire length)
    spacing: float = 0.3         # extra routing room (mm) around parts carrying this class
    noisy: bool = False          # switching / high-current class
    sensitive: bool = False      # analog / RF / clock class that should stay away from noisy ones
    pair_weight: float = 2.0     # extra pull keeping differential pair / bus pads together
    max_length: float = 0.0      # optional per-connection target (mm), 0 = none

    def to_json(self):
        return asdict(self)


DEFAULT_RULES: Dict[str, NetClassRule] = {
    "Default": NetClassRule(),
}

# Name-based starting points for common class names. Users edit these in the UI.
_SUGGESTED = [
    (re.compile(r"power|pwr|supply|vin|motor|high.?current|hc", re.I),
     NetClassRule(weight=1.5, spacing=0.6, noisy=True)),
    (re.compile(r"usb|diff|hdmi|lvds|eth|can|rs485|pcie|mipi", re.I),
     NetClassRule(weight=2.5, spacing=0.4, pair_weight=4.0)),
    (re.compile(r"clk|clock|xtal|osc|rf|ant|analog|audio|sense|adc", re.I),
     NetClassRule(weight=3.0, spacing=0.4, sensitive=True, max_length=15.0)),
    (re.compile(r"switch|sw|gate|pwm", re.I),
     NetClassRule(weight=3.0, spacing=0.5, noisy=True, max_length=10.0)),
]


def suggested_rule(netclass: str) -> NetClassRule:
    for rx, rule in _SUGGESTED:
        if rx.search(netclass):
            return NetClassRule(**asdict(rule))
    return NetClassRule()


@dataclass
class NetRules:
    rules: Dict[str, NetClassRule] = field(default_factory=dict)
    # shared ground / power handling
    gnd_weight: float = 0.02        # whole-net pull of a ground net (kept tiny on purpose)
    pwr_weight: float = 0.05        # whole-net pull of a power rail
    decap_weight: float = 6.0       # pull of a decoupling cap's pads to its IC's supply/ground pins
    fanout_damping: bool = True     # divide a net's weight by (pins - 1)
    max_fanout: int = 40            # signal nets bigger than this are treated like rails

    def rule(self, netclass: str) -> NetClassRule:
        if netclass not in self.rules:
            self.rules[netclass] = suggested_rule(netclass)
        return self.rules[netclass]

    def to_json(self):
        return {
            "rules": {k: v.to_json() for k, v in self.rules.items()},
            "gnd_weight": self.gnd_weight, "pwr_weight": self.pwr_weight,
            "decap_weight": self.decap_weight, "fanout_damping": self.fanout_damping,
            "max_fanout": self.max_fanout,
        }

    @classmethod
    def from_json(cls, d: dict) -> "NetRules":
        nr = cls()
        for k in ("gnd_weight", "pwr_weight", "decap_weight", "fanout_damping", "max_fanout"):
            if k in d:
                setattr(nr, k, type(getattr(nr, k))(d[k]))
        for name, r in (d.get("rules") or {}).items():
            base = NetClassRule()
            for k, v in r.items():
                if hasattr(base, k):
                    setattr(base, k, type(getattr(base, k))(v))
            nr.rules[name] = base
        return nr


def net_weights(board, rules: NetRules) -> Dict[str, float]:
    """Effective wire-length weight per net."""
    members = board.net_members()
    out = {}
    for net, pins in members.items():
        n = len(pins)
        if n < 2:
            continue
        kind = net_kind(net)
        rule = rules.rule(board.netclass(net))
        if kind == "gnd":
            w = rules.gnd_weight
        elif kind == "pwr" or n > rules.max_fanout:
            w = rules.pwr_weight * rule.weight
        else:
            w = rule.weight
            if rules.fanout_damping and n > 2:
                w = w / (n - 1) ** 0.5
        out[net] = w
    return out
