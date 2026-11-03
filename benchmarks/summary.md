# Footprint Placement Manager benchmark

KiCad 10 demo boards, `python tools/benchmark.py <demos> --time-limit 15`, seed 1.

## Improving the designer's own layout

| Board | Parts | Ratsnest (mm) | Change | Crossings | Overlaps | Off board | Time (s) |
|---|---:|---:|---:|---:|---:|---:|---:|
| pic_programmer.kicad_pcb | 63 | 980.9 → 851.6 | -13.2 % | 14 → 25 | 1 → 0 | 8 → 8 | 1.94 |
| ecc83-pp.kicad_pcb | 15 | 153.5 → 173.3 | +12.9 % | 1 → 4 | 1 → 0 | 1 → 1 | 0.24 |
| ecc83-pp_v2.kicad_pcb | 15 | 155.3 → 149.1 | -4.0 % | 4 → 2 | 1 → 2 | 0 → 1 | 0.43 |
| interf_u.kicad_pcb | 25 | 3347.7 → 3735.4 | +11.6 % | 349 → 526 | 0 → 0 | 1 → 1 | 1.58 |
| complex_hierarchy.kicad_pcb | 68 | 820.5 → 858.9 | +4.7 % | 28 → 46 | 0 → 0 | 1 → 1 | 2.01 |
| video.kicad_pcb | 189 | 27959.5 → 34589.6 | +23.7 % | 8695 → 14990 | 26 → 0 | 7 → 11 | 33.1 |

**Median signal ratsnest change: +8.2 %** (shorter on 2 of 6 boards). No new overlaps or off-board parts: 4 of 6.

## Placing from scratch vs. the designer's hand layout

| Board | Parts | Ratsnest (mm) | Change | Crossings | Overlaps | Off board | Time (s) |
|---|---:|---:|---:|---:|---:|---:|---:|
| pic_programmer.kicad_pcb | 63 | 980.9 → 795.8 | -18.9 % | 14 → 15 | 1 → 0 | 8 → 8 | 2.06 |
| ecc83-pp.kicad_pcb | 15 | 153.5 → 191.7 | +24.9 % | 1 → 5 | 1 → 0 | 1 → 1 | 0.25 |
| ecc83-pp_v2.kicad_pcb | 15 | 155.3 → 158.7 | +2.2 % | 4 → 5 | 1 → 0 | 0 → 0 | 0.32 |
| interf_u.kicad_pcb | 25 | 3347.7 → 2898.0 | -13.4 % | 349 → 452 | 0 → 0 | 1 → 1 | 1.55 |
| complex_hierarchy.kicad_pcb | 68 | 820.5 → 776.7 | -5.3 % | 28 → 31 | 0 → 0 | 1 → 1 | 2.12 |
| video.kicad_pcb | 189 | 27959.5 → 33654.9 | +20.4 % | 8695 → 13252 | 26 → 0 | 7 → 7 | 22.84 |

**Median signal ratsnest change: -1.5 %** (shorter on 3 of 6 boards). No new overlaps or off-board parts: 6 of 6.

Ratsnest = total straight-line length of all signal connections (power and ground excluded).
Lower is better: shorter, simpler routing. Crossings = ratsnest lines that cross each other.
Off board counts parts outside the outline (logos and fiducials often are, even in the original).
Scratch mode keeps locked parts, connectors and mounting holes where the designer put them.
