"""Reproduces 'Missing type mapping ... ReferencePoint' with a kicad-python build that
predates KiCad 10.0.7, then checks fpm.kipy_compat:
  - footprints load,
  - moving/rotating a footprint carries its reference point along,
  - every other reference-point field survives the round trip unchanged.

Needs: a kicad-python tree generated without ReferencePoint (KIPY_OLD) and a
descriptor set of the full KiCad 10 board_types.proto (FULL_DESC).
"""
import math
import os
import sys

KIPY_OLD, FULL_DESC = sys.argv[1], sys.argv[2]
sys.path[:0] = [KIPY_OLD, os.path.join(KIPY_OLD, "stubs"),
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]

from google.protobuf import descriptor_pb2, descriptor_pool
from google.protobuf.any_pb2 import Any
from google.protobuf.message_factory import GetMessageClass

import kipy.board_types as bt
from kipy.proto.board import board_types_pb2 as old
from kipy.geometry import Vector2, Angle

# full (KiCad 10.0.7+) ReferencePoint in a private pool, as KiCad would send it
fds = descriptor_pb2.FileDescriptorSet()
fds.ParseFromString(open(FULL_DESC, "rb").read())
pool = descriptor_pool.DescriptorPool()
for f in fds.file:
    pool.Add(f)
FullRP = GetMessageClass(pool.FindMessageTypeByName("kiapi.board.types.ReferencePoint"))


def make_fp():
    fp = old.FootprintInstance()
    fp.id.value = "fp-1"
    fp.position.x_nm, fp.position.y_nm = 100_000_000, 50_000_000
    fp.orientation.value_degrees = 0
    fp.layer = old.BoardLayer.BL_F_Cu
    fp.reference_field.text.text.text = "U1"
    pad = old.Pad()
    pad.id.value = "pad-1"
    pad.number = "1"
    pad.position.x_nm, pad.position.y_nm = 102_000_000, 50_000_000
    a = Any(); a.Pack(pad); fp.definition.items.append(a)
    rp = FullRP()
    rp.id.value = "rp-1"
    rp.layer = 3
    rp.position.x_nm, rp.position.y_nm = 100_000_000, 53_000_000
    rp.size.value_nm = 1_500_000
    rp.locked = 2
    rp.parent.value = "fp-1"
    a = Any(); a.Pack(rp); fp.definition.items.append(a)
    return fp


# 1) the user's error
try:
    bt.FootprintInstance(make_fp())
    print("FAIL: expected the old kicad-python to choke")
    sys.exit(1)
except NotImplementedError as e:
    print("reproduced:", e)

# 2) with the shim
from fpm import kipy_compat
kipy_compat.install()
fi = bt.FootprintInstance(make_fp())
print("loaded footprint with", len(fi.definition.items), "items; pads:", [p.number for p in fi.definition.pads])

fi.orientation = Angle.from_degrees(90)
fi.position = Vector2.from_xy(120_000_000, 60_000_000)
out = fi.proto
pad = rp = None
for a in out.definition.items:
    if a.type_url.endswith("ReferencePoint"):
        rp = FullRP(); a.Unpack(rp)
    elif a.type_url.endswith(".Pad"):
        pad = old.Pad(); a.Unpack(pad)
assert rp is not None, "reference point was dropped"

# expected: KiCad rotation convention around the footprint origin, then translate
def expect(x, y):
    dx, dy = x - 100_000_000, y - 50_000_000
    r = math.radians(90)
    return (int(dy * math.sin(r) + dx * math.cos(r)) + 120_000_000,
            int(dy * math.cos(r) - dx * math.sin(r)) + 60_000_000)

ex = expect(100_000_000, 53_000_000)
ep = expect(102_000_000, 50_000_000)
assert (pad.position.x_nm, pad.position.y_nm) == ep, (pad.position, ep)
assert abs(rp.position.x_nm - ex[0]) <= 1 and abs(rp.position.y_nm - ex[1]) <= 1, (rp.position, ex)
assert rp.id.value == "rp-1" and rp.layer == 3 and rp.size.value_nm == 1_500_000
assert rp.locked == 2 and rp.parent.value == "fp-1"
print("pad moved to", ep, "| reference point moved to", (rp.position.x_nm, rp.position.y_nm),
      "| id/layer/size/locked/parent intact")

# 3) an unknown future item type is kept byte-for-byte
fp = make_fp()
weird = Any(type_url="type.googleapis.com/kiapi.board.types.SomethingNew", value=b"\x08\x01\x12\x03abc")
fp.definition.items.append(weird)
fi2 = bt.FootprintInstance(fp)
fi2.position = Vector2.from_xy(1, 2)
kept = [a for a in fi2.proto.definition.items if a.type_url.endswith("SomethingNew")]
assert kept and kept[0].value == weird.value
print("unknown item type preserved unchanged")
print("OK")
