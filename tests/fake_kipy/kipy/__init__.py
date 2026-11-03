"""Minimal stand-in for kicad-python used by tests (mirrors the attributes fpm uses)."""
import math
from types import SimpleNamespace as NS
from .geometry import Vector2, Angle
from .board_types import Field, FootprintInstance, _Txt
from .proto.board.board_types_pb2 import BoardLayer, PadType

FAKE_BOARD = None   # set by the test


def _rot(x, y, deg):
    r = math.radians(deg); c, s = math.cos(r), math.sin(r)
    return x * c + y * s, -x * s + y * c


def nm(v): return int(round(v * 1e6))


class FakeFP(FootprintInstance):
    def __init__(self, comp):
        self.c = comp
        self.reference_field = _Txt(comp.ref)
        self.value_field = _Txt(comp.value)
        self.id = NS(value=comp.uid)
        self.position = Vector2(nm(comp.x), nm(comp.y))
        self.orientation = Angle(comp.rot)
        self.layer = BoardLayer.BL_B_Cu if comp.side == "B" else BoardLayer.BL_F_Cu
        self.locked = comp.locked
        self.attributes = NS(do_not_populate=comp.dnp)
        self.proto = NS(symbol_sheet_name=comp.sheet, custom_properties=[])
        self.sheet_path = NS(path_human_readable=comp.sheet)
        pads = []
        for p in comp.pads:
            dx, dy = _rot(p.lx, p.ly, comp.rot)
            pads.append(NS(number=p.number, position=Vector2(nm(comp.x + dx), nm(comp.y + dy)),
                           net=NS(name=p.net), pad_type=PadType.PT_SMD if p.smd else PadType.PT_PTH,
                           padstack=NS(copper_layers=[NS(size=Vector2(nm(p.w), nm(p.h)))], angle=Angle(comp.rot))))
        lay = BoardLayer.BL_B_CrtYd if comp.side == "B" else BoardLayer.BL_F_CrtYd
        corners = [(comp.xmin, comp.ymin), (comp.xmax, comp.ymin), (comp.xmax, comp.ymax), (comp.xmin, comp.ymax)]
        shapes = []
        for i in range(4):
            a = _rot(*corners[i], comp.rot); b = _rot(*corners[(i + 1) % 4], comp.rot)
            shapes.append(NS(layer=lay, start=Vector2(nm(comp.x + a[0]), nm(comp.y + a[1])),
                             end=Vector2(nm(comp.x + b[0]), nm(comp.y + b[1]))))
        items = [Field(k, v) for k, v in comp.fields.items()]
        self.definition = NS(id=comp.footprint, pads=pads, shapes=shapes, items=items)


class FakeBoard:
    def __init__(self, model):
        self.m = model
        self.name = model.name + ".kicad_pcb"
        self.fps = [FakeFP(c) for c in model.components.values()]
        self.commits = []
        self.updated = []
        self.selection = []

    def get_project(self): return NS(path="/tmp")
    def get_nets(self): return [NS(name=n) for n in self.m.net_members()]
    def get_netclass_for_nets(self, nets): return {n.name: NS(name=self.m.netclass(n.name)) for n in nets}
    def get_footprints(self): return list(self.fps)
    def get_shapes(self):
        return [NS(layer=BoardLayer.BL_Edge_Cuts, start=Vector2(nm(s[0]), nm(s[1])), end=Vector2(nm(s[2]), nm(s[3])))
                for s in self.m.outline_segments]
    def get_zones(self): return []
    def get_copper_layer_count(self): return 4
    def get_tracks(self): return list(getattr(self, "tracks", ["t1", "t2"]))
    def get_vias(self): return ["v1"] if getattr(self, "tracks", None) is None else []
    def remove_items(self, items): self.removed = list(items); self.tracks = []
    def refill_zones(self, block=True, max_poll_seconds=30.0): self.commits.append("refill")
    def begin_commit(self): self.commits.append("open"); return "c1"
    def update_items(self, items): self.updated.extend(items); return items
    def push_commit(self, c, msg): self.commits.append(msg)
    def drop_commit(self, c): self.commits.append("drop")
    def clear_selection(self): self.selection = []
    def add_to_selection(self, items): self.selection = list(items)
    def get_selection(self): return list(self.selection)


class KiCad:
    def __init__(self, *a, **k): pass
    def get_board(self): return FAKE_BOARD
    def get_version(self): return NS(full_version="10.0.0-fake")
