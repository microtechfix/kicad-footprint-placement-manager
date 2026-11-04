# Changelog

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
