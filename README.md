# Footprint Placement Manager (KiCad 10)

A KiCad PCB-editor plugin that **identifies circuit blocks, proposes a component
placement, and lets you review and refine it before routing.**

Design rule: *group by circuit function first, then optimize placement using
real pad-to-pad connections and electrical constraints.* Net classes are never
used to group parts. They only say how much a connection matters.

---

## Download

**Latest version: 0.7.0** — [download the zip](https://github.com/microtechfix/kicad-footprint-placement-manager/raw/main/dist/FootprintPlacementManager-0.7.0.zip) · [what's new](CHANGELOG.md)

## Install

1. In KiCad: **Preferences → Plugins**, turn on **Enable KiCad API** and set the
   Python interpreter to KiCad's bundled one:
   `C:\Program Files\KiCad\10.0\bin\pythonw.exe`
2. Download **[FootprintPlacementManager-0.7.0.zip](https://github.com/microtechfix/kicad-footprint-placement-manager/raw/main/dist/FootprintPlacementManager-0.7.0.zip)**
   (also under [Releases](https://github.com/microtechfix/kicad-footprint-placement-manager/releases)
   when published) and extract it into your KiCad plugins folder, so you get
   `C:\Users\<you>\Documents\KiCad\10.0\plugins\FootprintPlacementManager\plugin.json`.
   (Updating: close KiCad, delete the old `FootprintPlacementManager` folder, extract the new one.)
3. Restart the PCB editor. KiCad creates a private Python environment for the
   plugin and installs `kicad-python` from `requirements.txt` (first run needs
   internet and can take a minute).
4. Click the **Footprint Placement Manager** toolbar button (four coloured
   squares). If you don't see it, check that it's enabled under
   **Preferences → PCB Editor → Action Plugins**.

The manager opens as a normal desktop window built with wxPython, the same GUI
toolkit KiCad uses, so it needs no web browser and starts no local server.
Closing the window stops the plugin.

If nothing opens, look for `fpm_error.log` (startup errors) and `fpm.log`
(running output, including the local address) in the plugin folder.

---

## Workflow

| Step | What you do |
|---|---|
| **1 Analyze** | Reads the open board. Every footprint gets a block, a confidence and a one-line reason. Uncertain parts are listed with **Accept** or a block picker. |
| **2 Review groups** | Rename, merge, split (select parts on the canvas, then *Split selection*), move parts between blocks, *Select in KiCad*. Repeated channels and circuit templates live here too. |
| **3 Constraints** | Lock anchors, **freeze** finished blocks (they become fixed obstacles), **draw a region** a block must stay inside, and set net-class priorities. |
| **4 Preview & apply** | Choose the scope, generate a proposal, compare metrics, drag parts to refine (`R` rotates, `F` fits view), then apply all or only ticked blocks. **Undo** restores the previous placement. Each apply is one Ctrl+Z step in KiCad. |

The window has the four steps as tabs on the left and the board view on the
right. The board view shows the proposal solid and the current position dashed,
coloured by block, with the signal ratsnest (ground and power rails hidden).
Mouse wheel zooms, right-drag (or dragging empty space) pans, click selects,
Shift+click adds to the selection, F fits the board, Esc clears. In step 4 you
can drag selected parts and press R to rotate them.

Your decisions (manual groups, names, locks, regions, rules, options) are saved
next to the board as `<board>.fpm.json`, so they survive restarts and
schematic updates.

### Scopes

- **all unlocked parts**: full placement. Locked and frozen parts stay put;
  connectors already on the board stay fixed (they act as anchors) unless you
  untick *keep placed connectors*.
- **only new / off-board parts**: incremental. After *Update PCB from
  Schematic*, places only the new parts that land off the board, without
  touching your finished work.
- **chosen blocks** / **KiCad selection** / **canvas selection**: optimize just
  those parts.

---

## How blocks are identified

Evidence in priority order:

1. **Your explicit assignments**: the `CircuitBlock` field on a symbol, the
   `PlaceNear` field (`U3` or `U3:5` for a specific pin), and anything you
   assign in step 2.
2. **The schematic** (default; *Group by → Schematic* in step 1). The project's
   `.kicad_sch` files are read directly, including every hierarchical sheet:
   - **Drawn boxes:** graphic rectangles and text boxes. Every symbol inside a
     box belongs to that block, named after the box's text or the largest text
     inside it. This is the most reliable way to tell the manager your blocks.
   - **Hierarchical sheets:** a sheet with one functional area is one block,
     named after the sheet.
   - **Layout:** symbols drawn close together, or wired to each other nearby,
     form a block named after the nearest heading text. Gaps of empty space
     separate blocks, and connections made through net labels don't merge them.
   - A row of decoupling caps drawn on its own is not treated as a block. Each
     cap is attached to the IC whose supply pin it decouples.
   Footprints are matched to symbols by reference designator. **Save the
   schematic before analyzing** so recent edits are seen.
3. **Connectivity**, for anything the schematic leaves open: a capacitor between
   an IC supply pin and ground joins that IC; crystals join their MCU;
   feedback/sense networks join their regulator; ESD/TVS/fuses join the
   connector they protect; RC chains follow through.
4. **Heuristics**: reference prefix, value and footprint (STM32/ATmega/ESP32 →
   microcontroller, AMS1117/TPS5x/LM2596 → power supply, DRV/TMC/A4988 → motor
   driver, MAX485/CH340/TJA → communication, TL072/LM358/OPA → analog, …) and
   net names (FB, SW, XTAL, NRST …). These set each block's kind and default
   name.

Choose *Group by → Connectivity only* in step 1 to ignore the schematic.

A connector whose signals go to a communication transceiver is merged into
that transceiver's block (connector + protection + termination + transceiver).

### Optional fields (add in the schematic symbol properties)

| Field | Example | Effect |
|---|---|---|
| `CircuitBlock` | `Buck 5V` | Forces the block. Parts with the same value form one block. |
| `PlaceNear` | `U3` or `U3:12` | Joins U3's block and is pulled next to U3 (or pin 12). |
| `PlacementPriority` | `high`, `low`, `2.5` | Scales the importance of this part's connections. |
| `PlacementRotate` | `no` | Never rotate this part. |

---

## How placement works

- **Stage A, blocks:** each block becomes a soft rectangle sized from its
  parts plus routing room. Blocks are positioned by inter-block connections,
  their fixed anchors (connectors, locked parts), the board outline, keep-outs
  and regions. Noisy blocks (switching regulators with inductors, motor
  drivers, *noisy* net classes) are kept a set gap from sensitive ones (analog,
  clock, *sensitive* classes).
- **Stage B, inside each block:** the anchor goes first, then each part in
  order of connection strength. Candidate positions and rotations are generated
  so that a pad lands near the pad it connects to (**pad-to-pad**, not
  footprint centres). Decoupling caps are pulled to their own IC's supply pin.
- **Stage C, refinement:** a repeatable annealing pass (small moves, 90°
  rotations, swaps of similar parts, pulls towards connected pads), then
  legalization so courtyards don't overlap.

Every part gets a courtyard margin of *clearance + routing room*. Routing room
grows with pin count (dense ICs get channels) and with each net class's
*Room* setting. On very dense boards the routing room is reduced automatically
and the log says so.

**Hard constraints:** board edge, keep-out rule areas, locked/frozen parts,
block regions. If a part has no legal spot, it is placed as close as possible
and the log reports it (`No legal spot: …`).

**Net-class priorities (step 3):** weight (importance of short connections),
routing room, max connection length, noisy/sensitive, and differential-pair
pull. Ground and power rails get only a tiny whole-net pull, so they can't
collapse the board into one cluster. Decoupling is handled per IC pin instead.

**Metrics (step 4):** signal and full ratsnest length, ratsnest crossings,
RUDY congestion (peak, 95th percentile, overfull cells), courtyard overlaps,
off-board parts and nets over their length target. These are estimates. The
real test is how long routing takes and how good the finished board is.

---

## Master / slave arrangement

Within each block, the main IC is the **master** and the small support parts
(decoupling caps, resistors, crystals, small diodes and transistors) are its
**slaves**:

- Each slave is placed on the side of the master where the pin it connects to
  is, aligned with that pin and turned so its connecting pad faces it.
- A routing channel (*Master–slave channel mm*, default 0.6) is kept free
  between the master and its slaves so the pins can escape.
- Decoupling caps are spread across the master's supply pins, one pin at a time.
- Parts that hang off a slave (an RC chain, a pull-up's LED) go just outside
  that slave, in the same direction.
- Big parts (pots, connectors, modules) are not slaves; they are placed as part
  of the block in the usual way.

Turn it off with *Arrange slaves around their master* in step 4.

## Check and fix

*Check & fix* (or *Check and fix automatically after generating*) runs:

- **KiCad's own DRC** on a temporary copy of the board that holds the
  proposal, using `kicad-cli` and your project's design rules (`.kicad_pro`,
  `.kicad_dru`). Placement violations are kept: courtyard overlaps, clearance,
  copper-to-edge, hole spacing, keep-outs and solder-mask bridges. Unconnected
  items (normal before routing) and errors caused by old tracks are ignored.
  Your real board file is never touched.
- **Built-in checks** that need no KiCad: the same geometric tests, the real
  board outline (notches and cut-outs), and a **routability** estimate that
  flags areas where wiring demand exceeds what the copper layers can carry.

Then only the parts named in a violation get more room and are nudged locally,
everything else stays put, and the check runs again. This repeats for up to
*Fix rounds* (default 4), and the best round is kept. Whatever remains is listed
with the parts involved; click a row to see it on the board. Locked or frozen
parts are never moved.

DRC cannot prove that routing will succeed before any tracks exist; the
routability estimate is the honest stand-in for that.

## Repeated channels and templates

- **Repeated channels:** blocks with near-identical parts lists (e.g. four
  hierarchical channel sheets) are detected automatically. Arrange one, then
  *Copy to others*. Parts are matched by value, footprint and which anchor pins
  they connect to, then placed at the same position relative to each anchor.
  The result goes into the proposal so you can preview it before applying.
- **Templates:** *Save* stores a block's arrangement relative to its anchor in
  `templates/`. *Apply to chosen* reuses it on another block with the same
  kind of anchor, on this board or another.

## AI placement (step 5)

AI placement adds design intent on top of the normal placer. It never sets
coordinates itself: every suggestion becomes a weighted hint, and the placer,
the checks and the fix rounds work exactly as in Auto place. Choose the source
on the *AI placement* page:

| Source | Needs | What it does |
|---|---|---|
| **Learned from my layouts** | nothing (offline, free) | Reuses your own habits: where each small part sits relative to the IC pin it serves (per IC value, pin and part value), which footprints you put on the board edge, and where block types usually go. |
| **Claude reads the design** | Anthropic API key | Sends a text summary (refs, values, footprints, IC pin names and nets, blocks, board size) to Claude, which returns block names and masters, parts to keep next to specific pins, edge parts, board areas for blocks, and blocks to keep apart. |
| **Both** | API key | Learned habits first; Claude fills in the parts you have no habit for. |

- **Teach it:** open a board you laid out by hand and are happy with, then
  press *Learn from this board*. Re-learning the same board replaces its data.
- **Corrections count double:** with *Learn from parts I move by hand after
  Apply* ticked, any part you move in KiCad after an Apply is learned the next
  time the board is analyzed.
- **Review:** *Get suggestions* lists every suggestion with its reason; untick
  any you don't want. Click one to see its parts on the board. *AI auto place*
  (toolbar or step 5) collects fresh suggestions and runs the full
  place, check, fix and apply cycle as one undo step.
- **Storage:** the API key and model are kept in `~/.fpm/config.json` (or set
  `ANTHROPIC_API_KEY`); learned data in `~/.fpm/learned.json`. Both stay on
  your computer and are shared by all boards.

---

## Offline use (no KiCad running)

```
python fpm_cli.py board.kicad_pcb            # blocks + review list in the terminal
python fpm_cli.py board.kicad_pcb --place    # also a proposal with before/after metrics
python fpm_cli.py board.kicad_pcb --ui       # the full window, read-only
run_offline.bat board.kicad_pcb              # same as --ui on Windows
```

Offline mode never modifies the `.kicad_pcb` file.

---

## Files

```
plugin.json            KiCad IPC plugin manifest (validated against KiCad's api.v1 schema)
requirements.txt       kicad-python
fpm_action.py          toolbar entry point
fpm_cli.py             command line / offline
fpm/
  model.py             board model (mm, KiCad rotation convention)
  pcb_file.py          .kicad_pcb + .kicad_pro loader (offline)
  schematic.py         .kicad_sch reader: boxes, sheets, symbol layout and wiring
  kicad_ipc.py         live KiCad backend (IPC API) + offline file backend
  classify.py          block identification with confidence + reasons
  netrules.py          net kinds, net-class priorities
  placer.py            stage A / B / C placement engine
  metrics.py           ratsnest, crossings, congestion, overlaps
  drc.py               KiCad DRC via kicad-cli + built-in placement / routability checks
  pcb_write.py         writes proposed positions into a temporary board copy for DRC
  templates.py         repeated channels and saved templates
  ai.py                AI placement: Claude design reading + learned library -> placement hints
  session.py           workflow state, persistence, apply/undo
  gui.py               native window (wxPython): workflow tabs + board view
  server.py, web/      optional browser interface (python fpm_cli.py --live --browser)
  web/index.html       the UI
tests/                 workflow test on KiCad demo boards, mocked IPC test
```

## Versions and copies

- The window title and the first log line show the version and the folder the
  plugin runs from, e.g. `Footprint Placement Manager 0.7.0 running from …`.
- **Auto place** never uses AI suggestions. **AI auto place** collects fresh
  suggestions and uses all of them. *Generate proposal* in step 4 uses the
  suggestions ticked in step 5 only when *Use the AI suggestions* is ticked.
- Settings are saved next to the board in `<board>.fpm.json`. Saving merges
  with what is already in the file, so another copy or version of the plugin
  (such as the older *Footprint_Placement*, which has its own plugin ID) keeps
  its own keys.
- The browser interface (`fpm_cli.py --live --browser`, or the automatic
  fallback when wxPython is missing) covers steps 1 to 4 only: no Auto place,
  check and fix, or AI placement.

## Known limits (v0.5)

- Overlap checks use axis-aligned courtyard boxes, so a part already sitting at
  45° looks slightly bigger than it is. The engine itself only places at
  0/90/180/270°.
- Parts stay on their current side (top/bottom).
- Writing `CircuitBlock` back to the schematic isn't automatic. Set it in the
  symbol properties if you want it permanent; manual groups are kept in
  `.fpm.json` meanwhile.

---

## Fix ground (routed boards)

After routing, a ground pour on the outer layers is usually cut into pieces by the tracks. KiCad's
DRC then reports "isolated copper fill", "missing connection between items" and "thermal relief
connection incomplete" by the dozen, all from that one cause.

**Fix ground…** (in the *Check and fix* panel) reads the pour and adds only what is needed:

- a stitching via wherever a piece of the pour overlaps the pour on another layer;
- a short track, with a via if needed, for each ground pad the fill does not reach;
- a solid zone connection for pads where only one thermal spoke fits.

Refill the zones (**B**) and save first, so the tool sees the current copper. In KiCad the vias and
tracks are added as one step that Ctrl+Z undoes. Offline, or if you choose *copy*, it writes
`<board>-gndfix.kicad_pcb` next to your board and leaves the original alone:

```
python fpm_cli.py board.kicad_pcb --fix-ground
```

Open the copy, press **B**, and run DRC. Pads boxed in by other nets' tracks with no room for a
via are listed with their coordinates; those need a neighbouring track moved by hand.

## License

MIT. See [LICENSE](LICENSE).
