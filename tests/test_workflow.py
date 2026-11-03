"""End-to-end workflow test on KiCad demo boards (offline backend).

Run:  python tests/test_workflow.py path/to/board.kicad_pcb
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fpm.kicad_ipc import FileBackend
from fpm.session import Session


def run(src):
    tmp = tempfile.mkdtemp()
    for f in os.listdir(os.path.dirname(src)):
        p = os.path.join(os.path.dirname(src), f)
        if os.path.isfile(p):
            shutil.copy(p, tmp)
    path = os.path.join(tmp, os.path.basename(src))
    s = Session(FileBackend(path))
    import fpm.session as fs
    fs.PLUGIN_DIR = tmp                      # keep templates out of the repo
    s.analyze()
    assert s.blocks and len(s.assign) == len(s.board.components)
    blk = sorted(s.blocks.values(), key=lambda b: -len(b.members))[0]
    other = sorted(s.blocks.values(), key=lambda b: -len(b.members))[1]

    # review edits
    s.edit("rename", {"block": blk.id, "name": "Main block"})
    assert s.blocks[blk.id].name == "Main block"
    mover = [m for m in other.members if m != other.anchor][:1] or other.members[:1]
    s.edit("reassign", {"refs": mover, "block": blk.id})
    assert s.assign[mover[0]].block == blk.id and s.assign[mover[0]].evidence == "manual"
    s.edit("split", {"refs": mover, "name": "Split test"})
    nb = s.assign[mover[0]].block
    assert s.blocks[nb].name == "Split test"
    s.edit("merge", {"blocks": [blk.id, nb]})
    assert s.assign[mover[0]].block == blk.id
    s.edit("lock", {"refs": [blk.anchor], "value": True})
    assert blk.anchor in s.locked

    # constraints: region for a block + frozen block
    o = s.board.outline
    reg = {"x0": o.x0 + 1, "y0": o.y0 + 1, "x1": (o.x0 + o.x1) / 2, "y1": o.y1 - 1}
    s.edit("region", {"block": other.id, "region": reg})
    s.edit("freeze", {"block": blk.id, "value": True})

    # persistence round trip
    s2 = Session(FileBackend(path))
    s2.analyze()
    assert s2.blocks[blk.id].name == "Main block" and blk.anchor in s2.locked and s2.blocks[blk.id].frozen
    assert s2.blocks[other.id].region is not None

    # proposal
    s.propose({"scope": "all", "effort": 0.6, "time_limit": 20})
    frozen_moved = [r for r in s.proposal if r in s.blocks[blk.id].members]
    assert not frozen_moved, f"frozen block moved: {frozen_moved}"
    assert blk.anchor not in s.proposal
    R = s.blocks[other.id].region
    for m in s.blocks[other.id].members:
        if m in s.proposal:
            x, y, r = s.proposal[m]
            bb = s.board.components[m].bbox(x, y, r)
            if not R.contains(*bb, tol=0.3):
                assert any(m in l and "No legal spot" in l for l in s.logs), f"{m} left its region silently"
                print(f"  (reported) {m} could not fit its region")
    mb, ma = s.metrics_before, s.metrics_after
    print(f"  ratsnest {mb['signal_ratsnest_mm']} -> {ma['signal_ratsnest_mm']}, "
          f"crossings {mb['crossings']} -> {ma['crossings']}, overlaps {mb['overlaps']} -> {ma['overlaps']}")

    # manual tweak, template + replicate
    r0 = next(iter(s.proposal))
    x, y, rot = s.proposal[r0]
    s.edit_proposal({"moves": {r0: [x + 1, y, rot + 90]}})
    assert abs(s.proposal[r0][0] - (x + 1)) < 1e-6
    s.save_template(other.id, "test tpl")
    assert s.state()["templates"]
    s.apply_template(other.id, "test_tpl.json")
    reps = s.state()["repeated"]
    if reps:
        s.replicate(reps[0][0], reps[0])

    # apply chosen block, then everything, then undo twice
    s.edit("freeze", {"block": blk.id, "value": False})
    n1 = s.apply({"blocks": [other.id]}) if any(m in s.proposal for m in s.blocks[other.id].members) else 0
    n2 = s.apply({}) if s.proposal else 0
    for _ in range(bool(n1) + bool(n2)):
        s.undo()
    st = s.state()
    assert st["undo_depth"] == 0
    s.propose({"scope": "new"})
    print(f"  applied {n1}+{n2}, undo ok; 'new' scope moved {len(s.proposal)} (expect 0 when all on board)")
    return True


if __name__ == "__main__":
    for b in sys.argv[1:]:
        print(os.path.basename(b))
        run(b)
    print("OK")
