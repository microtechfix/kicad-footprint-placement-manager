# Footprint Placement Manager benchmark

KiCad 10 demo boards, `python tools/benchmark.py <demos> --time-limit 25`, seed 1.

## Improving the designer's own layout

| Board | Parts | Ratsnest (mm) | Change | Crossings | New overlaps | Newly off board | Score | Time (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| pic_programmer.kicad_pcb | 63 | 980.9 → 856.1 | -12.7 % | 14 → 8 | 0 | 0 | 1231 → 1094 | 9.5 |
| ecc83-pp.kicad_pcb | 15 | 153.5 → 133.9 | -12.8 % | 1 → 1 | 0 | 0 | 178 → 156 | 1.05 |
| ecc83-pp_v2.kicad_pcb | 15 | 155.3 → 136.2 | -12.3 % | 4 → 1 | 0 | 0 | 194 → 159 | 1.06 |
| interf_u.kicad_pcb | 25 | 3347.7 → 2901.1 | -13.3 % | 349 → 313 | 0 | 0 | 4911 → 4310 | 25.21 |
| complex_hierarchy.kicad_pcb | 68 | 820.5 → 692.9 | -15.6 % | 28 → 15 | 0 | 0 | 1038 → 862 | 13.51 |
| video.kicad_pcb | 189 | 27959.5 → 27959.5 | +0.0 % | 8695 → 8695 | 0 | 0 | 62790 → 62790 | 49.74 |
| StickHub.kicad_pcb | 94 | 280.9 → 280.9 | +0.0 % | 55 → 55 | 0 | 0 | 530 → 530 | 26.37 |
| kit-dev-coldfire-xilinx_5213.kicad_pcb | 160 | 6265.0 → 5586.2 | -10.8 % | 1453 → 1431 | 0 | 0 | 12104 → 11354 | 26.11 |

**Median signal ratsnest change: -12.5 %** (shorter on 6 of 8 boards). No new overlaps or off-board parts: 8 of 8.

## Placing from scratch vs. the designer's hand layout

| Board | Parts | Ratsnest (mm) | Change | Crossings | New overlaps | Newly off board | Score | Time (s) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| pic_programmer.kicad_pcb | 63 | 980.9 → 765.6 | -21.9 % | 14 → 7 | 0 | 0 | 1231 → 1023 | 4.99 |
| ecc83-pp.kicad_pcb | 15 | 153.5 → 191.7 | +24.9 % | 1 → 5 | 0 | 0 | 178 → 235 | 0.63 |
| ecc83-pp_v2.kicad_pcb | 15 | 155.3 → 164.3 | +5.8 % | 4 → 1 | 0 | 0 | 194 → 185 | 0.56 |
| interf_u.kicad_pcb | 25 | 3347.7 → 2898.9 | -13.4 % | 349 → 409 | 0 | 0 | 4911 → 4692 | 14.23 |
| complex_hierarchy.kicad_pcb | 68 | 820.5 → 766.8 | -6.5 % | 28 → 23 | 0 | 0 | 1038 → 987 | 7.48 |
| video.kicad_pcb | 189 | 27959.5 → 34356.9 | +22.9 % | 8695 → 13111 | 3 | 3 | 62790 → 94051 | 40.45 |
| StickHub.kicad_pcb | 94 | 280.9 → 298.1 | +6.1 % | 55 → 44 | 7 | 0 | 530 → 8913 | 12.42 |
| kit-dev-coldfire-xilinx_5213.kicad_pcb | 160 | 6265.0 → 6142.3 | -2.0 % | 1453 → 1387 | 0 | 1 | 12104 → 12955 | 26.38 |

**Median signal ratsnest change: +1.9 %** (shorter on 4 of 8 boards). No new overlaps or off-board parts: 5 of 8.

Ratsnest = total straight-line length of all signal connections (power and ground excluded).
Lower is better: shorter, simpler routing. Crossings = ratsnest lines that cross each other.
Score = signal ratsnest + 1/4 of power ratsnest + 4 mm per crossing + 1200 per new overlap or part newly off board
(lower is better). The tool never proposes a layout whose score is worse than the current one.
New overlaps / newly off board: compared with the designer's layout, whose own near-misses (45-degree parts,
tightly packed connectors, logos past the edge) are not counted against the tool.
Scratch mode keeps locked parts, connectors and mounting holes where the designer put them.
