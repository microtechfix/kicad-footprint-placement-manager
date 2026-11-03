"""Live KiCad backend over the official IPC API (kicad-python / kipy).

Reads the open board into the KiCad-independent model and writes accepted
placements back as one undoable commit.
"""
from __future__ import annotations

import os
from typing import Dict, List, Tuple

from .model import Board, Component, Pad, Region, unrotate

NM = 1e-6   # nanometres -> millimetres


def _mm(v) -> Tuple[float, float]:
    return v.x * NM, v.y * NM


def _shape_points(shape) -> List[Tuple[float, float]]:
    """Characteristic points of any kipy board shape, in mm (absolute)."""
    pts = []
    for attr in ("start", "mid", "end", "top_left", "bottom_right"):
        try:
            v = getattr(shape, attr)
            if v is not None and hasattr(v, "x"):
                pts.append(_mm(v))
        except Exception:
            pass
    try:
        c, rp = shape.center, shape.radius_point
        if c is not None and rp is not None and not pts:
            cx, cy = _mm(c)
            rx, ry = _mm(rp)
            r = ((cx - rx) ** 2 + (cy - ry) ** 2) ** 0.5
            pts += [(cx - r, cy - r), (cx + r, cy + r)]
    except Exception:
        pass
    try:
        for poly in shape.polygons:
            for node in poly.outline.nodes:
                try:
                    pts.append(_mm(node.point))
                except Exception:
                    pass
    except Exception:
        pass
    return pts


class LiveBackend:
    name = "KiCad (live)"
    can_apply = True

    def __init__(self):
        from . import kipy_compat
        kipy_compat.ensure_typing_extensions()
        try:
            kipy_compat.install()      # newer KiCad item types vs. older kicad-python
        except Exception as e:         # different kicad-python layout: carry on without the shim
            print(f"kipy compatibility shim skipped: {e!r}")
        from kipy import KiCad
        self.kicad = KiCad(client_name="footprint-placement-manager", timeout_ms=20000)
        self.board = None

    # ------------------------------------------------------------------
    def version(self) -> str:
        try:
            return self.kicad.get_version().full_version
        except Exception:
            return "unknown"

    def project_dir(self) -> str:
        try:
            return self.board.get_project().path
        except Exception:
            return os.getcwd()

    def board_file(self) -> str:
        try:
            return os.path.join(self.project_dir(), self.board.name)
        except Exception:
            return ""

    # ------------------------------------------------------------------
    def load(self) -> Board:
        from kipy.proto.board.board_types_pb2 import BoardLayer, PadType
        from kipy.board_types import Field

        self.board = self.kicad.get_board()
        kb = self.board
        b = Board(name=os.path.splitext(kb.name)[0], path=self.board_file())

        # net classes
        try:
            nets = kb.get_nets()
            ncs = kb.get_netclass_for_nets(nets)
            b.netclass_of = {name: nc.name for name, nc in ncs.items()}
            b.netclasses = sorted(set(b.netclass_of.values()) | {"Default"})
        except Exception:
            pass

        crt_layers = {BoardLayer.BL_F_CrtYd, BoardLayer.BL_B_CrtYd}
        fab_layers = {BoardLayer.BL_F_Fab, BoardLayer.BL_B_Fab}
        for fp in kb.get_footprints():
            ref = fp.reference_field.text.value
            x, y = _mm(fp.position)
            rot = fp.orientation.degrees
            comp = Component(
                ref=ref, uid=fp.id.value, value=fp.value_field.text.value,
                footprint=str(fp.definition.id), x=x, y=y, rot=rot,
                side="B" if fp.layer == BoardLayer.BL_B_Cu else "F", locked=fp.locked,
            )
            try:
                comp.dnp = fp.attributes.do_not_populate
            except Exception:
                pass
            try:
                comp.sheet = fp.proto.symbol_sheet_name or fp.sheet_path.path_human_readable
            except Exception:
                pass
            # user fields (CircuitBlock, PlaceNear, PlacementPriority ...)
            for item in fp.definition.items:
                if isinstance(item, Field):
                    try:
                        comp.fields[item.name] = item.text.value
                    except Exception:
                        pass
            try:
                for cp in fp.proto.custom_properties:
                    comp.fields.setdefault(cp.name, cp.value)
            except Exception:
                pass

            pts_crt, pts_other = [], []
            for pad in fp.definition.pads:
                px, py = _mm(pad.position)
                lx, ly = unrotate(px - x, py - y, rot)
                w = h = 0.5
                try:
                    layers = pad.padstack.copper_layers
                    if layers:
                        w, h = _mm(layers[0].size)
                    la = (pad.padstack.angle.degrees - rot) % 180
                    if 45 < la < 135:
                        w, h = h, w
                except Exception:
                    pass
                smd = pad.pad_type in (PadType.PT_SMD, PadType.PT_EDGE_CONNECTOR)
                if pad.pad_type in (PadType.PT_PTH, PadType.PT_NPTH):
                    comp.tht = True
                comp.pads.append(Pad(number=pad.number, net=pad.net.name if pad.net else "",
                                     lx=lx, ly=ly, w=w, h=h, smd=smd))
                pts_other += [(lx - w / 2, ly - h / 2), (lx + w / 2, ly + h / 2)]
            for sh in fp.definition.shapes:
                try:
                    lay = sh.layer
                except Exception:
                    continue
                if lay in crt_layers or lay in fab_layers:
                    loc = [unrotate(px - x, py - y, rot) for px, py in _shape_points(sh)]
                    (pts_crt if lay in crt_layers else pts_other).extend(loc)
            src = pts_crt or pts_other
            if src:
                xs = [p[0] for p in src]
                ys = [p[1] for p in src]
                pad = 0.0 if pts_crt else 0.25
                comp.xmin, comp.xmax = min(xs) - pad, max(xs) + pad
                comp.ymin, comp.ymax = min(ys) - pad, max(ys) + pad
                for pd in comp.pads:     # pads sticking out of the courtyard still take space
                    comp.xmin = min(comp.xmin, pd.lx - pd.w / 2)
                    comp.xmax = max(comp.xmax, pd.lx + pd.w / 2)
                    comp.ymin = min(comp.ymin, pd.ly - pd.h / 2)
                    comp.ymax = max(comp.ymax, pd.ly + pd.h / 2)
            r0, n = ref, 2
            while ref in b.components:
                ref = f"{r0}#{n}"
                n += 1
            comp.ref = ref
            b.components[ref] = comp

        # outline
        pts = []
        for sh in kb.get_shapes():
            try:
                if sh.layer != BoardLayer.BL_Edge_Cuts:
                    continue
            except Exception:
                continue
            p = _shape_points(sh)
            pts += p
            for i in range(len(p) - 1):
                b.outline_segments.append((p[i][0], p[i][1], p[i + 1][0], p[i + 1][1]))
            if len(p) == 2 and hasattr(sh, "top_left"):
                (a, c), (d, e) = p
                b.outline_segments[-1:] = [(a, c, d, c), (d, c, d, e), (d, e, a, e), (a, e, a, c)]
        if pts:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            b.outline = Region(min(xs), min(ys), max(xs), max(ys), name="Edge.Cuts")

        try:
            b.copper_layers = int(kb.get_copper_layer_count())
        except Exception:
            pass
        try:
            b.track_count = len(kb.get_tracks()) + len(kb.get_vias())
        except Exception:
            pass

        # rule areas: footprint keep-outs and placement areas
        try:
            zones = kb.get_zones()
            b.zone_count = sum(1 for z in zones if not z.is_rule_area())
            for z in zones:
                if not z.is_rule_area():
                    continue
                bb = z.bounding_box()
                x0, y0 = _mm(bb.pos)
                w, h = _mm(bb.size)
                ra = z.proto.rule_area_settings
                if ra.keepout_footprints:
                    b.keepouts.append(Region(x0, y0, x0 + w, y0 + h, name=z.name or "keepout"))
                if ra.placement_enabled:
                    b.placement_areas.append(Region(x0, y0, x0 + w, y0 + h, name=z.name,
                                                    source=ra.placement_source))
        except Exception:
            pass
        return b

    # ------------------------------------------------------------------
    def _footprints_by_ref(self) -> Dict[str, object]:
        out = {}
        for fp in self.board.get_footprints():
            ref = fp.reference_field.text.value
            r0, n = ref, 2
            while ref in out:
                ref = f"{r0}#{n}"
                n += 1
            out[ref] = fp
        return out

    def apply(self, moves: Dict[str, Tuple[float, float, float]], message: str,
              remove_tracks: bool = False) -> int:
        from kipy.geometry import Vector2, Angle
        if self.board is None:
            self.board = self.kicad.get_board()
        fps = self._footprints_by_ref()
        changed = []
        for ref, (x, y, rot) in moves.items():
            fp = fps.get(ref)
            if fp is None:
                continue
            fp.orientation = Angle.from_degrees(rot)
            fp.position = Vector2.from_xy(int(round(x * 1e6)), int(round(y * 1e6)))
            changed.append(fp)
        if not changed:
            return 0
        commit = self.board.begin_commit()
        try:
            self.board.update_items(changed)
            if remove_tracks:
                old = list(self.board.get_tracks()) + list(self.board.get_vias())
                if old:
                    self.board.remove_items(old)
        except Exception:
            self.board.drop_commit(commit)
            raise
        self.board.push_commit(commit, message)
        if remove_tracks:
            try:
                self.board.refill_zones(block=True, max_poll_seconds=60.0)
            except Exception:
                pass
        return len(changed)

    def board_text(self) -> str:
        """The board as KiCad currently holds it (including unsaved edits)."""
        if self.board is None:
            self.board = self.kicad.get_board()
        return self.board.get_as_string()

    def current_positions(self) -> Dict[str, Tuple[float, float, float]]:
        return {r: (_mm(fp.position)[0], _mm(fp.position)[1], fp.orientation.degrees)
                for r, fp in self._footprints_by_ref().items()}

    def select(self, refs: List[str]):
        fps = self._footprints_by_ref()
        items = [fps[r] for r in refs if r in fps]
        self.board.clear_selection()
        if items:
            self.board.add_to_selection(items)

    def get_selection(self) -> List[str]:
        from kipy.board_types import FootprintInstance
        out = []
        for it in self.board.get_selection():
            if isinstance(it, FootprintInstance):
                out.append(it.reference_field.text.value)
        return out


class FileBackend:
    """Offline backend: reads a .kicad_pcb, never writes it. Applied moves stay in memory."""
    name = "File (offline preview)"
    can_apply = False

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.overrides: Dict[str, Tuple[float, float, float]] = {}

    def version(self):
        return "offline"

    def project_dir(self):
        return os.path.dirname(self.path)

    def board_file(self):
        return self.path

    def load(self) -> Board:
        from . import pcb_file
        b = pcb_file.load(self.path)
        for r, (x, y, rot) in self.overrides.items():
            if r in b.components:
                c = b.components[r]
                c.x, c.y, c.rot = x, y, rot
        return b

    def apply(self, moves, message, remove_tracks: bool = False):
        self.overrides.update(moves)
        return len(moves)

    def board_text(self) -> str:
        from . import pcb_write
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        if self.overrides:
            text, _ = pcb_write.apply_positions(text, self.overrides)
        return text

    def current_positions(self):
        b = self.load()
        return b.positions()

    def select(self, refs):
        pass

    def get_selection(self):
        return []
