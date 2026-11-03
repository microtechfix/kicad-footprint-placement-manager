"""Check-and-fix loop on demo boards (built-in checks; KiCad DRC needs kicad-cli on the machine)."""
import os, shutil, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fpm.session as fs
from fpm.session import Session
from fpm.kicad_ipc import FileBackend
from fpm import drc

def run(src, tight):
    tmp = tempfile.mkdtemp()
    for f in os.listdir(os.path.dirname(src)):
        p = os.path.join(os.path.dirname(src), f)
        if os.path.isfile(p):
            shutil.copy(p, tmp)
    fs.PLUGIN_DIR = tmp
    s = Session(FileBackend(os.path.join(tmp, os.path.basename(src))))
    s.analyze()
    # deliberately cramped proposal: no routing room, so the checks find problems
    opts = {"scope": "all", "effort": 0.7, "time_limit": 20}
    if tight is not None:     # deliberately cramped: no routing room, so the checks find problems
        opts.update({"clearance": tight, "routing_space": 0.0, "escape_gap": 0.0})
    s.propose(opts)
    before = s.check(kicad=False)
    t = time.time()
    after = s.check_and_fix(rounds=4, kicad=False)
    e0 = [v for v in before if v.severity == "error"]
    e1 = [v for v in after if v.severity == "error"]
    kinds = lambda vs: {k: sum(1 for v in vs if v.type == k) for k in sorted({v.type for v in vs})}
    print(f"  {os.path.basename(src)}: before {kinds(before)} -> after {kinds(after)}  ({time.time()-t:.1f}s)")
    assert len(e1) <= len(e0)
    return e0, e1

def test_parse():
    report = {"violations": [
        {"type": "courtyards_overlap", "severity": "error", "description": "Courtyards overlap",
         "items": [{"description": "Footprint C3", "pos": {"x": 10, "y": 5}}, {"description": "Footprint U1"}]},
        {"type": "clearance", "severity": "error", "description": "Clearance violation (netclass 'Default' clearance 0.2000 mm; actual 0.1000 mm)",
         "items": [{"description": "Pad 2 [GND] of C3 on F.Cu", "pos": {"x": 1, "y": 2}},
                   {"description": "Pad 1 [/VCC] of R7 on F.Cu"}]},
        {"type": "clearance", "severity": "error", "description": "Clearance violation",
         "items": [{"description": "Track [GND] on F.Cu, length 3.1 mm"}, {"description": "Pad 1 of R7 on F.Cu"}]},
        {"type": "silk_overlap", "severity": "warning", "description": "Silkscreen overlap", "items": []},
    ], "unconnected_items": [{"type": "unconnected_items"}]}
    vs = drc.parse_report(report, {"C3", "U1", "R7"})
    assert [v.type for v in vs] == ["courtyards_overlap", "clearance"], vs
    assert vs[0].refs == ["C3", "U1"] and vs[1].refs == ["C3", "R7"]
    print("  KiCad report parsing OK (track and silk items ignored, unconnected ignored)")

if __name__ == "__main__":
    test_parse()
    tight = 0.0 if os.environ.get("CRAMPED") else None
    for p in sys.argv[1:]:
        run(p, tight)
    print("OK")
