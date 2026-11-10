"""Ground fix on a small hand-made board, and on any real boards given as arguments.

Run:  python tests/test_ground.py [board.kicad_pcb ...]

The built-in board is 20 x 20 mm, two layers. The back is one solid GND fill. The front GND
fill is cut in two by a signal track; C1's ground pad sits in the right half with one spoke,
and R1's ground pad is boxed in by tracks with no fill around it.
"""
import math
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "fake_kipy"))
sys.path.insert(0, os.path.dirname(HERE))

from fpm import ground, sexpr


def _poly(layer, pts, island=False):
    xy = " ".join(f"(xy {x} {y})" for x, y in pts)
    return f'\t\t(filled_polygon\n\t\t\t(layer "{layer}")\n{"\t\t\t(island yes)\n" if island else ""}\t\t\t(pts {xy})\n\t\t)\n'


def _fp(ref, x, y, pads):
    out = (f'\t(footprint "T:{ref}"\n\t\t(layer "F.Cu")\n\t\t(uuid "{ref}-u")\n\t\t(at {x} {y})\n'
           f'\t\t(property "Reference" "{ref}"\n\t\t\t(at 0 0 0)\n\t\t\t(layer "F.SilkS")\n\t\t)\n')
    for num, lx, ly, net in pads:
        out += (f'\t\t(pad "{num}" smd rect\n\t\t\t(at {lx} {ly})\n\t\t\t(size 0.6 0.6)\n'
                f'\t\t\t(layers "F.Cu" "F.Mask" "F.Paste")\n\t\t\t(net "{net}")\n\t\t\t(uuid "{ref}-{num}")\n\t\t)\n')
    return out + "\t)\n"


def _seg(x1, y1, x2, y2, net, layer="F.Cu"):
    return (f'\t(segment\n\t\t(start {x1} {y1})\n\t\t(end {x2} {y2})\n\t\t(width 0.2)\n\t\t(layer "{layer}")\n'
            f'\t\t(net "{net}")\n\t\t(uuid "s{x1}{y1}{x2}{y2}")\n\t)\n')


def board_text():
    left = [(0.3, 0.3), (9.3, 0.3), (9.3, 19.7), (0.3, 19.7)]
    # right half, with a hole for R1's boxed-in area (keyhole outline, as KiCad writes fills)
    right = [(10.7, 0.3), (19.7, 0.3), (19.7, 19.7), (10.7, 19.7), (10.7, 12.6), (12.6, 12.6), (12.6, 17.4),
             (17.4, 17.4), (17.4, 12.6), (10.7, 12.6)]
    back = [(0.3, 0.3), (19.7, 0.3), (19.7, 19.7), (0.3, 19.7)]
    t = ('(kicad_pcb\n\t(version 20260206)\n\t(generator "test")\n\t(layers\n\t\t(0 "F.Cu" signal)\n'
         '\t\t(2 "B.Cu" signal)\n\t\t(25 "Edge.Cuts" user)\n\t)\n')
    t += _fp("C1", 15, 5, [("1", -0.5, 0, "SIG_A"), ("2", 0.5, 0, "GND")])
    t += _fp("R1", 15, 15, [("1", -0.5, 0, "SIG_B"), ("2", 0.5, 0, "GND")])
    t += _fp("C2", 5, 5, [("1", -0.5, 0, "SIG_A"), ("2", 0.5, 0, "GND")])
    for a, b in (((0, 0), (20, 0)), ((20, 0), (20, 20)), ((20, 20), (0, 20)), ((0, 20), (0, 0))):
        t += (f'\t(gr_line\n\t\t(start {a[0]} {a[1]})\n\t\t(end {b[0]} {b[1]})\n\t\t(stroke\n\t\t\t(width 0.1)\n'
              f'\t\t\t(type default)\n\t\t)\n\t\t(layer "Edge.Cuts")\n\t\t(uuid "e{a[0]}{a[1]}")\n\t)\n')
    t += _seg(10, 0.5, 10, 19.5, "SIG_C")                       # cuts the front fill in two
    for a, b in (((13, 13), (17, 13)), ((17, 13), (17, 17)), ((17, 17), (13, 17)), ((13, 17), (13, 13.6))):
        t += _seg(*a, *b, "SIG_B")                               # a ring of track around R1 with one gap
    t += ('\t(zone\n\t\t(net "GND")\n\t\t(layers "F.Cu" "B.Cu")\n\t\t(uuid "z1")\n\t\t(name "GND")\n'
          '\t\t(connect_pads\n\t\t\t(clearance 0.5)\n\t\t)\n\t\t(min_thickness 0.25)\n'
          '\t\t(fill yes\n\t\t\t(thermal_gap 0.5)\n\t\t\t(thermal_bridge_width 0.5)\n\t\t)\n'
          '\t\t(polygon\n\t\t\t(pts (xy 0 0) (xy 20 0) (xy 20 20) (xy 0 20))\n\t\t)\n')
    t += _poly("F.Cu", left) + _poly("F.Cu", right) + _poly("B.Cu", back)
    return t + "\t)\n)\n"


def check(text, rules, name):
    gp = ground.plan(text, rules)
    print(f"{name}: {gp.summary()}")
    w = ground._World(text)
    # every proposed via and track must be legal against the untouched board
    for f in gp.vias:
        assert w.via_ok(f.x, f.y, gp.net, rules), f"illegal via at {f.x}, {f.y}"
    for f in gp.tracks:
        assert w.track_ok(f.x, f.y, f.x2, f.y2, f.width, f.layer, gp.net, rules), f"illegal track {f}"
        assert f.width >= rules.min_track
    assert gp.groups_after <= gp.groups_before
    out = ground.write(text, gp)
    root = sexpr.parse(out)                                      # still a well-formed board
    assert root[0] == "kicad_pcb"
    w2 = ground._World(out)
    assert len(w2.vias) == len(w.vias) + len(gp.vias)
    assert len(w2.segs) == len(w.segs) + len(gp.tracks)
    assert sum(1 for p in w2.pads if p.connect == 2) >= len(gp.solids)
    assert len(w2.pads) == len(w.pads)
    # planning again on the fixed board must not stitch the same pieces twice
    again = ground.plan(out, rules)
    assert again.groups_before <= gp.groups_after, (again.groups_before, gp.groups_after)
    assert len(again.vias) <= max(1, len(gp.unsolved)), "the fix is not stable"
    return gp, out


def main():
    rules = ground.GroundRules(clearance=0.15, via_size=0.6, via_drill=0.3, track_width=0.2, min_track=0.15)
    text = board_text()
    gp, out = check(text, rules, "built-in board")
    assert gp.pieces == 3 and gp.groups_before == 3, (gp.pieces, gp.groups_before)
    assert gp.groups_after == 1, gp.unsolved
    assert not gp.unsolved, gp.unsolved
    assert gp.stranded_pads == 1                                 # R1 pad 2
    assert any(f.ref == "R1" for f in gp.tracks), "R1's ground pad needs a track out of its box"
    assert len(gp.vias) >= 2                                     # left half, right half (R1 may bridge by track)
    # nothing may be added on top of the signal track at x = 10
    for f in gp.vias:
        assert abs(f.x - 10) >= 0.3 + 0.1 + 0.15 - 1e-6

    # file round trip: the original is never overwritten
    d = tempfile.mkdtemp()
    src = os.path.join(d, "b.kicad_pcb")
    open(src, "w", encoding="utf-8").write(text)
    gp2, dst = ground.fix_file(src)
    assert dst != src and os.path.exists(dst) and open(src, encoding="utf-8").read() == text
    try:
        ground.fix_file(src, src)
        raise AssertionError("fix_file must refuse to overwrite its input")
    except ValueError:
        pass

    # live backend: one commit with the vias and tracks, then a refill
    import kipy
    from types import SimpleNamespace as NS
    from fpm.kicad_ipc import LiveBackend
    from fpm.session import Session
    fb = NS(text=text, commits=[], created=[], name="b.kicad_pcb")
    fb.get_nets = lambda: [NS(name="GND"), NS(name="SIG_A")]
    fb.get_as_string = lambda: fb.text
    fb.begin_commit = lambda: fb.commits.append("open") or "c"
    fb.create_items = lambda items: fb.created.extend(items)
    fb.push_commit = lambda c, msg: fb.commits.append(msg)
    fb.drop_commit = lambda c: fb.commits.append("drop")
    fb.refill_zones = lambda block=True, max_poll_seconds=60.0: fb.commits.append("refill")
    fb.get_project = lambda: NS(path=d)
    be = LiveBackend()
    be.board = fb
    s = Session(be)
    logs = []
    s.log = logs.append
    r = s.fix_ground({"apply": True})
    assert r["applied"] == "live", logs
    assert len(fb.created) == len(s.ground_plan.vias) + len(s.ground_plan.tracks)
    assert fb.commits.count("open") == 1 and "drop" not in fb.commits and fb.commits[-1] == "refill"
    # an API failure falls back to a copy and leaves no half-open commit
    fb.created.clear(); fb.commits.clear()
    fb.create_items = lambda items: (_ for _ in ()).throw(RuntimeError("boom"))
    r = s.fix_ground({"apply": True})
    assert r["applied"] == "copy" and os.path.exists(r["file"]) and "drop" in fb.commits

    for path in sys.argv[1:]:
        rr = ground.load_rules(os.path.splitext(path)[0] + ".kicad_pro")
        check(open(path, encoding="utf-8").read(), rr, os.path.basename(path))
    print("ground: all checks passed")


if __name__ == "__main__":
    main()
