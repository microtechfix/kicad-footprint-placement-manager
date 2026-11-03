"""Check the live KiCad bridge against a mock of the kicad-python API.

The mock board is built from a .kicad_pcb, so LiveBackend.load() must reproduce
the file loader's model, and apply() must issue a single commit.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "fake_kipy"))
sys.path.insert(0, os.path.dirname(HERE))

import kipy
from fpm import pcb_file
from fpm.kicad_ipc import LiveBackend
from fpm.session import Session


def run(path):
    ref_model = pcb_file.load(path)
    kipy.FAKE_BOARD = kipy.FakeBoard(ref_model)
    be = LiveBackend()
    m = be.load()
    assert set(m.components) == set(ref_model.components)
    worst = 0.0
    for r, c in m.components.items():
        o = ref_model.components[r]
        assert abs(c.x - o.x) < 1e-5 and abs(c.y - o.y) < 1e-5 and c.side == o.side
        for p, q in zip(c.pads, o.pads):
            assert p.net == q.net and p.number == q.number
            worst = max(worst, abs(p.lx - q.lx), abs(p.ly - q.ly))
        for a, b in zip((c.xmin, c.ymin, c.xmax, c.ymax), (o.xmin, o.ymin, o.xmax, o.ymax)):
            worst = max(worst, abs(a - b))
    assert worst < 1e-4, worst
    assert m.outline and abs(m.outline.x0 - ref_model.outline.x0) < 1e-5

    s = Session(be)
    s.settings_path  # noqa
    s.analyze()
    s.propose({"scope": "all", "effort": 0.4, "time_limit": 10})
    n = s.apply({})
    fb = kipy.FAKE_BOARD
    assert fb.commits[0] == "open" and fb.commits[-1].startswith("Footprint Placement Manager"), fb.commits
    assert len(fb.updated) == n
    fp = fb.updated[0]
    ref = fp.reference_field.text.value
    assert abs(fp.position.x / 1e6 - s.board.components[ref].x) < 1e-3
    be.select(["U1"] if "U1" in m.components else list(m.components)[:1])
    assert be.get_selection()
    s.undo()
    assert m.copper_layers == 4 and m.track_count == 3, (m.copper_layers, m.track_count)
    s.propose({"scope": "all", "effort": 0.2, "time_limit": 5})
    fb.commits.clear()
    s.apply({"remove_tracks": True})
    assert fb.removed == ["t1", "t2", "v1"] and fb.commits[-1] == "refill", fb.commits
    print(f"  load matches file loader (max diff {worst:.2e} mm); applied {n} in one commit; undo ok")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        print(os.path.basename(p))
        run(p)
    print("OK")
