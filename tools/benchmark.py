#!/usr/bin/env python3
"""Benchmark the Footprint Placement Manager on real KiCad boards.

For every board it measures two things, offline and without touching your files
(each board is copied to a temporary folder first):

  improve   start from the designer's own layout and let the tool optimise it
  scratch   throw every part off the board (as after "Update PCB from Schematic"),
            keep only the locked parts, connectors and mounting holes where the
            designer put them, and let the tool place everything from nothing.
            The result is compared with the designer's hand-made layout.

Usage:
  python tools/benchmark.py BOARD_OR_FOLDER [...] [--out bench_out] [--time-limit 25]
                            [--modes improve,scratch] [--write-placed] [--max-parts 600]

Folders are searched recursively for *.kicad_pcb. Results go to --out:
  results.csv / results.json   every number, one row per board and mode
  summary.md                   a table you can paste into a README or show in a video
  placed/                      with --write-placed: the placed boards, ready to open in KiCad
"""
import argparse
import csv
import glob
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fpm import metrics as fm                       # noqa: E402
from fpm import pcb_write                           # noqa: E402
from fpm.kicad_ipc import FileBackend               # noqa: E402
from fpm.session import Session                     # noqa: E402
import fpm.session as fsession                      # noqa: E402

KEEP_PREFIXES = ("J", "P", "CN", "X", "H", "MH", "USB", "SW", "BT", "K")  # placed by hand first
KEYS = ("signal_ratsnest_mm", "all_ratsnest_mm", "crossings", "congestion_p95", "overlaps", "out_of_board")


def find_boards(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            out += sorted(glob.glob(os.path.join(p, "**", "*.kicad_pcb"), recursive=True))
        elif p.endswith(".kicad_pcb"):
            out.append(p)
    # skip backups / our own output
    return [b for b in out if "-backups" not in b and ".fpm-placed" not in b]


def copy_project(board, tmp):
    src = os.path.dirname(os.path.abspath(board))
    for f in os.listdir(src):
        p = os.path.join(src, f)
        if os.path.isfile(p) and f.endswith((".kicad_pcb", ".kicad_sch", ".kicad_pro", ".kicad_dru")):
            shutil.copy(p, tmp)
    return os.path.join(tmp, os.path.basename(board))


def summarise(m):
    return {
        "signal_ratsnest_mm": m["signal_ratsnest_mm"],
        "all_ratsnest_mm": m["all_ratsnest_mm"],
        "crossings": m["crossings"],
        "congestion_p95": m["congestion_p95"],
        "overlaps": m["overlaps"],
        "out_of_board": len(m["out_of_board"]),
    }


def kept_in_scratch(c):
    pre = c.prefix if hasattr(c, "prefix") else c.ref.rstrip("0123456789")
    return c.locked or pre.upper().startswith(KEEP_PREFIXES)


def scramble(board):
    """Pile all movable parts in a grid below the board, like a fresh schematic import."""
    o = board.outline
    x0 = o.x0 if o else 0.0
    y = (o.y1 if o else 0.0) + 15.0
    x, row_h, moved = x0, 0.0, []
    width = (o.x1 - o.x0) if o else 100.0
    for ref in sorted(board.components):
        c = board.components[ref]
        if kept_in_scratch(c) or c.dnp:
            continue
        w, h = (c.xmax - c.xmin) + 1.0, (c.ymax - c.ymin) + 1.0
        if x + w > x0 + width:
            x, y, row_h = x0, y + row_h, 0.0
        c.x, c.y, c.rot = x + w / 2, y + h / 2, 0.0
        x += w
        row_h = max(row_h, h)
        moved.append(ref)
    return moved


def run_board(board, args, placed_dir):
    rows = []
    tmp = tempfile.mkdtemp(prefix="fpm_bench_")
    try:
        path = copy_project(board, tmp)
        fsession.PLUGIN_DIR = tmp            # keep learned templates out of the plugin folder
        for mode in args.modes:
            t0 = time.time()
            s = Session(FileBackend(path))
            s.analyze()
            t_an = time.time() - t0
            comps = s.board.components
            n = len(comps)
            if n < args.min_parts or n > args.max_parts:
                rows.append({"board": os.path.basename(board), "mode": mode, "parts": n,
                             "status": f"skipped ({n} parts)"})
                break
            human = summarise(fm.evaluate(s.board, s.rules, {}))
            opts = {"scope": "all", "time_limit": args.time_limit, "effort": args.effort,
                    "seed": args.seed}
            moved_start = []
            if mode == "scratch":
                moved_start = scramble(s.board)
                opts["keep_connectors_fixed"] = True
            start = summarise(fm.evaluate(s.board, s.rules, {}))
            t1 = time.time()
            s.propose(opts)
            t_place = time.time() - t1
            after = summarise(fm.evaluate(s.board, s.rules, s.proposal))
            row = {"board": os.path.basename(board), "mode": mode, "parts": n,
                   "blocks": len(s.blocks), "moved": len(s.proposal),
                   "scrambled": len(moved_start), "analyze_s": round(t_an, 2),
                   "place_s": round(t_place, 2), "status": "ok"}
            for k in KEYS:
                row[f"human_{k}"] = human[k]
                row[f"start_{k}"] = start[k]
                row[f"after_{k}"] = after[k]
            rows.append(row)
            if args.write_placed and s.proposal:
                text = open(path, encoding="utf-8").read()
                moves = dict(s.proposal)
                new_text, _ = pcb_write.apply_positions(text, moves)
                stem = os.path.splitext(os.path.basename(board))[0]
                dst = os.path.join(placed_dir, f"{stem}.fpm-placed-{mode}")
                os.makedirs(dst, exist_ok=True)
                for f in os.listdir(tmp):
                    if f.endswith((".kicad_sch", ".kicad_pro", ".kicad_dru")):
                        shutil.copy(os.path.join(tmp, f), dst)
                with open(os.path.join(dst, os.path.basename(path)), "w", encoding="utf-8") as fh:
                    fh.write(new_text)
    except Exception as e:  # keep going: one broken board must not stop the run
        rows.append({"board": os.path.basename(board), "mode": ",".join(args.modes),
                     "status": f"error: {type(e).__name__}: {e}"})
        if args.verbose:
            traceback.print_exc()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return rows


def pct(a, b):
    return None if not a else round(100.0 * (b - a) / a, 1)


def write_summary(rows, out):
    ok = [r for r in rows if r.get("status") == "ok"]
    L = ["# Footprint Placement Manager benchmark", ""]
    for mode, title, ref in (("improve", "Improving the designer's own layout", "human"),
                             ("scratch", "Placing from scratch vs. the designer's hand layout", "human")):
        mr = [r for r in ok if r["mode"] == mode]
        if not mr:
            continue
        L += [f"## {title}", "",
              "| Board | Parts | Ratsnest (mm) | Change | Crossings | Overlaps | Off board | Time (s) |",
              "|---|---:|---:|---:|---:|---:|---:|---:|"]
        ch = []
        for r in mr:
            a, b = r[f"{ref}_signal_ratsnest_mm"], r["after_signal_ratsnest_mm"]
            c = pct(a, b)
            if c is not None:
                ch.append(c)
            L.append(f"| {r['board']} | {r['parts']} | {a} → {b} | {c:+.1f} % | "
                     f"{r[f'{ref}_crossings']} → {r['after_crossings']} | "
                     f"{r[f'{ref}_overlaps']} → {r['after_overlaps']} | "
                     f"{r[f'{ref}_out_of_board']} → {r['after_out_of_board']} | {r['place_s']} |")
        if ch:
            better = sum(1 for c in ch if c < 0)
            L += ["", f"**Median signal ratsnest change: {statistics.median(ch):+.1f} %** "
                      f"(shorter on {better} of {len(ch)} boards). "
                      f"No new overlaps or off-board parts: "
                      f"{sum(1 for r in mr if r['after_overlaps'] <= r[f'{ref}_overlaps'] and r['after_out_of_board'] <= r[f'{ref}_out_of_board'])}"
                      f" of {len(mr)}.", ""]
    bad = [r for r in rows if r.get("status") != "ok"]
    if bad:
        L += ["## Not measured", ""] + [f"- {r['board']}: {r['status']}" for r in bad] + [""]
    L += ["Ratsnest = total straight-line length of all signal connections (power and ground excluded).",
          "Lower is better: shorter, simpler routing. Crossings = ratsnest lines that cross each other.",
          "Off board counts parts outside the outline (logos and fiducials often are, even in the original).",
          "Scratch mode keeps locked parts, connectors and mounting holes where the designer put them."]
    open(os.path.join(out, "summary.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--out", default="bench_out")
    ap.add_argument("--modes", default="improve,scratch")
    ap.add_argument("--time-limit", type=float, default=25.0)
    ap.add_argument("--effort", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--min-parts", type=int, default=5)
    ap.add_argument("--max-parts", type=int, default=600)
    ap.add_argument("--write-placed", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    args.modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    os.makedirs(args.out, exist_ok=True)
    placed = os.path.join(args.out, "placed")
    boards = find_boards(args.paths)
    if not boards:
        ap.error("no .kicad_pcb files found")
    rows = []
    for i, b in enumerate(boards, 1):
        print(f"[{i}/{len(boards)}] {os.path.relpath(b)}", flush=True)
        for r in run_board(b, args, placed):
            rows.append(r)
            if r.get("status") == "ok":
                print(f"    {r['mode']:8s} {r['parts']:4d} parts  ratsnest "
                      f"{r['human_signal_ratsnest_mm']} -> {r['after_signal_ratsnest_mm']} mm, "
                      f"overlaps {r['after_overlaps']}, {r['place_s']} s", flush=True)
            else:
                print(f"    {r['status']}", flush=True)
    cols = sorted({k for r in rows for k in r}, key=lambda k: (k not in ("board", "mode", "status", "parts"), k))
    with open(os.path.join(args.out, "results.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    json.dump(rows, open(os.path.join(args.out, "results.json"), "w"), indent=1)
    print("\n" + write_summary(rows, args.out))


if __name__ == "__main__":
    main()
