"""Session state for the reviewable workflow:
analyze -> review groups -> constraints -> preview -> apply (selected) -> undo.

User decisions (manual groups, names, locks, regions, rules, options) are saved
next to the board in <board>.fpm.json so they survive restarts and schematic
updates.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from typing import Dict, List, Optional, Tuple

from . import classify as cl
from . import metrics, placer, templates
from . import APP_NAME, __version__
from .model import Region, norm_angle
from .netrules import NetRules

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Session:
    def __init__(self, backend):
        self.backend = backend
        self.lock = threading.RLock()
        self.board = None
        self.blocks: Dict[str, cl.Block] = {}
        self.assign: Dict[str, cl.Assignment] = {}
        # persistent user decisions
        self.overrides: Dict[str, str] = {}           # ref -> block id
        self.user_blocks: Dict[str, cl.Block] = {}    # created / split blocks
        self.names: Dict[str, str] = {}               # block id -> custom name
        self.frozen: Dict[str, bool] = {}
        self.regions: Dict[str, Region] = {}
        self.locked: set = set()
        self.rules = NetRules()
        self.options = placer.PlaceOptions()
        self.group_source = "schematic"               # schematic | connectivity
        self.sch_info = {"file": "", "stats": {}, "error": ""}
        self.sch_groups = None
        self.pin_names: Dict[str, Dict[str, str]] = {}   # ref -> pin number -> pin name (schematic)
        self.violations: list = []
        self.ground_plan = None
        self.check_note = ""
        self.use_kicad_drc = True
        self.fix_rounds = 4
        # AI placement
        self.ai_source = "learned"                    # claude | learned | both
        self.ai_use = True                            # use enabled suggestions when generating proposals
        self.ai_learn_auto = True                     # learn from manual corrections after Apply
        self.ai_suggestions: List[dict] = []
        self.ai_notes = ""
        self.last_applied: Dict[str, list] = {}       # ref -> [applied x, y, rot, previous x, y, rot]
        self._hints_in_use: dict = {}
        # transient
        self.proposal: Dict[str, Tuple[float, float, float]] = {}
        self.proposal_blocks: Dict[str, list] = {}
        self.metrics_before: Optional[dict] = None
        self.metrics_after: Optional[dict] = None
        self.undo_stack: List[dict] = []
        self.logs: List[str] = []
        self.last_seen = time.time()
        self.log(f"{APP_NAME} {__version__} running from {PLUGIN_DIR}")

    # ------------------------------------------------------------ helpers
    def log(self, msg: str):
        line = time.strftime("%H:%M:%S ") + str(msg)
        self.logs.append(line)
        self.logs = self.logs[-300:]

    @property
    def settings_path(self) -> str:
        bf = self.backend.board_file() or os.path.join(self.backend.project_dir(), "board")
        return os.path.splitext(bf)[0] + ".fpm.json"

    @property
    def template_dir(self) -> str:
        return os.path.join(PLUGIN_DIR, "templates")

    def save(self):
        data = {
            "version": 1,
            "overrides": self.overrides,
            "user_blocks": {k: v.to_json() for k, v in self.user_blocks.items()},
            "names": self.names, "frozen": self.frozen,
            "regions": {k: v.to_json() for k, v in self.regions.items()},
            "locked": sorted(self.locked),
            "rules": self.rules.to_json(),
            "options": {k: v for k, v in self.options.to_json().items()
                        if k not in ("refs", "blocks", "hints", "extra_margin", "repair")},
            "group_source": self.group_source,
            "use_kicad_drc": self.use_kicad_drc, "fix_rounds": self.fix_rounds,
            "ai": {"source": self.ai_source, "use": self.ai_use, "learn_auto": self.ai_learn_auto,
                   "suggestions": self.ai_suggestions, "notes": self.ai_notes},
            "last_applied": self.last_applied,
        }
        path = self.settings_path
        try:
            # keep keys written by other versions / copies of the plugin
            try:
                with open(path, encoding="utf-8") as f:
                    old = json.load(f)
                if isinstance(old, dict):
                    data["options"] = {**(old.get("options") or {}), **data["options"]}
                    data = {**old, **data}
            except (OSError, ValueError):
                pass
            data["saved_by"] = f"{APP_NAME} {__version__}"
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, path)
        except OSError as e:
            self.log(f"Could not save settings: {e}")

    def load_settings(self):
        p = self.settings_path
        if not os.path.exists(p):
            return
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:
            self.log(f"Ignoring unreadable {p}: {e}")
            return
        self.overrides = dict(d.get("overrides", {}))
        self.user_blocks = {k: cl.Block.from_json(v) for k, v in d.get("user_blocks", {}).items()}
        self.names = dict(d.get("names", {}))
        self.frozen = {k: bool(v) for k, v in d.get("frozen", {}).items()}
        self.regions = {k: Region(**v) for k, v in d.get("regions", {}).items()}
        self.locked = set(d.get("locked", []))
        self.rules = NetRules.from_json(d.get("rules", {}))
        self.options = placer.PlaceOptions.from_json(d.get("options", {}))
        self.group_source = d.get("group_source", "schematic")
        self.use_kicad_drc = bool(d.get("use_kicad_drc", True))
        self.fix_rounds = int(d.get("fix_rounds", 4))
        a = d.get("ai") or {}
        self.ai_source = a.get("source", self.ai_source)
        self.ai_use = bool(a.get("use", True))
        self.ai_learn_auto = bool(a.get("learn_auto", True))
        self.ai_suggestions = list(a.get("suggestions", []))
        self.ai_notes = a.get("notes", "")
        self.last_applied = dict(d.get("last_applied", {}))
        self.log(f"Loaded saved decisions from {os.path.basename(p)}")

    # ------------------------------------------------------------ analyze
    def analyze(self, reload=True):
        with self.lock:
            if reload or self.board is None:
                self.board = self.backend.load()
                if not getattr(self, "_settings_loaded", False):
                    self.load_settings()
                    self._settings_loaded = True
                for nc in self.board.netclasses:
                    self.rules.rule(nc)
                for nc in set(self.board.netclass_of.values()):
                    self.rules.rule(nc)
            # drop overrides for parts that no longer exist
            self.overrides = {r: b for r, b in self.overrides.items() if r in self.board.components}
            self.locked &= set(self.board.components)
            if reload or self.sch_groups is None:
                self._read_schematic()
            groups = self.sch_groups if self.group_source == "schematic" else None
            self.blocks, self.assign = cl.classify(self.board, self.overrides, self.user_blocks, groups)
            for bid, b in self.blocks.items():
                if bid in self.names:
                    b.name = self.names[bid]
                b.frozen = self.frozen.get(bid, False)
                if bid in self.regions:
                    b.region = self.regions[bid]
            o = self.board.outline
            for b in self.blocks.values():
                r = b.region
                if r and o and not o.contains(r.x0, r.y0, r.x1, r.y1, tol=0.5):
                    self.log(f"Note: region of '{b.name}' extends past the board edge; the edge wins.")
            self._apply_ai_blocks()
            self.metrics_before = metrics.evaluate(self.board, self.rules, label="current")
            if reload:
                self._learn_corrections()
            if reload:
                self.proposal = {}
                self.metrics_after = None
            else:
                self.proposal = {r: v for r, v in self.proposal.items() if r in self.board.components}
                self._eval() if self.proposal else None
            n_rev = sum(1 for a in self.assign.values() if a.confidence < cl.REVIEW_THRESHOLD)
            self.log(f"Analyzed {self.board.name}: {len(self.board.components)} footprints, "
                     f"{len(self.blocks)} blocks, {n_rev} parts to review")

    def _read_schematic(self):
        from . import schematic
        self.sch_groups = {}
        self.sch_info = {"file": "", "stats": {}, "error": ""}
        try:
            path = schematic.find_root_schematic(self.backend.board_file(), self.backend.project_dir())
        except Exception as e:
            path = None
            self.sch_info["error"] = str(e)
        if not path:
            self.sch_info["error"] = self.sch_info["error"] or "No schematic found next to the board"
            self.log("Schematic not found next to the board; grouping by connectivity.")
            return
        try:
            sheets = schematic.load(path)
            self.pin_names = {}
            for si in sheets:
                for sym in si.symbols:
                    if sym.pin_names:
                        self.pin_names.setdefault(sym.ref, {}).update(sym.pin_names)
            mapping, groups, stats = schematic.group(sheets)
            on_board = set(self.board.components)
            matched = sum(1 for r in mapping if r in on_board)
            stats["matched"] = matched
            stats["groups"] = len(groups)
            self.sch_groups = mapping
            self.sch_info = {"file": path, "stats": stats, "error": ""}
            self.log(f"Schematic {os.path.basename(path)}: {stats['sheets']} sheet(s), {stats['boxes']} drawn box(es), "
                     f"{len(groups)} groups; {matched} of {len(on_board)} footprints matched by reference. "
                     "Save the schematic before analyzing so recent edits are seen.")
        except Exception as e:
            self.sch_info["error"] = f"Could not read the schematic: {e}"
            self.log(self.sch_info["error"])

    def set_group_source(self, src: str):
        with self.lock:
            self.group_source = "connectivity" if src == "connectivity" else "schematic"
            self.save()
            self.analyze(reload=False)

    # ------------------------------------------------------------ review edits
    def _new_user_block(self, name: str) -> str:
        i = 1
        while f"u:{i}" in self.user_blocks or f"u:{i}" in self.blocks:
            i += 1
        bid = f"u:{i}"
        self.user_blocks[bid] = cl.Block(id=bid, name=name or f"Block {i}", kind="user", source="user")
        return bid

    def edit(self, op: str, d: dict):
        with self.lock:
            if op == "rename":
                bid = d["block"]
                self.names[bid] = d["name"]
                if bid in self.user_blocks:
                    self.user_blocks[bid].name = d["name"]
            elif op == "reassign":
                for r in d["refs"]:
                    if d["block"] == "":
                        self.overrides.pop(r, None)
                    else:
                        self.overrides[r] = d["block"]
            elif op == "confirm":
                for r in d["refs"]:
                    if r in self.assign:
                        self.overrides[r] = self.assign[r].block
            elif op == "merge":
                ids = d["blocks"]
                target = ids[0]
                for bid in ids[1:]:
                    for m in self.blocks.get(bid, cl.Block(bid, bid, "user")).members:
                        self.overrides[m] = target
                for m in self.blocks.get(target, cl.Block(target, target, "user")).members:
                    self.overrides.setdefault(m, target)
                if d.get("name"):
                    self.names[target] = d["name"]
            elif op == "split" or op == "create":
                bid = self._new_user_block(d.get("name", ""))
                anchor = d.get("anchor") or (d.get("refs") or [""])[0]
                self.user_blocks[bid].anchor = anchor
                for r in d.get("refs", []):
                    self.overrides[r] = bid
            elif op == "freeze":
                self.frozen[d["block"]] = bool(d["value"])
            elif op == "region":
                if d.get("region"):
                    r = d["region"]
                    self.regions[d["block"]] = Region(min(r["x0"], r["x1"]), min(r["y0"], r["y1"]),
                                                      max(r["x0"], r["x1"]), max(r["y0"], r["y1"]),
                                                      name="user region", source="user")
                else:
                    self.regions.pop(d["block"], None)
            elif op == "lock":
                for r in d["refs"]:
                    (self.locked.add if d["value"] else self.locked.discard)(r)
            elif op == "reset_groups":
                self.overrides.clear()
                self.user_blocks.clear()
                self.names.clear()
            else:
                raise ValueError(f"unknown edit {op}")
            self.save()
            self.analyze(reload=False)

    def set_rules(self, d: dict):
        with self.lock:
            self.rules = NetRules.from_json(d)
            for nc in self.board.netclasses if self.board else []:
                self.rules.rule(nc)
            self.save()

    def set_options(self, d: dict):
        with self.lock:
            self.options = placer.PlaceOptions.from_json({**self.options.to_json(), **d})
            self.save()

    # ------------------------------------------------------------ propose
    def propose(self, d: dict):
        with self.lock:
            if d:
                self.set_options(d)
            opts = placer.PlaceOptions.from_json(self.options.to_json())
            opts.edge_margin = max(opts.edge_margin, self._edge_rule() + 0.05)
            if opts.scope == "kicad_selection":
                opts.scope = "selection"
                opts.refs = self.backend.get_selection()
                self.log(f"Using KiCad selection: {len(opts.refs)} footprints")
                if not opts.refs:
                    raise ValueError("Nothing is selected in KiCad. Select footprints first.")
            self.log(f"Generating proposal (scope: {opts.scope})")
            use_ai = bool(d.get("use_ai", self.ai_use)) if d else self.ai_use
            self._apply_ai(opts, use_ai)
            moves, centers = placer.propose(self.board, self.blocks, self.assign, self.rules, opts,
                                            locked=self.locked, log=self.log)
            self.proposal = moves
            self.proposal_blocks = centers
            self.violations = []
            self._eval()
            self.log(f"Proposal moves {len(moves)} footprints")
            if d and d.get("auto_check"):
                self.check_and_fix()

    # ------------------------------------------------------------ check and fix
    def _positions(self):
        return {**self.board.positions(), **self.proposal}

    def _edge_rule(self) -> float:
        from . import drc
        pro = os.path.splitext(self.backend.board_file() or "")[0] + ".kicad_pro"
        return drc.load_rules(pro, self.board).edge_clearance

    def check(self, kicad: Optional[bool] = None) -> list:
        """Run the placement checks on the proposal (or the current placement)."""
        from . import drc, pcb_write
        kicad = self.use_kicad_drc if kicad is None else kicad
        pro = os.path.splitext(self.backend.board_file() or "")[0] + ".kicad_pro"
        rules = drc.load_rules(pro, self.board)
        rules.layers = max(2, getattr(self.board, "copper_layers", 2))
        pos = self._positions()
        only = set(self.proposal) if self.proposal else None
        vs = drc.builtin_checks(self.board, pos, rules, only=only)
        note = f"Built-in checks: {len(vs)} issue(s)"
        if kicad:
            try:
                text = self.backend.board_text()
                if self.proposal:
                    text, _ = pcb_write.apply_positions(text, self.proposal)
                keep = os.path.splitext(pro)[0] + ".fpm-drc.json" if self.backend.board_file() else ""
                kv, knote = drc.kicad_drc(text, pro, list(self.board.components), keep_report=keep)
                if only is not None:
                    kv = [v for v in kv if not v.refs or any(r in only for r in v.refs)]
                # KiCad's result replaces the built-in geometric checks it also covers
                if knote.startswith("KiCad DRC ("):
                    vs = [v for v in vs if v.type == "congestion"] + kv
                note += "; " + knote
                if kv:
                    from collections import Counter
                    cnt = Counter(v.type for v in kv)
                    note += " [" + ", ".join(f"{t} {n}" for t, n in cnt.most_common(6)) + "]"
            except Exception as e:
                note += f"; KiCad DRC failed: {e}"
        self.check_note = note
        self.violations = vs
        return vs

    def check_and_fix(self, rounds: Optional[int] = None, kicad: Optional[bool] = None):
        """Check the proposal; move only the parts that fail; repeat until clean or out of rounds."""
        import copy
        rounds = self.fix_rounds if rounds is None else rounds
        with self.lock:
            locked = set(self.locked) | {r for r, c in self.board.components.items() if c.locked}
            frozen = {m for b in self.blocks.values() if b.frozen for m in b.members}
            extra: Dict[str, float] = {}
            vs = []
            best = None                      # (score, proposal, violations, note)
            stale = 0
            for rnd in range(rounds + 1):
                vs = self.check(kicad)
                errors = [v for v in vs if v.severity == "error" or v.type == "congestion"]
                score = sum(1.0 if v.severity == "error" else 0.3 for v in errors)
                if best is None or score < best[0]:
                    best = (score, dict(self.proposal), list(vs), self.check_note)
                    stale = 0
                else:
                    stale += 1
                self.log(f"Check round {rnd}: {len(errors)} issue(s). {self.check_note}")
                if stale >= 2:
                    self.log("Two rounds without improvement; stopping so the layout isn't churned further.")
                    break
                culprits = {r for v in errors for r in v.refs if r not in locked and r not in frozen}
                if not errors or not culprits or rnd == rounds:
                    break
                for v in errors:
                    for r in v.refs:
                        if r in culprits:
                            step = 0.15 if v.type == "congestion" else 0.3
                            extra[r] = min(1.5, extra.get(r, 0.0) + step)
                b2 = copy.deepcopy(self.board)
                for r, (x, y, rot) in self.proposal.items():
                    c = b2.components[r]
                    c.x, c.y, c.rot = x, y, rot
                opts = placer.PlaceOptions.from_json({**self.options.to_json(), "scope": "selection",
                                                      "refs": sorted(culprits), "repair": True,
                                                      "displacement_weight": 0.4, "effort": 0.6,
                                                      "time_limit": 10.0, "seed": rnd + 2})
                opts.extra_margin = dict(extra)
                opts.hints = dict(self._hints_in_use)
                opts.edge_margin = max(opts.edge_margin, self._edge_rule() + 0.05 + 0.1 * rnd)
                moves, _ = placer.propose(b2, self.blocks, self.assign, self.rules, opts, locked=self.locked,
                                          log=lambda m: None)
                for r, v in moves.items():
                    self.proposal[r] = v
                self.log(f"  moved {len(moves)} part(s): {', '.join(sorted(moves)[:12])}"
                         f"{'…' if len(moves) > 12 else ''}")
            if best is not None and best[1] != self.proposal:
                self.proposal, vs, self.check_note = best[1], best[2], best[3]
                self.violations = vs
                self.log(f"Kept the best round ({int(best[0])} weighted issue(s)).")
            self._eval()
            left = [v for v in vs if v.severity == "error"]
            self.log("Check passed: no placement violations." if not left else
                     f"{len(left)} violation(s) remain; see the list. Locked or frozen parts can't be moved.")
            return vs

    def fix_ground(self, d: Optional[dict] = None) -> dict:
        """Stitch the ground pour together: vias between its pieces, vias or tracks for pads the
        fill does not reach, solid connections for pads with too few thermal spokes.

        d: apply (bool), net (default GND or the largest zone net), solid (bool, default True),
           mode: "auto" (live in KiCad when possible, else a fixed copy) | "live" | "copy".
        The original board file is never overwritten; "copy" writes <board>-gndfix.kicad_pcb.
        """
        from . import ground
        d = d or {}
        with self.lock:
            live = bool(self.backend.can_apply)
            if live and d.get("refill", True):
                if self.backend.refill():
                    self.log("Refilled the zones so the ground fix sees the current copper.")
            text = self.backend.board_text()
            bf = self.backend.board_file() or ""
            rules = ground.load_rules(os.path.splitext(bf)[0] + ".kicad_pro")
            gp = ground.plan(text, rules, net=d.get("net"), solid_starved=bool(d.get("solid", True)))
            self.ground_plan = gp
            for n in gp.notes if not gp.net else []:
                self.log(n)
            if not gp.net or not gp.pieces:
                return {**gp.to_json(), "applied": "", "file": ""}
            self.log(gp.summary())
            res = {**gp.to_json(), "applied": "", "file": ""}
            if not d.get("apply") or not gp.fixes:
                if not gp.fixes and not gp.unsolved:
                    self.log(f"The {gp.net} fill is already one connected piece. Nothing to fix.")
                return res
            mode = d.get("mode", "auto")
            if live and mode in ("auto", "live"):
                try:
                    n = self.backend.apply_ground(gp, f"{APP_NAME}: ground fix ({len(gp.vias)} vias)")
                    res["applied"] = "live"
                    self.log(f"Added {n['vias']} via(s) and {n['tracks']} track(s) in KiCad and refilled the "
                             f"zones. Ctrl+Z in KiCad undoes it.")
                    if gp.solids:
                        pads = ", ".join(f"{f.ref} pad {f.pad}" for f in gp.solids[:40])
                        self.log(f"{len(gp.solids)} pad(s) still have too few thermal spokes. In KiCad, open "
                                 f"each pad's properties and set Connections > Pad connection to Solid: {pads}")
                except Exception as e:
                    self.log(f"Could not add the items through KiCad's API ({type(e).__name__}: {e}); "
                             f"writing a fixed copy instead.")
                    if mode == "live":
                        raise
            if not res["applied"]:
                if not bf:
                    raise ValueError("Save the board first, so the fixed copy has somewhere to go.")
                out = os.path.splitext(bf)[0] + "-gndfix.kicad_pcb"
                with open(out, "w", encoding="utf-8", newline="\n") as f:
                    f.write(ground.write(text, gp))
                pro = os.path.splitext(bf)[0] + ".kicad_pro"
                if os.path.exists(pro) and not os.path.exists(os.path.splitext(out)[0] + ".kicad_pro"):
                    import shutil
                    shutil.copy(pro, os.path.splitext(out)[0] + ".kicad_pro")
                res["applied"], res["file"] = "copy", out
                self.log(f"Wrote the fixed board to {out}. Your original file is untouched. Open the copy, "
                         f"press B to refill the zones, and run DRC.")
            for u in gp.unsolved:
                self.log("  needs a manual fix: " + u)
            for n in gp.notes:
                self.log(n)
            return res

    def _eval(self):
        self.metrics_after = metrics.evaluate(self.board, self.rules, self.proposal, label="proposal")

    def edit_proposal(self, d: dict):
        """Manual tweaks from the preview canvas: {ref: [x, y, rot] | null}"""
        with self.lock:
            for r, v in d.get("moves", {}).items():
                if r not in self.board.components:
                    continue
                if v is None:
                    self.proposal.pop(r, None)
                else:
                    self.proposal[r] = (float(v[0]), float(v[1]), norm_angle(float(v[2])))
            self._eval()

    def replicate(self, src: str, targets: List[str]):
        with self.lock:
            pos = {**self.board.positions(), **self.proposal}
            total, missing = 0, []
            for t in targets:
                if t == src or t not in self.blocks:
                    continue
                mv, unmatched = templates.replicate(self.board, self.blocks[src], self.blocks[t], pos)
                self.proposal.update(mv)
                total += len(mv)
                missing += unmatched
            self._eval()
            self.log(f"Replicated {self.blocks[src].name} onto {len(targets) - (src in targets)} block(s): "
                     f"{total} parts" + (f"; unmatched: {', '.join(missing)}" if missing else ""))

    def save_template(self, bid: str, name: str):
        with self.lock:
            pos = {**self.board.positions(), **self.proposal}
            path = templates.save_template(self.board, self.blocks[bid], pos, self.template_dir, name)
            self.log(f"Saved template {os.path.basename(path)}")

    def apply_template(self, bid: str, fn: str):
        with self.lock:
            pos = {**self.board.positions(), **self.proposal}
            mv, warn = templates.apply_template(self.board, self.blocks[bid], self.template_dir, fn, pos)
            self.proposal.update(mv)
            self._eval()
            for w in warn:
                self.log(w)
            self.log(f"Template {fn}: positioned {len(mv)} parts of {self.blocks[bid].name}")

    # ------------------------------------------------------------ apply / undo
    def auto_place(self, apply: bool = True, remove_tracks: bool = False, ai: Optional[str] = None) -> dict:
        """One click: re-read board and schematic, place everything (blocks, master/slave),
        check and fix, then apply to KiCad as a single undo step."""
        with self.lock:
            self.log("Auto place: reading the board and schematic")
            self.analyze(reload=True)
            if self.group_source != "schematic" and self.sch_groups:
                self.log("Auto place groups by the schematic (it was set to connectivity only).")
                self.group_source = "schematic"
                self.save()
                self.analyze(reload=False)
            if ai:
                self.ai_suggest(ai)
            before = dict(self.metrics_before or {})
            # plain Auto place ignores AI suggestions; AI auto place uses all of them
            self.propose({"scope": "all", "auto_check": True, "use_ai": bool(ai)})
            after = dict(self.metrics_after or {})
            errors = [v for v in self.violations if v.severity == "error"]
            moved = len(self.proposal)
            applied = 0
            if apply and self.proposal:
                applied = self.apply({"remove_tracks": remove_tracks})
            summary = {
                "moved": moved, "applied": applied, "errors": len(errors),
                "warnings": len(self.violations) - len(errors),
                "ratsnest": (before.get("signal_ratsnest_mm"), after.get("signal_ratsnest_mm")),
                "crossings": (before.get("crossings"), after.get("crossings")),
                "check": self.check_note,
                "ai": (f"{sum(1 for x in self.ai_suggestions if x.get('enabled', True))} AI hint(s) "
                       f"from {ai}") if ai else "",
            }
            self.log(f"Auto place done: {applied or moved} footprints placed, {len(errors)} error(s) left. "
                     f"Ratsnest {summary['ratsnest'][0]} -> {summary['ratsnest'][1]} mm.")
            self.last_auto = summary
            return summary

    # ------------------------------------------------------------ AI placement
    def _fixed_refs(self) -> set:
        o = self.options
        out = set(self.locked)
        for r, c in self.board.components.items():
            if c.locked or (o.keep_connectors_fixed and cl.role(c) == "connector"):
                out.add(r)
        return out

    def _apply_ai_blocks(self):
        """Show enabled AI block names / masters on the blocks (not saved as your own names)."""
        if not self.ai_suggestions or not self.blocks:
            return
        from . import ai
        _h, masters, names = ai.to_hints(self.ai_suggestions, self.assign, self.blocks)
        for bid, m in masters.items():
            self.blocks[bid].anchor = m
        for bid, nm in names.items():
            if bid not in self.names:              # a name you typed yourself always wins
                self.blocks[bid].name = nm

    def _apply_ai(self, opts, use: bool):
        """Turn the enabled AI suggestions into placer hints for this proposal."""
        from . import ai
        self._hints_in_use = {}
        opts.hints = {}
        if not use or not self.ai_suggestions:
            if self.ai_suggestions and not use:
                self.log("AI suggestions not used for this proposal.")
            return
        hints, masters, names = ai.to_hints(self.ai_suggestions, self.assign, self.blocks)
        opts.hints = hints
        self._hints_in_use = hints
        n = len(hints["near"]) + len(hints["edge"]) + len(hints["zones"]) + len(hints["apart"])
        self.log(f"Using {n} AI placement hint(s), {len(masters)} master change(s), {len(names)} block name(s)")

    def ai_suggest(self, source: Optional[str] = None, model: Optional[str] = None) -> List[dict]:
        """Collect suggestions from Claude and/or the learned library (replaces the previous list)."""
        from . import ai
        with self.lock:
            if self.board is None:
                self.analyze()
            source = source or self.ai_source
            self.ai_source = source
            fixed = self._fixed_refs()
            movable = set(self.board.components) - fixed
            out: List[dict] = []
            notes = []
            if source in ("learned", "both"):
                lib = ai.Library()
                got = lib.suggest(self.board, self.blocks, self.assign, self.pin_names, movable)
                self.log(f"Learned library: {len(got)} suggestion(s). {lib.stats()}")
                out += got
            if source in ("claude", "both"):
                cfg = ai.load_config()
                model = model or cfg.get("model") or ai.MODELS[0]
                desc = ai.describe(self.board, self.blocks, self.assign, self.pin_names, fixed)
                self.log(f"Asking Claude ({model}) to read the design ({len(desc) // 1000} kB summary)")
                plan = ai.claude_plan(desc, ai.api_key(), model, log=self.log)
                got = ai.plan_to_suggestions(plan, self.board, self.blocks, fixed)
                self.log(f"Claude: {len(got)} suggestion(s)")
                if plan.get("notes"):
                    notes.append("Claude: " + plan["notes"].strip())
                if source == "both":           # learned exact habits win over generic advice for the same part
                    have = {(x["kind"], x["data"].get("ref")) for x in out if x["kind"] in ("near", "edge")}
                    got = [x for x in got if (x["kind"], x["data"].get("ref")) not in have]
                out += got
            seen, uniq = set(), []
            for x in out:
                if x["id"] in seen:
                    x["id"] += f"#{len(uniq)}"
                seen.add(x["id"])
                uniq.append(x)
            self.ai_suggestions = uniq
            self.ai_notes = "\n".join(notes)
            self.save()
            self.analyze(reload=False)             # show AI block names / masters
            return uniq

    def ai_set_enabled(self, ids, enabled: bool):
        with self.lock:
            ids = set(ids)
            blocks_changed = False
            for x in self.ai_suggestions:
                if x["id"] in ids and x.get("enabled", True) != bool(enabled):
                    x["enabled"] = bool(enabled)
                    blocks_changed |= x["kind"] in ("name", "master")
            self.save()
            if blocks_changed and self.board is not None:
                self.analyze(reload=False)         # restore / re-apply AI block names and masters
            return blocks_changed

    def ai_clear(self):
        with self.lock:
            self.ai_suggestions = []
            self.ai_notes = ""
            self.save()
            if self.board is not None:
                self.analyze(reload=False)

    def learn_board(self) -> int:
        """Teach the learned library this board's current (hand-made or finished) layout."""
        from . import ai
        with self.lock:
            if self.board is None:
                self.analyze()
            lib = ai.Library()
            n = lib.learn(self.board, self.blocks, self.assign, self.pin_names)
            self.log(f"Learned {n} part placements from {self.board.name}. {lib.stats()}")
            return n

    def forget_learned(self):
        from . import ai
        ai.Library().clear()
        self.log("Cleared everything the tool had learned.")

    def _learn_corrections(self):
        """Parts the user moved by hand after we applied a proposal are strong examples.
        Parts that went back to where they were before the Apply (Ctrl+Z in KiCad) are not."""
        if not self.last_applied or not self.ai_learn_auto:
            return
        from . import ai
        C = self.board.components

        def at(c, p):
            return math.hypot(c.x - p[0], c.y - p[1]) <= 0.3 and abs(norm_angle(c.rot - p[2])) <= 1

        moved, reverted = [], 0
        for r, v in self.last_applied.items():
            if r not in C:
                continue
            if at(C[r], v[:3]):
                continue
            if len(v) >= 6 and at(C[r], v[3:6]):
                reverted += 1
                continue
            moved.append(r)
        self.last_applied = {}
        if reverted:
            self.log(f"{reverted} part(s) were put back where they were before the last Apply; not learned.")
        if not moved or len(moved) > 0.6 * len(C):
            self.save()
            return
        try:
            n = ai.Library().learn(self.board, self.blocks, self.assign, self.pin_names, refs=moved,
                                   weight=2.0, tag=self.board.name + " (corrections)")
            if n:
                self.log(f"Learned from {n} of the {len(moved)} part(s) you moved by hand after the last Apply.")
            else:
                self.log(f"You moved {len(moved)} part(s) by hand after the last Apply; none of them sits next to "
                         "an IC pin it serves, so there was nothing to learn.")
        except Exception as e:
            self.log(f"Could not learn from your corrections: {e}")
        self.save()

    def apply(self, d: dict):
        with self.lock:
            if not self.proposal:
                raise ValueError("No proposal to apply. Generate one in Preview first.")
            blocks = d.get("blocks")
            refs = d.get("refs")
            sel = dict(self.proposal)
            if blocks is not None:
                keep = {m for b in blocks if b in self.blocks for m in self.blocks[b].members}
                sel = {r: v for r, v in sel.items() if r in keep}
            if refs is not None:
                sel = {r: v for r, v in sel.items() if r in set(refs)}
            if not sel:
                raise ValueError("The selected blocks have no proposed moves.")
            before = {r: (c.x, c.y, c.rot) for r, c in self.board.components.items() if r in sel}
            rt = bool(d.get("remove_tracks"))
            msg = f"{APP_NAME}: place {len(sel)} footprints"
            if rt:
                n = self.backend.apply(sel, msg + " (old tracks removed)", remove_tracks=True)
            else:
                n = self.backend.apply(sel, msg)
            if rt:
                self.log("Removed the old tracks and vias and refilled the zones; route the board again.")
                self.board.track_count = 0
            self.undo_stack.append(before)
            self.last_applied.update({r: [x, y, rot, *before[r]] for r, (x, y, rot) in sel.items()})
            self.save()
            for r, (x, y, rot) in sel.items():
                c = self.board.components[r]
                c.x, c.y, c.rot = x, y, rot
                self.proposal.pop(r, None)
            self.metrics_before = metrics.evaluate(self.board, self.rules, label="current")
            self._eval()
            self.log(f"Applied {n} footprints" + ("" if self.backend.can_apply else " (offline: in memory only)"))
            return n

    def undo(self):
        with self.lock:
            if not self.undo_stack:
                raise ValueError("Nothing to undo.")
            before = self.undo_stack.pop()
            n = self.backend.apply(before, f"{APP_NAME}: undo")
            for r in before:
                self.last_applied.pop(r, None)
            self.save()
            for r, (x, y, rot) in before.items():
                if r in self.board.components:
                    c = self.board.components[r]
                    c.x, c.y, c.rot = x, y, rot
            self.metrics_before = metrics.evaluate(self.board, self.rules, label="current")
            self._eval()
            self.log(f"Restored {n} footprints")

    # ------------------------------------------------------------ state
    def state(self) -> dict:
        with self.lock:
            if self.board is None:
                return {"loaded": False, "backend": self.backend.name, "logs": self.logs[-80:]}
            review = [a.to_json() for a in self.assign.values() if a.confidence < cl.REVIEW_THRESHOLD]
            repeated = cl.repeated_groups(self.board, self.blocks)
            return {
                "loaded": True,
                "backend": self.backend.name, "can_apply": self.backend.can_apply,
                "version": self.backend.version(),
                "board": self.board.to_json(),
                "blocks": [b.to_json() for b in sorted(self.blocks.values(),
                                                       key=lambda b: (b.kind in ("misc", "mechanical"), b.name))],
                "assign": {r: a.to_json() for r, a in self.assign.items()},
                "review": sorted(review, key=lambda a: a["confidence"]),
                "repeated": repeated,
                "locked": sorted(self.locked),
                "rules": self.rules.to_json(),
                "options": self.options.to_json(),
                "proposal": {r: list(v) for r, v in self.proposal.items()},
                "proposal_blocks": self.proposal_blocks,
                "metrics_before": self.metrics_before, "metrics_after": self.metrics_after,
                "undo_depth": len(self.undo_stack),
                "templates": templates.list_templates(self.template_dir),
                "settings_file": self.settings_path,
                "group_source": self.group_source,
                "schematic": self.sch_info,
                "violations": [v.to_json() for v in self.violations], "check_note": self.check_note,
                "logs": self.logs[-80:],
            }
