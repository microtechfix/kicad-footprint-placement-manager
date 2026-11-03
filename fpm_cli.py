"""Command-line / offline use.

  python fpm_cli.py board.kicad_pcb                 # print blocks and metrics
  python fpm_cli.py board.kicad_pcb --place         # also generate a proposal and compare metrics
  python fpm_cli.py board.kicad_pcb --ui            # open the review UI on the file (read-only)
  python fpm_cli.py --live                          # open the UI on the board open in KiCad
                                                    # (needs KiCad's API server enabled)
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser(description="KiCad Footprint Placement Manager")
    ap.add_argument("board", nargs="?", help=".kicad_pcb file (offline mode)")
    ap.add_argument("--live", action="store_true", help="connect to the running KiCad instead of a file")
    ap.add_argument("--ui", action="store_true", help="open the interface (native window; --browser for the web version)")
    ap.add_argument("--place", action="store_true", help="generate a placement proposal")
    ap.add_argument("--scope", default="all", choices=["all", "new"])
    ap.add_argument("--out", help="write the proposal (ref -> x, y, rot) to this JSON file")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--browser", action="store_true", help="use the web browser instead of a native window")
    a = ap.parse_args()

    if a.live:
        from fpm.kicad_ipc import LiveBackend
        backend = LiveBackend()
    elif a.board:
        from fpm.kicad_ipc import FileBackend
        backend = FileBackend(a.board)
    else:
        ap.error("give a .kicad_pcb file or --live")

    if a.ui or a.live:
        if not a.browser:
            try:
                from fpm import gui
                from fpm.session import Session
                gui.run(Session(backend))
                return
            except ImportError:
                print("wxPython not available; using the browser interface")
        from fpm.server import serve
        serve(backend, open_browser=not a.no_browser, port=a.port, ui="browser")
        return

    from fpm.session import Session
    s = Session(backend)
    s.analyze()
    print(f"\n{s.board.name}: {len(s.board.components)} footprints, {len(s.blocks)} blocks\n")
    for b in sorted(s.blocks.values(), key=lambda b: -len(b.members)):
        rv = [m for m in b.members if s.assign[m].confidence < 0.6]
        print(f"  {b.name:48s} {len(b.members):3d} parts" + (f"   review: {' '.join(rv)}" if rv else ""))
    if a.place:
        s.propose({"scope": a.scope})
        keys = ["signal_ratsnest_mm", "all_ratsnest_mm", "crossings", "congestion_max", "congestion_p95",
                "overlaps"]
        print(f"\n  {'metric':24s} {'current':>10s} {'proposal':>10s}")
        for k in keys:
            print(f"  {k:24s} {s.metrics_before[k]:>10} {s.metrics_after[k]:>10}")
        if a.out:
            with open(a.out, "w") as f:
                json.dump(s.proposal, f, indent=1)
            print(f"\n  proposal written to {a.out}")


if __name__ == "__main__":
    main()
