# Changelog

## 0.7.0 (2026-11-10)

- **Fix ground.** For a routed board whose ground pour has been cut into pieces. The new
  **Fix ground…** button (Check and fix panel) and `fpm_cli.py board.kicad_pcb --fix-ground`:
  - join every piece of the pour to the rest with a stitching via, including thermal vias in the
    exposed pads of QFN-style parts;
  - connect ground pads the fill does not reach, with a short track to the fill or to a new via,
    routed around other nets when the pad is boxed in;
  - switch pads that only got one thermal spoke to a solid zone connection (fixed copy only);
  - list what still needs a manual fix, with coordinates.
  Every via and track is checked against the board's clearance, hole and edge rules on all copper
  layers. Live in KiCad it is one undoable commit; offline it writes `<board>-gndfix.kicad_pcb`
  and never touches the original.

## 0.6.0 (2026-11-04)

- **Never makes your layout worse.** When the parts are already on the board, Propose tries a
  fresh placement and a refinement of your layout, puts back any part that would overlap or leave
  the board, and keeps whichever scores best. If nothing beats your layout, it changes nothing.
  Option: `keep_best` (on by default).
- **Fewer ratsnest crossings.** The optimiser now counts crossing connections, worth 4 mm of
  connection length each. Option: `crossing_weight` (0 turns it off).
- **Benchmark.** `tools/benchmark.py` runs the placer on copies of real boards and reports
  before/after numbers. On 8 KiCad demo boards, improving the designer's own layout shortened
  signal connections by a median of 12.5 %, with no new overlaps. See `benchmarks/summary.md`.

## 0.5.0

- First public release for KiCad 10 (IPC API plugin).
