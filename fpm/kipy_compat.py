"""Compatibility shims for kicad-python versions older than the running KiCad.

KiCad 10.0.7 added `ReferencePoint` items inside footprints. kicad-python
releases that predate it raise "Missing type mapping ... ReferencePoint" while
reading footprints. This module:

  * registers a minimal ReferencePoint message (only the position field is
    declared; every other field is kept byte-for-byte as an unknown field),
  * wraps it so footprint moves/rotations carry the reference point along,
  * keeps any *other* unknown item type as an opaque blob that is written back
    unchanged, instead of crashing or silently deleting it.

Everything is a no-op when kicad-python already knows these types.
"""
from __future__ import annotations

import math

_installed = False


def ensure_typing_extensions():
    """Some kicad-python builds import typing_extensions even on Python 3.13, where its
    installer skips that package. Provide a stand-in built from the standard library."""
    try:
        import typing_extensions  # noqa: F401
        return
    except ImportError:
        pass
    import sys
    import types
    import typing
    import warnings
    mod = types.ModuleType("typing_extensions")

    def _deprecated_fallback(msg, *a, **k):
        def deco(obj):
            return obj
        return deco

    mod.deprecated = getattr(warnings, "deprecated", _deprecated_fallback)

    def __getattr__(name):
        if hasattr(typing, name):
            return getattr(typing, name)
        raise AttributeError(name)

    mod.__getattr__ = __getattr__
    sys.modules["typing_extensions"] = mod


def _build_reference_point_class():
    from google.protobuf import descriptor_pb2, descriptor_pool
    pool = descriptor_pool.Default()
    try:
        pool.FindMessageTypeByName("kiapi.board.types.ReferencePoint")
        return None                                   # kicad-python already has it
    except KeyError:
        pass
    fdp = descriptor_pb2.FileDescriptorProto()
    fdp.name = "fpm_compat/reference_point.proto"
    fdp.package = "kiapi.board.types"
    fdp.syntax = "proto3"
    v = fdp.message_type.add()
    v.name = "FpmCompatVector2"
    for i, n in ((1, "x_nm"), (2, "y_nm")):
        f = v.field.add()
        f.name, f.number = n, i
        f.type = descriptor_pb2.FieldDescriptorProto.TYPE_INT64
        f.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    m = fdp.message_type.add()
    m.name = "ReferencePoint"
    f = m.field.add()
    f.name, f.number = "position", 3
    f.type = descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE
    f.type_name = ".kiapi.board.types.FpmCompatVector2"
    f.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    pool.Add(fdp)
    desc = pool.FindMessageTypeByName("kiapi.board.types.ReferencePoint")
    try:
        from google.protobuf.message_factory import GetMessageClass
        return GetMessageClass(desc)
    except ImportError:                                # protobuf < 4.21
        from google.protobuf import message_factory
        return message_factory.MessageFactory(pool).GetPrototype(desc)


class PositionedItem:
    """Wrapper for a footprint child kicad-python can't model; it only knows its position."""

    def __init__(self, proto):
        self._proto = proto

    @property
    def proto(self):
        return self._proto

    def move(self, dx: int, dy: int):
        p = self._proto.position
        p.x_nm += dx
        p.y_nm += dy

    def rotate(self, degrees: float, cx: int, cy: int):
        p = self._proto.position
        x, y = p.x_nm - cx, p.y_nm - cy
        r = math.radians(degrees)
        s, c = math.sin(r), math.cos(r)
        p.x_nm = int(y * s + x * c) + cx
        p.y_nm = int(y * c - x * s) + cy


class OpaqueItem:
    """Unknown item type: kept as the original packed bytes and written back unchanged."""

    def __init__(self, any_msg):
        from google.protobuf.any_pb2 import Any
        self.raw = Any()
        self.raw.CopyFrom(any_msg)


def install():
    global _installed
    if _installed:
        return
    _installed = True
    from google.protobuf.any_pb2 import Any
    import kipy.board_types as bt
    import kipy.util.proto as kp
    try:
        import kipy.board as kb
    except Exception:
        kb = None

    url = "type.googleapis.com/kiapi.board.types.ReferencePoint"
    if url not in kp._any_urls:
        cls = _build_reference_point_class()
        if cls is not None:
            kp._any_urls[url] = cls
            rp_cls = cls
        else:
            rp_cls = None
    else:
        rp_cls = None if hasattr(bt, "ReferencePoint") else kp._any_urls[url]

    orig_unwrap = bt.unwrap

    def unwrap(message):
        if rp_cls is not None and message.type_url == url:
            concrete = rp_cls()
            message.Unpack(concrete)
            return PositionedItem(concrete)
        try:
            return orig_unwrap(message)
        except (NotImplementedError, AssertionError):
            return OpaqueItem(message)

    bt.unwrap = unwrap
    if kb is not None and hasattr(kb, "unwrap"):
        kb.unwrap = unwrap

    def _pack(self):
        del self._proto.items[:]
        for item in self._unwrapped_items:
            if isinstance(item, OpaqueItem):
                self._proto.items.append(item.raw)
            else:
                a = Any()
                a.Pack(item.proto)
                self._proto.items.append(a)

    bt.Footprint._pack = _pack

    # Footprint moves/rotations must carry the compat items along (children are absolute).
    FI = bt.FootprintInstance
    pos_prop, rot_prop = FI.position, FI.orientation

    def set_position(self, position):
        old = self.position
        pos_prop.fset(self, position)
        dx, dy = position.x - old.x, position.y - old.y
        for it in self.definition.items:
            if isinstance(it, PositionedItem):
                it.move(dx, dy)

    def set_orientation(self, orientation):
        old = self.orientation.degrees
        rot_prop.fset(self, orientation)
        delta = self.orientation.degrees - old
        c = self.position
        for it in self.definition.items:
            if isinstance(it, PositionedItem):
                it.rotate(delta, c.x, c.y)

    FI.position = property(pos_prop.fget, set_position)
    FI.orientation = property(rot_prop.fget, set_orientation)
