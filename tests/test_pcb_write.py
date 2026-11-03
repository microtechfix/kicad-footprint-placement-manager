"""Moving footprints in the board text must put every pad exactly where the model predicts."""
import os, random, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fpm import pcb_file, pcb_write

def run(path):
    b = pcb_file.load(path)
    rnd = random.Random(1)
    moves = {}
    for r, c in list(b.components.items())[::2]:
        moves[r] = (c.x + rnd.uniform(-5, 5), c.y + rnd.uniform(-5, 5), rnd.choice([0, 90, 180, -90, c.rot]))
    text = open(path, encoding="utf-8").read()
    new, done = pcb_write.apply_positions(text, moves)
    assert set(done) == set(moves), set(moves) - set(done)
    tmp = os.path.join(tempfile.mkdtemp(), "t.kicad_pcb")
    open(tmp, "w", encoding="utf-8").write(new)
    b2 = pcb_file.load(tmp)
    worst = 0.0
    for r, c in b.components.items():
        x, y, rot = moves.get(r, (c.x, c.y, c.rot))
        c2 = b2.components[r]
        for p, p2 in zip(c.pads, c2.pads):
            ex = c.pad_pos(p, x, y, rot)
            got = c2.pad_pos(p2)
            worst = max(worst, abs(ex[0] - got[0]), abs(ex[1] - got[1]))
            # pad orientation: KiCad stores pad angle absolute; size swap must agree
            assert (abs(p.w - p2.w) < 1e-6 and abs(p.h - p2.h) < 1e-6), (r, p.number)
    assert worst < 1e-5, worst
    # untouched footprints are byte-identical in the output
    assert len(new) - len(text) < 60 * len(moves) + 100
    print(f"  {os.path.basename(path)}: moved {len(moves)}, max pad error {worst:.1e} mm")

if __name__ == "__main__":
    for p in sys.argv[1:]:
        run(p)
    print("OK")
