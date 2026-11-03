"""AI placement: Claude plan (mocked API), hints reaching the placer, learned library and corrections.

Run:  python tests/test_ai.py path/to/board.kicad_pcb
"""
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["FPM_HOME"] = tempfile.mkdtemp()          # never touch the real ~/.fpm

from fpm import ai  # noqa: E402
from fpm.kicad_ipc import FileBackend  # noqa: E402
from fpm.session import Session  # noqa: E402
from fpm.classify import role  # noqa: E402


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def run(src):
    tmp = tempfile.mkdtemp()
    shutil.copy(src, tmp)
    for ext in (".kicad_pro", ".kicad_sch"):
        p = os.path.splitext(src)[0] + ext
        if os.path.exists(p):
            shutil.copy(p, tmp)
    s = Session(FileBackend(os.path.join(tmp, os.path.basename(src))))
    s.analyze()
    C = s.board.components
    fixed = s._fixed_refs()
    movable = [r for r in C if r not in fixed]
    ic = next((r for r in movable if role(C[r]) == "ic"), None) or next(r for r in C if role(C[r]) == "ic")
    small = [r for r in movable if role(C[r]) == "passive"][:2]
    linked = {r for r in movable if role(C[r]) == "passive" and ai.master_link(s.board, r, s.blocks, s.assign)}
    blk = sorted(s.blocks.values(), key=lambda b: -len(b.members))
    desc = ai.describe(s.board, s.blocks, s.assign, s.pin_names, fixed)
    assert "PARTS" in desc and ic in desc

    plan = {
        "blocks": [{"block_id": blk[0].id, "name": "Main section", "master": ic if ic in blk[0].members else "",
                    "function": "test"}],
        "near": [{"ref": small[0], "target": ic, "max_mm": 2, "reason": "decoupling"}],
        "edge": [{"ref": small[1], "side": "left", "reason": "test edge"}],
        "zones": [{"block_id": blk[0].id, "area": "top-left", "reason": "test"}],
        "apart": [{"a": blk[0].id, "b": blk[1].id, "gap_mm": 4, "reason": "noise"}],
        "notes": "Test notes.",
    }
    sent = {}

    def fake_urlopen(req, timeout=0):
        sent["body"] = json.loads(req.data.decode())
        sent["headers"] = dict(req.header_items())
        return _Resp(json.dumps({"content": [{"type": "tool_use", "name": "submit_placement_plan", "input": plan}],
                                 "usage": {"input_tokens": 1, "output_tokens": 1}}).encode())

    ai.urllib.request.urlopen = fake_urlopen
    ai.save_config({"anthropic_api_key": "sk-test", "model": "claude-sonnet-5-5"})
    sug = s.ai_suggest("claude")
    assert sent["body"]["tool_choice"]["name"] == "submit_placement_plan"
    assert sent["headers"].get("X-api-key") == "sk-test", sent["headers"]
    kinds = {x["kind"] for x in sug}
    assert {"near", "edge", "zone", "apart", "name"} <= kinds, kinds
    assert "Test notes" in s.ai_notes

    x0 = s.board.outline.x0

    def gap_after(use):
        s.ai_use = use
        s.propose({"scope": "all", "effort": 0.4, "time_limit": 10})
        pos = {**s.board.positions(), **s.proposal}
        return C[small[1]].bbox(*pos[small[1]])[0] - x0
    plain = gap_after(False)
    edge_gap = gap_after(True)
    if not (edge_gap < 3.0 or edge_gap < plain - 1.0):
        from fpm import placer
        pl = placer.Placer(s.board, s.blocks, s.assign, s.rules, placer.PlaceOptions(), s.locked)
        assert pl.packed, f"{small[1]} not pulled left ({plain:.2f} -> {edge_gap:.2f} mm) on a board with room"
        print(f"  (board is packed: no free spot on the left edge for {small[1]})")
    print(f"  edge hint on {small[1]}: {plain:.2f} -> {edge_gap:.2f} mm from the left edge")
    assert any(b.name == "Main section" for b in s.blocks.values())

    # plain Auto place must not use AI suggestions; AI auto place must
    s.logs.clear()
    s.auto_place(apply=False)
    assert any("AI suggestions not used" in m for m in s.logs), s.logs[-5:]
    assert not s._hints_in_use

    # settings written by another plugin copy survive our saves
    with open(s.settings_path, encoding="utf-8") as f:
        d = json.load(f)
    d["other_copy"] = {"x": 1}
    d["options"]["legacy_only_option"] = 7
    with open(s.settings_path, "w", encoding="utf-8") as f:
        json.dump(d, f)
    s.save()
    with open(s.settings_path, encoding="utf-8") as f:
        d = json.load(f)
    assert d["other_copy"] == {"x": 1} and d["options"]["legacy_only_option"] == 7

    # learned: teach the proposal, then the library suggests it back
    s.apply({})
    s.board.name = "teacher"
    n = s.learn_board()
    assert n > 0
    lib_sug = ai.Library().suggest(s.board, s.blocks, s.assign, s.pin_names, set(movable))
    assert any(x["kind"] == "near" and "offset" in x["data"] for x in lib_sug)

    # corrections: a part put back where it was before Apply (Ctrl+Z) is NOT learned ...
    import math
    passives = sorted(x for x in s.last_applied if x in linked and
                      math.hypot(s.last_applied[x][0] - s.last_applied[x][3],
                                 s.last_applied[x][1] - s.last_applied[x][4]) > 1.0)
    back = passives[1]
    v = s.last_applied[back]
    C[back].x, C[back].y, C[back].rot = v[3], v[4], v[5]
    s.backend.overrides[back] = (v[3], v[4], v[5])
    # ... a part moved somewhere new is learned with weight 2
    r = passives[0]
    C[r].x += 1.5
    s.backend.overrides[r] = (C[r].x, C[r].y, C[r].rot)
    s.analyze(reload=True)
    lib = ai.Library()
    assert any(smp[4] > 1 for v in lib.d["rel"].values() for smp in v), "correction not learned"
    assert any("put back where they were" in m for m in s.logs), "revert not recognised"
    print(f"  {os.path.basename(src)}: {len(sug)} Claude suggestions, edge gap {edge_gap:.2f} mm, "
          f"learned {n}, {len(lib_sug)} learned suggestions, correction learned")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        run(p)
    print("OK")
