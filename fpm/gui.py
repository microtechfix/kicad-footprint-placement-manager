"""Native desktop interface (wxPython, as bundled with KiCad).

Same workflow as before (Analyze, Review groups, Constraints, Preview & apply),
built from ordinary wx controls plus a drawn board view. No web page or local
server is involved.
"""
from __future__ import annotations

import math
import os
import threading
import traceback
from typing import Dict, List, Optional, Set, Tuple

import wx
import wx.grid as gridlib

from . import classify as cl
from . import metrics as mt
from . import APP_NAME, __version__

ICON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icons", "fpm-48.png")

# board view palette
BG = wx.Colour(10, 13, 17)
BOARD = wx.Colour(12, 42, 31)
EDGE = wx.Colour(232, 211, 106)
RATS = wx.Colour(159, 180, 208, 140)
TEXT = wx.Colour(232, 237, 245)
SEL = wx.Colour(255, 255, 255)
PAD_F = wx.Colour(201, 162, 74, 210)
PAD_B = wx.Colour(77, 127, 191, 210)
KEEP = wx.Colour(255, 107, 107)

SCOPES = [("all", "All unlocked parts"), ("new", "Only new / off-board parts"),
          ("blocks", "Chosen blocks"), ("kicad_selection", "KiCad selection"),
          ("selection", "Parts selected in this window")]


def hexcol(h: str, alpha: int = 255) -> wx.Colour:
    h = h.lstrip("#")
    try:
        return wx.Colour(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)
    except (ValueError, IndexError):
        return wx.Colour(136, 136, 136, alpha)


def conf_colour(c: float) -> wx.Colour:
    if c >= 0.8:
        return wx.Colour(30, 140, 80)
    if c >= 0.6:
        return wx.Colour(110, 140, 40)
    if c >= 0.4:
        return wx.Colour(190, 130, 20)
    return wx.Colour(200, 60, 60)


# =============================================================================== board view
class BoardCanvas(wx.Panel):
    def __init__(self, parent, frame: "MainFrame"):
        super().__init__(parent, style=wx.WANTS_CHARS | wx.BORDER_NONE)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.frame = frame
        self.k = 4.0              # pixels per mm
        self.ox = 0.0             # world coordinate at the left edge
        self.oy = 0.0
        self.show_rats = True
        self.show_pads = True
        self.show_refs = True
        self.mode = "both"        # both | proposal | current
        self.drag: Optional[dict] = None
        self.tool: Optional[Tuple[str, str]] = None     # ("region", block id)
        self._rats_key = None
        self._rats: List[Tuple[float, float, float, float]] = []
        self._fitted = False
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))
        self.Bind(wx.EVT_MOUSEWHEEL, self.on_wheel)
        self.Bind(wx.EVT_LEFT_DOWN, self.on_left_down)
        self.Bind(wx.EVT_LEFT_UP, self.on_left_up)
        self.Bind(wx.EVT_RIGHT_DOWN, self.on_pan_down)
        self.Bind(wx.EVT_RIGHT_UP, self.on_pan_up)
        self.Bind(wx.EVT_MIDDLE_DOWN, self.on_pan_down)
        self.Bind(wx.EVT_MIDDLE_UP, self.on_pan_up)
        self.Bind(wx.EVT_MOTION, self.on_motion)
        self.Bind(wx.EVT_MOUSE_CAPTURE_LOST, lambda e: setattr(self, "drag", None))

    # ---------------------------------------------------------------- geometry
    @property
    def session(self):
        return self.frame.session

    def w2s(self, x: float, y: float) -> Tuple[float, float]:
        return (x - self.ox) * self.k, (y - self.oy) * self.k

    def s2w(self, sx: float, sy: float) -> Tuple[float, float]:
        return sx / self.k + self.ox, sy / self.k + self.oy

    def pos_of(self, ref: str) -> Tuple[float, float, float]:
        s = self.session
        p = s.proposal.get(ref)
        if p is not None and self.mode != "current":
            return p
        c = s.board.components[ref]
        return c.x, c.y, c.rot

    def fit(self):
        s = self.session
        if not s.board:
            return
        o = s.board.outline
        xs, ys = [], []
        if o:
            xs += [o.x0, o.x1]
            ys += [o.y0, o.y1]
        for r in s.board.components:
            x, y, _ = self.pos_of(r)
            xs.append(x)
            ys.append(y)
        if not xs:
            return
        w, h = self.GetClientSize()
        w, h = max(w, 100), max(h, 100)
        x0, x1, y0, y1 = min(xs) - 4, max(xs) + 4, min(ys) - 4, max(ys) + 4
        self.k = max(0.2, min(w / (x1 - x0), h / (y1 - y0)))
        self.ox = (x0 + x1) / 2 - w / 2 / self.k
        self.oy = (y0 + y1) / 2 - h / 2 / self.k
        self._fitted = True
        self.Refresh()

    def hit(self, sx, sy) -> Optional[str]:
        s = self.session
        if not s.board:
            return None
        wx_, wy = self.s2w(sx, sy)
        best, area = None, 1e18
        for r, c in s.board.components.items():
            x, y, rot = self.pos_of(r)
            b = c.bbox(x, y, rot)
            if b[0] <= wx_ <= b[2] and b[1] <= wy <= b[3]:
                a = (b[2] - b[0]) * (b[3] - b[1])
                if a < area:
                    best, area = r, a
        return best

    def invalidate(self):
        self._rats_key = None
        self.Refresh()

    # ---------------------------------------------------------------- drawing
    def _poly(self, gc, pts):
        path = gc.CreatePath()
        path.MoveToPoint(pts[0][0], pts[0][1])
        for x, y in pts[1:]:
            path.AddLineToPoint(x, y)
        path.CloseSubpath()
        gc.DrawPath(path)

    def _rect_pts(self, cx, cy, rot, x0, y0, x1, y1):
        out = []
        r = math.radians(rot)
        co, si = math.cos(r), math.sin(r)
        for lx, ly in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            wx_ = cx + lx * co + ly * si
            wy = cy - lx * si + ly * co
            out.append(self.w2s(wx_, wy))
        return out

    def on_paint(self, _evt):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(BG))
        dc.Clear()
        s = self.session
        if not s.board:
            dc.SetTextForeground(TEXT)
            dc.DrawText("Reading the board from KiCad…", 20, 20)
            return
        if not self._fitted:
            self.fit()
        gc = wx.GraphicsContext.Create(dc)
        if gc is None:
            return
        board = s.board
        frame = self.frame
        blocks = {b.id: b for b in s.blocks.values()}

        # board outline
        o = board.outline
        if o:
            a, b = self.w2s(o.x0, o.y0)
            c, d = self.w2s(o.x1, o.y1)
            gc.SetPen(wx.TRANSPARENT_PEN)
            gc.SetBrush(wx.Brush(BOARD))
            gc.DrawRectangle(a, b, c - a, d - b)
        gc.SetPen(wx.Pen(EDGE, 2))
        for x0, y0, x1, y1 in board.outline_segments:
            a, b = self.w2s(x0, y0)
            c, d = self.w2s(x1, y1)
            gc.StrokeLine(a, b, c, d)

        # keep-outs and regions
        gc.SetBrush(wx.Brush(wx.Colour(255, 90, 90, 30)))
        gc.SetPen(wx.Pen(KEEP, 1, wx.PENSTYLE_SHORT_DASH))
        for k in board.keepouts:
            a, b = self.w2s(k.x0, k.y0)
            c, d = self.w2s(k.x1, k.y1)
            gc.DrawRectangle(a, b, c - a, d - b)
        font = wx.Font(9, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        gc.SetBrush(wx.TRANSPARENT_BRUSH)
        for bl in blocks.values():
            if bl.region:
                r = bl.region
                col = hexcol(bl.color)
                gc.SetPen(wx.Pen(col, 2, wx.PENSTYLE_LONG_DASH))
                a, b = self.w2s(r.x0, r.y0)
                c, d = self.w2s(r.x1, r.y1)
                gc.DrawRectangle(a, b, c - a, d - b)
                gc.SetFont(font, col)
                gc.DrawText(bl.name, a + 3, b - 15)

        sel = frame.selected
        locked = s.locked
        # current positions as dashed ghosts + move lines
        if s.proposal and self.mode == "both":
            gc.SetBrush(wx.TRANSPARENT_BRUSH)
            for r in s.proposal:
                c = board.components.get(r)
                if not c:
                    continue
                bl = blocks.get(s.assign[r].block) if r in s.assign else None
                gc.SetPen(wx.Pen(hexcol(bl.color if bl else "#888888", 150), 1, wx.PENSTYLE_SHORT_DASH))
                self._poly(gc, self._rect_pts(c.x, c.y, c.rot, c.xmin, c.ymin, c.xmax, c.ymax))
                px, py, _ = s.proposal[r]
                a, b = self.w2s(c.x, c.y)
                e, f = self.w2s(px, py)
                gc.SetPen(wx.Pen(wx.Colour(127, 139, 160, 110), 1))
                gc.StrokeLine(a, b, e, f)

        # footprints
        tfont = wx.Font(8, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        labels = []
        for r, c in board.components.items():
            x, y, rot = self.pos_of(r)
            bl = blocks.get(s.assign[r].block) if r in s.assign else None
            col = hexcol(bl.color if bl else "#888888")
            alpha = 70 if c.side == "B" else 95
            gc.SetBrush(wx.Brush(wx.Colour(col.Red(), col.Green(), col.Blue(), alpha)))
            if r in sel:
                gc.SetPen(wx.Pen(SEL, 2))
            else:
                gc.SetPen(wx.Pen(col, 1, wx.PENSTYLE_SHORT_DASH if c.side == "B" else wx.PENSTYLE_SOLID))
            self._poly(gc, self._rect_pts(x, y, rot, c.xmin, c.ymin, c.xmax, c.ymax))
            if self.show_pads and self.k > 1.5:
                gc.SetPen(wx.TRANSPARENT_PEN)
                gc.SetBrush(wx.Brush(PAD_B if c.side == "B" else PAD_F))
                for p in c.pads:
                    self._poly(gc, self._rect_pts(x + 0, y + 0, rot, p.lx - p.w / 2, p.ly - p.h / 2,
                                                  p.lx + p.w / 2, p.ly + p.h / 2))
            if c.locked or r in locked:
                gc.SetPen(wx.TRANSPARENT_PEN)
                gc.SetBrush(wx.Brush(KEEP))
                sx, sy = self._rect_pts(x, y, rot, c.xmin, c.ymin, c.xmax, c.ymax)[0]
                gc.DrawEllipse(sx - 3, sy - 3, 6, 6)
            if self.show_refs:
                labels.append((r, x, y, (c.xmax - c.xmin) * self.k))

        # ratsnest
        if self.show_rats:
            key = (id(s.board), tuple(sorted(s.proposal.items())), self.mode)
            if key != self._rats_key:
                pos = {r: self.pos_of(r) for r in board.components}
                rats = mt.ratsnest(board, pos)
                self._rats = [seg for segs in rats.values() for seg in segs]
                self._rats_key = key
            path = gc.CreatePath()
            for x0, y0, x1, y1 in self._rats:
                a, b = self.w2s(x0, y0)
                c, d = self.w2s(x1, y1)
                path.MoveToPoint(a, b)
                path.AddLineToPoint(c, d)
            gc.SetPen(wx.Pen(RATS, 1))
            gc.StrokePath(path)

        # check results: red rings on errors, amber on warnings
        for v in getattr(s, "violations", None) or []:
            col = wx.Colour(255, 80, 80) if v.severity == "error" else wx.Colour(240, 180, 60)
            gc.SetPen(wx.Pen(col, 2))
            gc.SetBrush(wx.TRANSPARENT_BRUSH)
            vx, vy = self.w2s(v.x, v.y)
            if v.x or v.y:
                gc.DrawEllipse(vx - 7, vy - 7, 14, 14)

        # reference labels (skip when too small to read)
        gc.SetFont(tfont, TEXT)
        for r, x, y, wpx in labels:
            tw, th = gc.GetTextExtent(r)
            if wpx < tw * 0.6 and self.k < 6:
                continue
            sx, sy = self.w2s(x, y)
            gc.DrawText(r, sx - tw / 2, sy - th / 2)

        # rubber band for the region tool
        if self.drag and self.drag.get("kind") == "region":
            d = self.drag
            gc.SetPen(wx.Pen(wx.Colour(91, 157, 255), 1, wx.PENSTYLE_SHORT_DASH))
            gc.SetBrush(wx.Brush(wx.Colour(91, 157, 255, 30)))
            x0, y0 = d["sx"], d["sy"]
            x1, y1 = d["cx"], d["cy"]
            gc.DrawRectangle(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))

    # ---------------------------------------------------------------- mouse
    def on_wheel(self, e: wx.MouseEvent):
        mx, my = e.GetPosition()
        wx_, wy = self.s2w(mx, my)
        f = math.exp(e.GetWheelRotation() / 120.0 * 0.18)
        self.k = max(0.2, min(250.0, self.k * f))
        self.ox = wx_ - mx / self.k
        self.oy = wy - my / self.k
        self.Refresh()

    def on_pan_down(self, e: wx.MouseEvent):
        mx, my = e.GetPosition()
        self.drag = {"kind": "pan", "mx": mx, "my": my, "ox": self.ox, "oy": self.oy}
        if not self.HasCapture():
            self.CaptureMouse()

    def on_pan_up(self, _e):
        if self.drag and self.drag.get("kind") == "pan":
            self.drag = None
        if self.HasCapture():
            self.ReleaseMouse()

    def on_left_down(self, e: wx.MouseEvent):
        self.SetFocus()
        mx, my = e.GetPosition()
        s = self.session
        if not s.board:
            return
        if self.tool and self.tool[0] == "region":
            self.drag = {"kind": "region", "sx": mx, "sy": my, "cx": mx, "cy": my}
            self.CaptureMouse()
            return
        r = self.hit(mx, my)
        fr = self.frame
        if r is None:
            if not e.ShiftDown():
                fr.set_selection(set())
            self.drag = {"kind": "pan", "mx": mx, "my": my, "ox": self.ox, "oy": self.oy}
            self.CaptureMouse()
            return
        if e.ShiftDown():
            sel = set(fr.selected)
            sel.symmetric_difference_update({r})
            fr.set_selection(sel)
            return
        if r not in fr.selected:
            fr.set_selection({r})
        if fr.current_step() == 3:
            start = {}
            for q in fr.selected:
                c = s.board.components[q]
                if not (c.locked or q in s.locked):
                    start[q] = self.pos_of(q)
            if start:
                wx_, wy = self.s2w(mx, my)
                self.drag = {"kind": "move", "wx": wx_, "wy": wy, "start": start, "moved": False}
                self.CaptureMouse()

    def on_motion(self, e: wx.MouseEvent):
        mx, my = e.GetPosition()
        d = self.drag
        if d is None:
            r = self.hit(mx, my) if self.session.board else None
            self._tooltip(r)
            return
        if d["kind"] == "pan":
            self.ox = d["ox"] - (mx - d["mx"]) / self.k
            self.oy = d["oy"] - (my - d["my"]) / self.k
            self.Refresh()
        elif d["kind"] == "region":
            d["cx"], d["cy"] = mx, my
            self.Refresh()
        elif d["kind"] == "move":
            wx_, wy = self.s2w(mx, my)
            dx, dy = wx_ - d["wx"], wy - d["wy"]
            d["moved"] = True
            for q, (x, y, rot) in d["start"].items():
                self.session.proposal[q] = (round((x + dx) * 20) / 20, round((y + dy) * 20) / 20, rot)
            self.Refresh()

    def on_left_up(self, e: wx.MouseEvent):
        d, self.drag = self.drag, None
        if self.HasCapture():
            self.ReleaseMouse()
        if not d:
            return
        if d["kind"] == "region" and self.tool:
            bid = self.tool[1]
            self.tool = None
            self.SetCursor(wx.Cursor(wx.CURSOR_ARROW))
            x0, y0 = self.s2w(d["sx"], d["sy"])
            x1, y1 = self.s2w(d["cx"], d["cy"])
            if abs(x1 - x0) > 1 and abs(y1 - y0) > 1:
                self.frame.run(lambda: self.session.edit(
                    "region", {"block": bid, "region": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}),
                    "Saving region")
            self.Refresh()
        elif d["kind"] == "move" and d["moved"]:
            moves = {q: list(self.session.proposal[q]) for q in d["start"]}
            self.frame.run(lambda: self.session.edit_proposal({"moves": moves}), "Updating proposal")

    def _tooltip(self, r):
        if r == getattr(self, "_tip_ref", None):
            return
        self._tip_ref = r
        if r is None:
            self.UnsetToolTip()
            return
        s = self.session
        c = s.board.components[r]
        a = s.assign.get(r)
        bl = s.blocks.get(a.block) if a else None
        txt = f"{r}  {c.value}\n{c.footprint}"
        if a:
            txt += f"\n{bl.name if bl else a.block}  ·  {round(a.confidence * 100)}%\n{a.reason}"
        self.SetToolTip(txt)


# =============================================================================== helpers
def list_ctrl(parent, cols):
    lc = wx.ListCtrl(parent, style=wx.LC_REPORT | wx.BORDER_THEME)
    for i, (name, width) in enumerate(cols):
        lc.InsertColumn(i, name, width=width)
    return lc


def selected_rows(lc: wx.ListCtrl) -> List[int]:
    out = []
    i = lc.GetFirstSelected()
    while i != -1:
        out.append(i)
        i = lc.GetNextSelected(i)
    return out


def hint(parent, frame, text):
    """Explanatory text that wraps to the panel width."""
    st = wx.StaticText(parent, label=text)
    frame._full_text[st] = text
    frame._wrap_labels.append(st)
    return st


def boxed(parent, label, orient=wx.VERTICAL):
    sb = wx.StaticBoxSizer(orient, parent, label)
    return sb, sb.GetStaticBox()


def spin(parent, value, lo, hi, inc, digits=2):
    sc = wx.SpinCtrlDouble(parent, value=str(value), min=lo, max=hi, initial=value, inc=inc, size=wx.Size(80, -1))
    sc.SetDigits(digits)
    return sc


# =============================================================================== main window
class MainFrame(wx.Frame):
    def __init__(self, session, title=APP_NAME):
        area = wx.GetClientDisplayRect()          # screen minus taskbar, in this display's scaling
        w = min(1500, max(800, area.width - 40))
        h = min(950, max(560, area.height - 40))
        super().__init__(None, title=title, size=wx.Size(w, h))
        self.session = session
        self.selected: Set[str] = set()
        self.busy = False
        self._block_ids: List[str] = []
        self._members: List[str] = []
        self._review: List[str] = []
        try:
            self.SetIcon(wx.Icon(ICON, wx.BITMAP_TYPE_PNG))
        except Exception:
            pass
        self.SetMinSize(wx.Size(760, 520))
        self._wrap_labels: List[wx.StaticText] = []
        self._full_text: Dict[wx.StaticText, str] = {}
        self.CreateStatusBar()

        split = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE | wx.SP_3D)
        left = wx.Panel(split)
        right = wx.Panel(split)

        # ---- left: workflow notebook + log
        self.nb = wx.Notebook(left)
        self.nb.AddPage(self._page_analyze(self.nb), "1  Analyze")
        self.nb.AddPage(self._page_groups(self.nb), "2  Review groups")
        self.nb.AddPage(self._page_constraints(self.nb), "3  Constraints")
        self.nb.AddPage(self._page_preview(self.nb), "4  Preview && apply")
        self.nb.AddPage(self._page_ai(self.nb), "5  AI placement")
        self.nb.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, lambda e: (self.canvas.Refresh(), e.Skip()))
        self.log = wx.TextCtrl(left, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL, size=wx.Size(-1, 90))
        self.log.SetFont(wx.Font(8, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        ls = wx.BoxSizer(wx.VERTICAL)
        ls.Add(self.nb, 1, wx.EXPAND | wx.ALL, 4)
        ls.Add(wx.StaticText(left, label="Log"), 0, wx.LEFT, 6)
        ls.Add(self.log, 0, wx.EXPAND | wx.ALL, 4)
        left.SetSizer(ls)

        # ---- right: view options + canvas
        bar = wx.BoxSizer(wx.HORIZONTAL)
        auto = wx.Button(right, label="Auto place")
        auto.SetFont(auto.GetFont().Bold())
        auto.SetToolTip("Group, place, check and fix, then apply to KiCad in one step (undoable)")
        auto.Bind(wx.EVT_BUTTON, self.on_auto_place)
        bar.Add(auto, 0, wx.RIGHT, 6)
        aiauto = wx.Button(right, label="AI auto place")
        aiauto.SetFont(aiauto.GetFont().Bold())
        aiauto.SetToolTip("Auto place using AI hints from the source chosen on the AI placement page "
                          "(learned from your layouts, Claude, or both)")
        aiauto.Bind(wx.EVT_BUTTON, self.on_ai_auto_place)
        bar.Add(aiauto, 0, wx.RIGHT, 12)
        fit = wx.Button(right, label="Fit (F)")
        fit.Bind(wx.EVT_BUTTON, lambda e: self.canvas.fit())
        bar.Add(fit, 0, wx.RIGHT, 8)
        self.cb_rats = wx.CheckBox(right, label="Ratsnest")
        self.cb_pads = wx.CheckBox(right, label="Pads")
        self.cb_refs = wx.CheckBox(right, label="References")
        for cb in (self.cb_rats, self.cb_pads, self.cb_refs):
            cb.SetValue(True)
            cb.Bind(wx.EVT_CHECKBOX, self.on_view_opts)
            bar.Add(cb, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        bar.Add(wx.StaticText(right, label="Show"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.ch_mode = wx.Choice(right, choices=["Current + proposal", "Proposal", "Current"])
        self.ch_mode.SetSelection(0)
        self.ch_mode.Bind(wx.EVT_CHOICE, self.on_view_opts)
        bar.Add(self.ch_mode, 0, wx.ALIGN_CENTER_VERTICAL)
        bar.AddStretchSpacer()
        bar.Add(wx.StaticText(right, label="Wheel: zoom · drag: pan · "
                                            "Shift+click: add · R: rotate"),
                0, wx.ALIGN_CENTER_VERTICAL)
        self.canvas = BoardCanvas(right, self)
        rs = wx.BoxSizer(wx.VERTICAL)
        rs.Add(bar, 0, wx.EXPAND | wx.ALL, 6)
        rs.Add(self.canvas, 1, wx.EXPAND)
        right.SetSizer(rs)

        split.SplitVertically(left, right, max(340, min(520, int(w * 0.34))))
        split.SetMinimumPaneSize(300)
        split.SetSashGravity(0.0)                     # resizing the window grows the board view
        self._left = left
        left.Bind(wx.EVT_SIZE, self.on_left_size)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_key)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.Centre()
        self.Maximize()

    # ------------------------------------------------------------------ layout helpers
    def _wrap_width(self) -> int:
        if not hasattr(self, "_left"):
            return 380
        w, _h = self._left.GetClientSize()
        return max(200, w - 60)

    def _wrap(self, label: wx.StaticText):
        """Wrap a label to the current panel width (re-done whenever the panel is resized)."""
        if label not in self._full_text:
            self._full_text[label] = label.GetLabel()
        label.SetLabel(self._full_text[label])
        label.Wrap(self._wrap_width())
        if label not in self._wrap_labels:
            self._wrap_labels.append(label)

    def _set_wrapped(self, label: wx.StaticText, text: str):
        self._full_text[label] = text
        self._wrap(label)

    def on_left_size(self, e):
        e.Skip()
        wx.CallAfter(self._rewrap)

    def _rewrap(self):
        for lab in self._wrap_labels:
            try:
                lab.SetLabel(self._full_text.get(lab, lab.GetLabel()))
                lab.Wrap(self._wrap_width())
            except Exception:
                pass
        for i in range(self.nb.GetPageCount()):
            pg = self.nb.GetPage(i)
            pg.Layout()
            if isinstance(pg, wx.ScrolledWindow):
                pg.FitInside()

    # ------------------------------------------------------------------ pages
    def _scroll_page(self, nb):
        p = wx.ScrolledWindow(nb, style=wx.VSCROLL)
        p.SetScrollRate(0, 12)
        return p

    def _page_analyze(self, nb):
        p = self._scroll_page(nb)
        s = wx.BoxSizer(wx.VERTICAL)
        self.st_stats = wx.StaticText(p, label="Reading the board…")
        self.st_stats.SetFont(self.st_stats.GetFont().Bold())
        s.Add(self.st_stats, 0, wx.ALL, 8)

        g, gb = boxed(p, "Grouping")
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(gb, label="Group by"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.ch_source = wx.Choice(gb, choices=["Schematic (boxes, sheets, layout)", "Connectivity only"])
        self.ch_source.Bind(wx.EVT_CHOICE, self.on_group_source)
        row.Add(self.ch_source, 1)
        g.Add(row, 0, wx.EXPAND | wx.ALL, 4)
        self.st_sch = wx.StaticText(gb, label="")
        g.Add(self.st_sch, 0, wx.EXPAND | wx.ALL, 4)
        ab = wx.Button(gb, label="Auto place whole board")
        ab.SetFont(ab.GetFont().Bold())
        ab.Bind(wx.EVT_BUTTON, self.on_auto_place)
        g.Add(ab, 0, wx.ALL, 4)
        g.Add(hint(gb, self, "Auto place groups the board from the schematic, places every unlocked part "
                             "(masters with their slaves), runs the checks and fixes, and applies the result "
                             "to KiCad as one undo step. Locked and frozen parts stay where they are."),
              0, wx.EXPAND | wx.ALL, 4)
        b = wx.Button(gb, label="Re-analyze board")
        b.Bind(wx.EVT_BUTTON, lambda e: self.run(lambda: self.session.analyze(reload=True), "Analyzing board",
                                                 refit=True))
        g.Add(b, 0, wx.ALL, 4)
        s.Add(g, 0, wx.EXPAND | wx.ALL, 6)

        r, rb = boxed(p, "Needs review")
        r.Add(hint(rb, self, "Low-confidence assignments. Accept the suggestion or move the part."),
              0, wx.ALL, 4)
        self.lc_review = list_ctrl(rb, [("Ref", 60), ("Value", 90), ("Suggested block", 150), ("Conf", 45),
                                        ("Reason", 300)])
        self.lc_review.SetMinSize(wx.Size(-1, 260))
        self.lc_review.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_review_select)
        r.Add(self.lc_review, 1, wx.EXPAND | wx.ALL, 4)
        row = wx.BoxSizer(wx.HORIZONTAL)
        for label, fn in (("Accept selected", self.on_accept_sel), ("Accept all", self.on_accept_all),
                          ("Move to block…", self.on_review_move)):
            bt = wx.Button(rb, label=label)
            bt.Bind(wx.EVT_BUTTON, fn)
            row.Add(bt, 0, wx.RIGHT, 6)
        r.Add(row, 0, wx.ALL, 4)
        s.Add(r, 1, wx.EXPAND | wx.ALL, 6)
        p.SetSizer(s)
        return p

    def _page_groups(self, nb):
        p = self._scroll_page(nb)
        s = wx.BoxSizer(wx.VERTICAL)
        b1, bb = boxed(p, "Blocks")
        self.il = wx.ImageList(12, 12)
        self.lc_blocks = list_ctrl(bb, [("Block", 210), ("Parts", 45), ("Kind", 90), ("Conf", 45), ("Source", 80)])
        self.lc_blocks.SetImageList(self.il, wx.IMAGE_LIST_SMALL)
        self.lc_blocks.SetMinSize(wx.Size(-1, 220))
        self.lc_blocks.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_block_select)
        b1.Add(self.lc_blocks, 1, wx.EXPAND | wx.ALL, 4)
        row = wx.WrapSizer(wx.HORIZONTAL)
        for label, fn in (("Rename…", self.on_rename), ("Merge selected", self.on_merge),
                          ("Select in KiCad", self.on_kicad_select), ("Reset manual groups", self.on_reset)):
            bt = wx.Button(bb, label=label)
            bt.Bind(wx.EVT_BUTTON, fn)
            row.Add(bt, 0, wx.RIGHT | wx.BOTTOM, 4)
        b1.Add(row, 0, wx.ALL, 4)
        s.Add(b1, 1, wx.EXPAND | wx.ALL, 6)

        m, mb = boxed(p, "Parts in the selected block")
        self.lc_members = list_ctrl(mb, [("Ref", 60), ("Value", 90), ("Conf", 45), ("Evidence", 85),
                                         ("Reason", 300)])
        self.lc_members.SetMinSize(wx.Size(-1, 220))
        self.lc_members.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_member_select)
        self.lc_members.Bind(wx.EVT_LIST_ITEM_DESELECTED, self.on_member_select)
        m.Add(self.lc_members, 1, wx.EXPAND | wx.ALL, 4)
        row = wx.WrapSizer(wx.HORIZONTAL)
        for label, fn in (("Move selected parts to…", self.on_move_parts),
                          ("New block from selected parts…", self.on_split)):
            bt = wx.Button(mb, label=label)
            bt.Bind(wx.EVT_BUTTON, fn)
            row.Add(bt, 0, wx.RIGHT | wx.BOTTOM, 4)
        m.Add(row, 0, wx.ALL, 4)
        s.Add(m, 1, wx.EXPAND | wx.ALL, 6)

        r, rb = boxed(p, "Repeated channels")
        r.Add(hint(rb, self, "Arrange one channel, then copy its relative placement onto the others."),
              0, wx.ALL, 4)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.ch_rep = wx.Choice(rb, choices=[])
        row.Add(self.ch_rep, 1, wx.RIGHT, 6)
        bt = wx.Button(rb, label="Copy arrangement")
        bt.Bind(wx.EVT_BUTTON, self.on_replicate)
        row.Add(bt, 0)
        r.Add(row, 0, wx.EXPAND | wx.ALL, 4)
        s.Add(r, 0, wx.EXPAND | wx.ALL, 6)

        t, tb = boxed(p, "Circuit templates")
        row = wx.BoxSizer(wx.HORIZONTAL)
        for label, fn in (("Save selected block as template…", self.on_tpl_save),
                          ("Apply template to selected block…", self.on_tpl_apply)):
            bt = wx.Button(tb, label=label)
            bt.Bind(wx.EVT_BUTTON, fn)
            row.Add(bt, 0, wx.RIGHT, 6)
        t.Add(row, 0, wx.ALL, 4)
        s.Add(t, 0, wx.EXPAND | wx.ALL, 6)
        p.SetSizer(s)
        return p

    def _page_constraints(self, nb):
        p = self._scroll_page(nb)
        s = wx.BoxSizer(wx.VERTICAL)
        a, ab = boxed(p, "Anchors and locks")
        a.Add(hint(ab, self, "Locked parts never move. Parts locked in KiCad are always respected.\n"
                                       "Connectors already on the board stay fixed unless you change that in step 4."),
              0, wx.ALL, 4)
        row = wx.BoxSizer(wx.HORIZONTAL)
        for label, val in (("Lock selected parts", True), ("Unlock selected parts", False)):
            bt = wx.Button(ab, label=label)
            bt.Bind(wx.EVT_BUTTON, lambda e, v=val: self.on_lock(v))
            row.Add(bt, 0, wx.RIGHT, 6)
        a.Add(row, 0, wx.ALL, 4)
        self.st_locked = wx.StaticText(ab, label="")
        a.Add(self.st_locked, 0, wx.EXPAND | wx.ALL, 4)
        s.Add(a, 0, wx.EXPAND | wx.ALL, 6)

        f, fb = boxed(p, "Freeze finished blocks (tick) and placement regions")
        self.clb_freeze = wx.CheckListBox(fb, choices=[], size=wx.Size(-1, 170))
        self.clb_freeze.Bind(wx.EVT_CHECKLISTBOX, self.on_freeze)
        f.Add(self.clb_freeze, 1, wx.EXPAND | wx.ALL, 4)
        row = wx.BoxSizer(wx.HORIZONTAL)
        bt = wx.Button(fb, label="Draw region for highlighted block")
        bt.Bind(wx.EVT_BUTTON, self.on_draw_region)
        row.Add(bt, 0, wx.RIGHT, 6)
        bt = wx.Button(fb, label="Clear region")
        bt.Bind(wx.EVT_BUTTON, self.on_clear_region)
        row.Add(bt, 0)
        f.Add(row, 0, wx.ALL, 4)
        f.Add(hint(fb, self, "Highlight a block in the list, press Draw, then drag a rectangle on the board."),
              0, wx.ALL, 4)
        s.Add(f, 0, wx.EXPAND | wx.ALL, 6)

        n, nbx = boxed(p, "Net-class priorities")
        n.Add(hint(nbx, self, "Net classes set how much a connection matters, never which block a part "
                                        "belongs to."), 0, wx.ALL, 4)
        self.grid = gridlib.Grid(nbx)
        self.grid.CreateGrid(0, 6)
        for i, lab in enumerate(["Weight", "Room mm", "Max mm", "Noisy", "Sensitive", "Pair"]):
            self.grid.SetColLabelValue(i, lab)
            self.grid.SetColSize(i, 62)
        self.grid.SetRowLabelSize(110)
        self.grid.SetMinSize(wx.Size(-1, 150))
        n.Add(self.grid, 1, wx.EXPAND | wx.ALL, 4)
        row = wx.FlexGridSizer(2, 3, 4, 8)
        self.sp_gnd = spin(nbx, 0.02, 0, 2, 0.01)
        self.sp_pwr = spin(nbx, 0.05, 0, 2, 0.01)
        self.sp_dec = spin(nbx, 6.0, 0, 50, 0.5, 1)
        for lab in ("GND pull", "Rail pull", "Decoupling pull"):
            row.Add(wx.StaticText(nbx, label=lab))
        for w in (self.sp_gnd, self.sp_pwr, self.sp_dec):
            row.Add(w)
        n.Add(row, 0, wx.ALL, 4)
        bt = wx.Button(nbx, label="Save priorities")
        bt.Bind(wx.EVT_BUTTON, self.on_save_rules)
        n.Add(bt, 0, wx.ALL, 4)
        s.Add(n, 0, wx.EXPAND | wx.ALL, 6)
        p.SetSizer(s)
        return p

    def _page_preview(self, nb):
        p = self._scroll_page(nb)
        s = wx.BoxSizer(wx.VERTICAL)
        o, ob = boxed(p, "Proposal")
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(ob, label="Place"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.ch_scope = wx.Choice(ob, choices=[lab for _, lab in SCOPES])
        self.ch_scope.Bind(wx.EVT_CHOICE, lambda e: self.clb_scope.Enable(self.scope_key() == "blocks"))
        row.Add(self.ch_scope, 1)
        o.Add(row, 0, wx.EXPAND | wx.ALL, 4)
        self.clb_scope = wx.CheckListBox(ob, choices=[], size=wx.Size(-1, 110))
        o.Add(self.clb_scope, 0, wx.EXPAND | wx.ALL, 4)
        self.cb_ms = wx.CheckBox(ob, label="Arrange slaves around their master (main IC of each block)")
        self.cb_conn = wx.CheckBox(ob, label="Keep connectors that are already on the board")
        self.cb_rot = wx.CheckBox(ob, label="Allow rotation")
        self.cb_auto = wx.CheckBox(ob, label="Check and fix automatically after generating")
        self.cb_ai4 = wx.CheckBox(ob, label="Use the AI suggestions ticked in step 5")
        self.cb_ai4.Bind(wx.EVT_CHECKBOX, self.on_ai_flags)
        for cb in (self.cb_ms, self.cb_conn, self.cb_rot, self.cb_auto, self.cb_ai4):
            o.Add(cb, 0, wx.ALL, 3)
        grid = wx.FlexGridSizer(0, 4, 4, 8)
        self.sp_clr = spin(ob, 0.25, 0, 5, 0.05)
        self.sp_room = spin(ob, 1.0, 0, 5, 0.1, 1)
        self.sp_disp = spin(ob, 0.0, 0, 5, 0.05)
        self.sp_eff = spin(ob, 1.0, 0.1, 5, 0.25)
        self.sp_sep = spin(ob, 3.0, 0, 30, 0.5, 1)
        self.sp_gap = spin(ob, 0.6, 0, 5, 0.1, 1)
        for lab, w in (("Clearance mm", self.sp_clr), ("Routing room ×", self.sp_room),
                       ("Stay-put pull", self.sp_disp), ("Effort", self.sp_eff), ("Noisy gap mm", self.sp_sep),
                       ("Master–slave channel mm", self.sp_gap)):
            grid.Add(wx.StaticText(ob, label=lab), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(w)
        o.Add(grid, 0, wx.ALL, 4)
        row = wx.BoxSizer(wx.HORIZONTAL)
        bt = wx.Button(ob, label="Generate proposal")
        bt.SetDefault()
        bt.Bind(wx.EVT_BUTTON, self.on_propose)
        row.Add(bt, 0, wx.RIGHT, 6)
        bt = wx.Button(ob, label="Clear proposal")
        bt.Bind(wx.EVT_BUTTON, self.on_clear_proposal)
        row.Add(bt, 0, wx.RIGHT, 6)
        self.st_moves = wx.StaticText(ob, label="")
        row.Add(self.st_moves, 0, wx.ALIGN_CENTER_VERTICAL)
        o.Add(row, 0, wx.ALL, 4)
        s.Add(o, 0, wx.EXPAND | wx.ALL, 6)

        c, cbx = boxed(p, "Check and fix")
        c.Add(hint(cbx, self, "Runs KiCad's own DRC on the proposal (clearances, courtyards, board edge, "
                                        "keep-outs) plus a routability check, then moves only the parts that fail "
                                        "and checks again."), 0, wx.ALL, 4)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.cb_kdrc = wx.CheckBox(cbx, label="Use KiCad DRC (kicad-cli)")
        row.Add(self.cb_kdrc, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        row.Add(wx.StaticText(cbx, label="Fix rounds"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.sp_rounds = spin(cbx, 4, 0, 12, 1, 0)
        row.Add(self.sp_rounds, 0)
        c.Add(row, 0, wx.ALL, 4)
        row = wx.BoxSizer(wx.HORIZONTAL)
        bt = wx.Button(cbx, label="Check only")
        bt.Bind(wx.EVT_BUTTON, self.on_check)
        row.Add(bt, 0, wx.RIGHT, 6)
        bt = wx.Button(cbx, label="Check && fix")
        bt.Bind(wx.EVT_BUTTON, self.on_check_fix)
        row.Add(bt, 0, wx.RIGHT, 6)
        bt = wx.Button(cbx, label="Fix ground…")
        bt.SetToolTip("For a routed board: joins the pieces of the ground pour with vias and connects ground "
                      "pads the fill does not reach.")
        bt.Bind(wx.EVT_BUTTON, self.on_fix_ground)
        row.Add(bt, 0)
        c.Add(row, 0, wx.ALL, 4)
        self.st_check = wx.StaticText(cbx, label="")
        c.Add(self.st_check, 0, wx.EXPAND | wx.ALL, 4)
        self.lc_viol = list_ctrl(cbx, [("Type", 120), ("Parts", 90), ("Problem", 330)])
        self.lc_viol.SetMinSize(wx.Size(-1, 160))
        self.lc_viol.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_viol_select)
        c.Add(self.lc_viol, 1, wx.EXPAND | wx.ALL, 4)
        s.Add(c, 0, wx.EXPAND | wx.ALL, 6)

        q, qb = boxed(p, "Estimated quality")
        self.lc_metrics = list_ctrl(qb, [("Metric", 170), ("Current", 75), ("Proposal", 75), ("Change", 70)])
        self.lc_metrics.SetMinSize(wx.Size(-1, 210))
        q.Add(self.lc_metrics, 1, wx.EXPAND | wx.ALL, 4)
        self.st_crit = wx.StaticText(qb, label="")
        q.Add(self.st_crit, 0, wx.EXPAND | wx.ALL, 4)
        q.Add(hint(qb, self, "Estimates only. Routing time and the finished board are the real test."),
              0, wx.ALL, 4)
        s.Add(q, 0, wx.EXPAND | wx.ALL, 6)

        a, ab = boxed(p, "Apply")
        self.clb_apply = wx.CheckListBox(ab, choices=[], size=wx.Size(-1, 150))
        a.Add(self.clb_apply, 1, wx.EXPAND | wx.ALL, 4)
        row = wx.BoxSizer(wx.HORIZONTAL)
        for label, fn in (("Apply ticked blocks", self.on_apply_sel), ("Apply all", self.on_apply_all),
                          ("Undo", self.on_undo)):
            bt = wx.Button(ab, label=label)
            bt.Bind(wx.EVT_BUTTON, fn)
            row.Add(bt, 0, wx.RIGHT, 6)
            if label == "Undo":
                self.bt_undo = bt
        a.Add(row, 0, wx.ALL, 4)
        self.st_apply = wx.StaticText(ab, label="")
        a.Add(self.st_apply, 0, wx.ALL, 4)
        s.Add(a, 0, wx.EXPAND | wx.ALL, 6)
        p.SetSizer(s)
        return p

    AI_SOURCES = [("learned", "Learned from my layouts (offline, free)"),
                  ("claude", "Claude reads the design (online, uses your API key)"),
                  ("both", "Both: learned habits first, Claude for the rest")]

    def _page_ai(self, nb):
        from . import ai
        p = self._scroll_page(nb)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(hint(p, self, "AI placement adds design intent to the normal placer: which parts belong next to "
                            "which IC pin, what goes on the board edge, where blocks sit and which blocks stay "
                            "apart. You review every suggestion before it is used; the placer, the checks and "
                            "the fix rounds work exactly as in Auto place."), 0, wx.EXPAND | wx.ALL, 8)

        g, gb = boxed(p, "AI source")
        self.rb_ai = wx.RadioBox(gb, choices=[t for _, t in self.AI_SOURCES], majorDimension=1,
                                 style=wx.RA_SPECIFY_COLS)
        self.rb_ai.Bind(wx.EVT_RADIOBOX, self.on_ai_source)
        g.Add(self.rb_ai, 0, wx.EXPAND | wx.ALL, 4)
        self.cb_ai_use = wx.CheckBox(gb, label="Use the ticked suggestions when generating a proposal in step 4 "
                                               "(plain Auto place never uses them)")
        self.cb_ai_use.Bind(wx.EVT_CHECKBOX, self.on_ai_flags)
        g.Add(self.cb_ai_use, 0, wx.ALL, 4)
        bt = wx.Button(gb, label="AI auto place whole board")
        bt.SetFont(bt.GetFont().Bold())
        bt.Bind(wx.EVT_BUTTON, self.on_ai_auto_place)
        g.Add(bt, 0, wx.ALL, 4)
        s.Add(g, 0, wx.EXPAND | wx.ALL, 6)

        c, cb = boxed(p, "Claude (online)")
        c.Add(hint(cb, self, "Sends a text summary of the design (part references, values, footprints, IC pin "
                             "names and nets, block list, board size) to the Anthropic API. No files are "
                             "uploaded. Usage is billed to your API key. The key is stored only on this "
                             "computer in ~/.fpm/config.json (or set ANTHROPIC_API_KEY)."), 0, wx.EXPAND | wx.ALL, 4)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(cb, label="API key"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.tc_key = wx.TextCtrl(cb, style=wx.TE_PASSWORD)
        row.Add(self.tc_key, 1, wx.RIGHT, 6)
        b = wx.Button(cb, label="Save")
        b.Bind(wx.EVT_BUTTON, self.on_ai_save_key)
        row.Add(b, 0)
        c.Add(row, 0, wx.EXPAND | wx.ALL, 4)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(cb, label="Model"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.ch_model = wx.Choice(cb, choices=ai.MODELS)
        self.ch_model.Bind(wx.EVT_CHOICE, self.on_ai_save_key)
        row.Add(self.ch_model, 0)
        self.st_key = wx.StaticText(cb, label="")
        row.Add(self.st_key, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 10)
        c.Add(row, 0, wx.ALL, 4)
        s.Add(c, 0, wx.EXPAND | wx.ALL, 6)

        l, lb = boxed(p, "Learned from your layouts (offline)")
        self.st_learn = wx.StaticText(lb, label="")
        l.Add(self.st_learn, 0, wx.EXPAND | wx.ALL, 4)
        row = wx.BoxSizer(wx.HORIZONTAL)
        for label, fn in (("Learn from this board", self.on_ai_learn), ("Forget everything learned", self.on_ai_forget)):
            b = wx.Button(lb, label=label)
            b.Bind(wx.EVT_BUTTON, fn)
            row.Add(b, 0, wx.RIGHT, 6)
        l.Add(row, 0, wx.ALL, 4)
        self.cb_ai_learn = wx.CheckBox(lb, label="Learn from parts I move by hand after Apply")
        self.cb_ai_learn.Bind(wx.EVT_CHECKBOX, self.on_ai_flags)
        l.Add(self.cb_ai_learn, 0, wx.ALL, 4)
        l.Add(hint(lb, self, "Teach it boards you laid out by hand and are happy with. It remembers where each "
                             "small part sits relative to the IC pin it serves (per IC and part value), which "
                             "footprints you put on the edge, and where block types usually go. Corrections you "
                             "make after an Apply count double."), 0, wx.EXPAND | wx.ALL, 4)
        s.Add(l, 0, wx.EXPAND | wx.ALL, 6)

        r, rb = boxed(p, "Suggestions")
        row = wx.BoxSizer(wx.HORIZONTAL)
        for label, fn in (("Get suggestions", self.on_ai_suggest), ("Use all", lambda e: self.on_ai_enable_all(True)),
                          ("Use none", lambda e: self.on_ai_enable_all(False)), ("Clear", self.on_ai_clear)):
            b = wx.Button(rb, label=label)
            b.Bind(wx.EVT_BUTTON, fn)
            row.Add(b, 0, wx.RIGHT, 6)
        r.Add(row, 0, wx.ALL, 4)
        self.lc_ai = list_ctrl(rb, [("Type", 60), ("Suggestion", 300), ("Why", 260), ("From", 65)])
        try:
            self.lc_ai.EnableCheckBoxes(True)
        except Exception:
            pass
        self.lc_ai.SetMinSize(wx.Size(-1, 280))
        self.lc_ai.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_ai_select)
        self.lc_ai.Bind(wx.EVT_LIST_ITEM_CHECKED, lambda e: self.on_ai_check(e, True))
        self.lc_ai.Bind(wx.EVT_LIST_ITEM_UNCHECKED, lambda e: self.on_ai_check(e, False))
        r.Add(self.lc_ai, 1, wx.EXPAND | wx.ALL, 4)
        r.Add(hint(rb, self, "Tick the suggestions to use. Click one to see its parts on the board."),
              0, wx.EXPAND | wx.ALL, 4)
        self.st_ai_notes = wx.StaticText(rb, label="")
        r.Add(self.st_ai_notes, 0, wx.EXPAND | wx.ALL, 4)
        s.Add(r, 1, wx.EXPAND | wx.ALL, 6)
        p.SetSizer(s)
        self._ai_ids: List[str] = []
        self._ai_filling = False
        return p

    def fill_ai(self):
        from . import ai
        s = self.session
        keys = [k for k, _ in self.AI_SOURCES]
        self.rb_ai.SetSelection(keys.index(s.ai_source) if s.ai_source in keys else 0)
        self.cb_ai_use.SetValue(s.ai_use)
        self.cb_ai_learn.SetValue(s.ai_learn_auto)
        cfg = ai.load_config()
        m = cfg.get("model") or ai.MODELS[0]
        self.ch_model.SetSelection(ai.MODELS.index(m) if m in ai.MODELS else 0)
        has = bool(ai.api_key())
        self.st_key.SetLabel("Key saved" if has else "No key yet")
        try:
            self._set_wrapped(self.st_learn, ai.Library().stats())
        except Exception as e:
            self._set_wrapped(self.st_learn, f"Learned library unreadable: {e}")
        self._ai_filling = True
        lc = self.lc_ai
        lc.DeleteAllItems()
        self._ai_ids = []
        names = {"near": "Near", "edge": "Edge", "zone": "Area", "apart": "Apart", "master": "Master", "name": "Name"}
        for i, x in enumerate(s.ai_suggestions):
            lc.InsertItem(i, names.get(x["kind"], x["kind"]))
            lc.SetItem(i, 1, x.get("text", ""))
            lc.SetItem(i, 2, x.get("reason", ""))
            lc.SetItem(i, 3, x.get("source", ""))
            try:
                lc.CheckItem(i, bool(x.get("enabled", True)))
            except Exception:
                pass
            self._ai_ids.append(x["id"])
        self._ai_filling = False
        n_on = sum(1 for x in s.ai_suggestions if x.get("enabled", True))
        note = f"{n_on} of {len(s.ai_suggestions)} suggestion(s) in use." if s.ai_suggestions else \
            "No suggestions yet. Press Get suggestions, or AI auto place."
        if s.ai_notes:
            note += "\n\n" + s.ai_notes
        self._set_wrapped(self.st_ai_notes, note)

    # ------------------------------------------------------------------ AI handlers
    def _ai_key(self) -> str:
        return [k for k, _ in self.AI_SOURCES][max(0, self.rb_ai.GetSelection())]

    def on_ai_source(self, _e):
        self.session.ai_source = self._ai_key()
        self.session.save()

    def on_ai_flags(self, e):
        src = e.GetEventObject() if e is not None else None
        self.session.ai_use = (self.cb_ai4 if src is self.cb_ai4 else self.cb_ai_use).GetValue()
        self.cb_ai_use.SetValue(self.session.ai_use)
        self.cb_ai4.SetValue(self.session.ai_use)
        self.session.ai_learn_auto = self.cb_ai_learn.GetValue()
        self.session.save()

    def on_ai_save_key(self, _e):
        from . import ai
        cfg = ai.load_config()
        k = self.tc_key.GetValue().strip()
        if k:
            cfg["anthropic_api_key"] = k
            self.tc_key.SetValue("")
        cfg["model"] = ai.MODELS[max(0, self.ch_model.GetSelection())]
        try:
            ai.save_config(cfg)
        except OSError as e:
            wx.MessageBox(f"Could not save the settings: {e}", "AI placement", wx.OK | wx.ICON_WARNING, self)
        self.fill_ai()

    def _ai_ready(self, src) -> bool:
        from . import ai
        if src in ("claude", "both") and not ai.api_key():
            wx.MessageBox("Claude needs an Anthropic API key. Paste it on the AI placement page and press Save, "
                          "or choose 'Learned from my layouts'.", "AI placement", wx.OK | wx.ICON_INFORMATION, self)
            self.nb.SetSelection(4)
            return False
        return True

    def on_ai_suggest(self, _e):
        src = self._ai_key()
        if not self._ai_ready(src):
            return
        self.run(lambda: self.session.ai_suggest(src), "Getting AI suggestions",
                 after=lambda: self.nb.SetSelection(4))

    def on_ai_enable_all(self, value: bool):
        self.session.ai_set_enabled([x["id"] for x in self.session.ai_suggestions], value)
        self.refresh_all()

    def on_ai_clear(self, _e):
        self.session.ai_clear()
        self.refresh_all()

    def on_ai_check(self, e, value: bool):
        if self._ai_filling:
            return
        i = e.GetIndex()
        if 0 <= i < len(self._ai_ids):
            sid = self._ai_ids[i]
            if self.session.ai_set_enabled([sid], value):
                wx.CallAfter(self.refresh_all)         # block names / masters changed
            else:
                n_on = sum(1 for x in self.session.ai_suggestions if x.get("enabled", True))
                self.cb_ai4.SetLabel(f"Use the AI suggestions ticked in step 5 ({n_on})")
                self.cb_ai4.Enable(n_on > 0)

    def on_ai_select(self, e):
        i = e.GetIndex()
        if 0 <= i < len(self.session.ai_suggestions):
            x = self.session.ai_suggestions[i]
            refs = [r for r in x.get("refs", []) if r in self.session.board.components] if self.session.board else []
            for k in ("block_refs", "a_refs", "b_refs"):
                refs += x["data"].get(k, [])
            if refs:
                self.set_selection(set(refs))

    def on_ai_learn(self, _e):
        if wx.MessageBox("Learn from the current layout of this board?\n\nOnly do this for a layout you are happy "
                         "with (laid out by hand or finished and checked).", "Learn from this board",
                         wx.YES_NO | wx.ICON_QUESTION, self) != wx.YES:
            return
        self.run(self.session.learn_board, "Learning from this board")

    def on_ai_forget(self, _e):
        if wx.MessageBox("Forget everything learned from your layouts?", "AI placement",
                         wx.YES_NO | wx.ICON_WARNING, self) == wx.YES:
            self.run(self.session.forget_learned, "Clearing learned data")

    def on_ai_auto_place(self, _e):
        src = self._ai_key()
        if not self._ai_ready(src):
            return
        self.on_auto_place(None, ai=src)

    # ------------------------------------------------------------------ running work
    def current_step(self) -> int:
        return self.nb.GetSelection()

    def run(self, fn, label: str, refit: bool = False, after=None):
        """Run session work off the UI thread, then refresh everything."""
        if self.busy:
            return
        self.busy = True
        self.SetStatusText(label + "…")
        wx.BeginBusyCursor()

        def work():
            err = None
            try:
                fn()
            except Exception as e:      # shown to the user, details in the log file
                err = e
                traceback.print_exc()
            wx.CallAfter(done, err)

        def done(err):
            self.busy = False
            if wx.IsBusy():
                wx.EndBusyCursor()
            self.SetStatusText("Ready")
            self.refresh_all()
            if refit:
                self.canvas.fit()
            if err is not None:
                wx.MessageBox(str(err), APP_NAME, wx.OK | wx.ICON_WARNING, self)
            elif after:
                after()

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ refresh
    def ordered_blocks(self):
        return sorted(self.session.blocks.values(), key=lambda b: (b.kind in ("misc", "mechanical"), b.name.lower()))

    def refresh_all(self):
        s = self.session
        self.log.SetValue("\n".join(s.logs[-200:]))
        self.log.ShowPosition(self.log.GetLastPosition())
        if not s.board:
            self.st_stats.SetLabel("No board loaded. Open a board in the PCB editor and press Re-analyze.")
            self.canvas.Refresh()
            return
        self.SetTitle(f"{APP_NAME} {__version__} — {s.board.name}")
        nets = {p.net for c in s.board.components.values() for p in c.pads if p.net}
        review = [a for a in s.assign.values() if a.confidence < cl.REVIEW_THRESHOLD]
        mb = s.metrics_before or {}
        self._set_wrapped(self.st_stats, f"{len(s.board.components)} footprints · {len(nets)} nets · {len(s.blocks)} blocks · "
                               f"{len(review)} to review\nSignal ratsnest {mb.get('signal_ratsnest_mm', '–')} mm · "
                               f"{mb.get('crossings', '–')} crossings")
        # grouping source
        self.ch_source.SetSelection(0 if s.group_source == "schematic" else 1)
        si = s.sch_info or {}
        st = si.get("stats") or {}
        if si.get("error"):
            self._set_wrapped(self.st_sch, si["error"])
        else:
            self._set_wrapped(self.st_sch, f"{os.path.basename(si.get('file', ''))}: {st.get('sheets', 0)} sheet(s), "
                                 f"{st.get('boxes', 0)} drawn box(es), {st.get('groups', 0)} groups, "
                                 f"{st.get('matched', 0)} footprints matched")

        blocks = self.ordered_blocks()
        self._block_ids = [b.id for b in blocks]
        names = {b.id: b.name for b in blocks}

        # review list
        self._review = [a.ref for a in sorted(review, key=lambda a: a.confidence)]
        lc = self.lc_review
        lc.DeleteAllItems()
        for i, ref in enumerate(self._review):
            a = s.assign[ref]
            c = s.board.components[ref]
            lc.InsertItem(i, ref)
            lc.SetItem(i, 1, c.value)
            lc.SetItem(i, 2, names.get(a.block, a.block))
            lc.SetItem(i, 3, f"{round(a.confidence * 100)}%")
            lc.SetItem(i, 4, a.reason)
            lc.SetItemTextColour(i, conf_colour(a.confidence))

        # blocks list with colour swatches
        keep = selected_rows(self.lc_blocks)
        keep_ids = {self._prev_block_ids[i] for i in keep if i < len(getattr(self, "_prev_block_ids", []))} \
            if hasattr(self, "_prev_block_ids") else set()
        self.il.RemoveAll()
        lc = self.lc_blocks
        lc.DeleteAllItems()
        for i, b in enumerate(blocks):
            bmp = wx.Bitmap(12, 12)
            mdc = wx.MemoryDC(bmp)
            mdc.SetBackground(wx.Brush(hexcol(b.color)))
            mdc.Clear()
            mdc.SelectObject(wx.NullBitmap)
            idx = self.il.Add(bmp)
            conf = sum(s.assign[m].confidence for m in b.members) / max(1, len(b.members))
            lc.InsertItem(i, b.name, idx)
            lc.SetItem(i, 1, str(len(b.members)))
            lc.SetItem(i, 2, cl.KIND_LABEL.get(b.kind, b.kind))
            lc.SetItem(i, 3, f"{round(conf * 100)}%")
            lc.SetItem(i, 4, b.source)
            if b.id in keep_ids:
                lc.Select(i)
        self._prev_block_ids = list(self._block_ids)
        self.fill_members()

        # repeated channels
        reps = cl.repeated_groups(s.board, s.blocks)
        self._reps = reps
        self.ch_rep.Set([" / ".join(names.get(b, b) for b in g) for g in reps] or ["(none found)"])
        self.ch_rep.SetSelection(0)

        # constraints
        self._set_wrapped(self.st_locked, "Locked here: " + (", ".join(sorted(s.locked)) or "none") + "\nLocked in KiCad: " +
                                (", ".join(r for r, c in s.board.components.items() if c.locked) or "none"))
        self.clb_freeze.Set([b.name + ("   [region]" if b.region else "") for b in blocks])
        for i, b in enumerate(blocks):
            self.clb_freeze.Check(i, b.frozen)
        self.fill_rules()

        # options
        o = s.options
        keys = [k for k, _ in SCOPES]
        self.ch_scope.SetSelection(keys.index(o.scope) if o.scope in keys else 0)
        self.clb_scope.Set([b.name for b in blocks])
        for i, b in enumerate(blocks):
            self.clb_scope.Check(i, b.id in (o.blocks or []))
        self.clb_scope.Enable(self.scope_key() == "blocks")
        self.cb_ms.SetValue(o.master_slave)
        self.sp_gap.SetValue(o.escape_gap)
        self.cb_kdrc.SetValue(s.use_kicad_drc)
        self.sp_rounds.SetValue(s.fix_rounds)
        self.cb_auto.SetValue(getattr(self, "_auto_check", True))
        n_ai = sum(1 for x in s.ai_suggestions if x.get("enabled", True))
        self.cb_ai4.SetValue(s.ai_use)
        self.cb_ai4.SetLabel(f"Use the AI suggestions ticked in step 5 ({n_ai})")
        self.cb_ai4.Enable(n_ai > 0)
        self.cb_conn.SetValue(o.keep_connectors_fixed)
        self.cb_rot.SetValue(o.allow_rotation)
        self.sp_clr.SetValue(o.clearance)
        self.sp_room.SetValue(o.routing_space)
        self.sp_disp.SetValue(o.displacement_weight)
        self.sp_eff.SetValue(o.effort)
        self.sp_sep.SetValue(o.separation)
        self.fill_metrics()
        self.fill_apply()
        self.fill_violations()
        self.fill_ai()
        self._rewrap()
        self.canvas.invalidate()

    def fill_violations(self):
        s = self.session
        vs = s.violations or []
        lc = self.lc_viol
        lc.DeleteAllItems()
        self._viols = list(vs)
        for i, v in enumerate(vs):
            lc.InsertItem(i, v.type.replace("_", " "))
            lc.SetItem(i, 1, ", ".join(v.refs[:4]) + ("…" if len(v.refs) > 4 else ""))
            lc.SetItem(i, 2, v.message)
            lc.SetItemTextColour(i, wx.Colour(200, 60, 60) if v.severity == "error" else wx.Colour(190, 130, 20))
        errs = sum(1 for v in vs if v.severity == "error")
        warn = len(vs) - errs
        if s.check_note:
            head = "Passed: no placement violations." if not errs else f"{errs} error(s)"
            self._set_wrapped(self.st_check, f"{head}{f', {warn} warning(s)' if warn else ''}.  {s.check_note}")
        else:
            self._set_wrapped(self.st_check, "Not checked yet.")

    def fill_members(self):
        s = self.session
        rows = selected_rows(self.lc_blocks)
        lc = self.lc_members
        lc.DeleteAllItems()
        self._members = []
        if not rows or rows[0] >= len(self._block_ids):
            return
        b = s.blocks.get(self._block_ids[rows[0]])
        if not b:
            return
        self._members = list(b.members)
        for i, m in enumerate(self._members):
            a = s.assign[m]
            lc.InsertItem(i, m)
            lc.SetItem(i, 1, s.board.components[m].value)
            lc.SetItem(i, 2, f"{round(a.confidence * 100)}%")
            lc.SetItem(i, 3, a.evidence)
            lc.SetItem(i, 4, a.reason)
            lc.SetItemTextColour(i, conf_colour(a.confidence))

    def fill_rules(self):
        s = self.session
        g = self.grid
        names = sorted(s.rules.rules)
        if g.GetNumberRows():
            g.DeleteRows(0, g.GetNumberRows())
        g.AppendRows(len(names))
        for i, n in enumerate(names):
            r = s.rules.rules[n]
            g.SetRowLabelValue(i, n)
            for col, v in enumerate((r.weight, r.spacing, r.max_length)):
                g.SetCellValue(i, col, f"{v:g}")
                g.SetCellEditor(i, col, gridlib.GridCellFloatEditor(-1, 2))
            for col, v in ((3, r.noisy), (4, r.sensitive)):
                g.SetCellEditor(i, col, gridlib.GridCellBoolEditor())
                g.SetCellRenderer(i, col, gridlib.GridCellBoolRenderer())
                g.SetCellValue(i, col, "1" if v else "")
            g.SetCellValue(i, 5, f"{r.pair_weight:g}")
            g.SetCellEditor(i, 5, gridlib.GridCellFloatEditor(-1, 1))
        self._rule_names = names
        self.sp_gnd.SetValue(s.rules.gnd_weight)
        self.sp_pwr.SetValue(s.rules.pwr_weight)
        self.sp_dec.SetValue(s.rules.decap_weight)

    def fill_metrics(self):
        s = self.session
        a, b = s.metrics_before, s.metrics_after
        lc = self.lc_metrics
        lc.DeleteAllItems()
        if not a:
            return
        rows = [("Signal ratsnest (mm)", "signal_ratsnest_mm"), ("All ratsnest (mm)", "all_ratsnest_mm"),
                ("Ratsnest crossings", "crossings"), ("Congestion peak", "congestion_max"),
                ("Congestion 95%", "congestion_p95"), ("Overfull cells", "congestion_overflow_cells"),
                ("Courtyard overlaps", "overlaps")]
        for i, (label, k) in enumerate(rows):
            lc.InsertItem(i, label)
            lc.SetItem(i, 1, str(a[k]))
            if b:
                d = round(b[k] - a[k], 2)
                lc.SetItem(i, 2, str(b[k]))
                lc.SetItem(i, 3, ("+" if d > 0 else "") + (str(d) if d else ""))
                if d:
                    lc.SetItemTextColour(i, wx.Colour(30, 140, 80) if d < 0 else wx.Colour(200, 60, 60))
        i = len(rows)
        lc.InsertItem(i, "Off-board parts")
        lc.SetItem(i, 1, str(len(a.get("out_of_board", []))))
        if b:
            lc.SetItem(i, 2, str(len(b.get("out_of_board", []))))
        crit = (b or {}).get("critical_over_target") or []
        self._set_wrapped(self.st_crit, ("Over length target: " + ", ".join(f"{c['net']} {c['length']}/{c['target']}"
                                                                  for c in crit[:8])) if crit else "")
        self.st_moves.SetLabel(f"{len(s.proposal)} moves" if s.proposal else "")

    def fill_apply(self):
        s = self.session
        per: Dict[str, int] = {}
        for r in s.proposal:
            a = s.assign.get(r)
            if a:
                per[a.block] = per.get(a.block, 0) + 1
        self._apply_ids = [b.id for b in self.ordered_blocks() if per.get(b.id)]
        self.clb_apply.Set([f"{s.blocks[b].name}   ({per[b]} moves)" for b in self._apply_ids])
        for i in range(len(self._apply_ids)):
            self.clb_apply.Check(i, True)
        self.bt_undo.SetLabel(f"Undo ({len(s.undo_stack)})")
        self.bt_undo.Enable(bool(s.undo_stack))
        self.st_apply.SetLabel("Each apply is one step in KiCad's undo history (Ctrl+Z works too)."
                               if s.backend.can_apply else
                               "Offline mode: the .kicad_pcb file is never modified.")

    # ------------------------------------------------------------------ selection helpers
    def set_selection(self, refs: Set[str]):
        self.selected = set(refs)
        self.canvas.Refresh()
        self.SetStatusText(f"{len(self.selected)} part(s) selected" if self.selected else "Ready")

    def selected_block_ids(self) -> List[str]:
        return [self._block_ids[i] for i in selected_rows(self.lc_blocks) if i < len(self._block_ids)]

    def pick_block(self, message: str, allow_new=True) -> Optional[str]:
        blocks = self.ordered_blocks()
        choices = [b.name for b in blocks] + (["+ New block…"] if allow_new else [])
        dlg = wx.SingleChoiceDialog(self, message, "Choose block", choices)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return None
            i = dlg.GetSelection()
        finally:
            dlg.Destroy()
        if i < len(blocks):
            return blocks[i].id
        return "__new"

    def ask_text(self, message: str, default: str = "") -> Optional[str]:
        dlg = wx.TextEntryDialog(self, message, APP_NAME, default)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return None
            v = dlg.GetValue().strip()
            return v or None
        finally:
            dlg.Destroy()

    # ------------------------------------------------------------------ handlers: analyze
    def on_group_source(self, _e):
        src = "schematic" if self.ch_source.GetSelection() == 0 else "connectivity"
        self.run(lambda: self.session.set_group_source(src), "Regrouping")

    def on_review_select(self, _e):
        refs = {self._review[i] for i in selected_rows(self.lc_review) if i < len(self._review)}
        self.set_selection(refs)

    def on_accept_sel(self, _e):
        refs = [self._review[i] for i in selected_rows(self.lc_review) if i < len(self._review)]
        if refs:
            self.run(lambda: self.session.edit("confirm", {"refs": refs}), "Accepting")

    def on_accept_all(self, _e):
        refs = list(self._review)
        if refs:
            self.run(lambda: self.session.edit("confirm", {"refs": refs}), "Accepting")

    def on_review_move(self, _e):
        refs = [self._review[i] for i in selected_rows(self.lc_review) if i < len(self._review)]
        self._move_refs(refs)

    def _move_refs(self, refs):
        if not refs:
            wx.MessageBox("Select one or more parts first.", APP_NAME, wx.OK, self)
            return
        bid = self.pick_block(f"Move {', '.join(refs[:6])}{'…' if len(refs) > 6 else ''} to:")
        if bid is None:
            return
        if bid == "__new":
            name = self.ask_text("Name for the new block:", "New block")
            if name:
                self.run(lambda: self.session.edit("create", {"name": name, "refs": refs}), "Creating block")
            return
        self.run(lambda: self.session.edit("reassign", {"refs": refs, "block": bid}), "Moving parts")

    # ------------------------------------------------------------------ handlers: groups
    def on_block_select(self, _e):
        self.fill_members()
        ids = self.selected_block_ids()
        refs = {m for b in ids for m in self.session.blocks[b].members} if ids else set()
        self.set_selection(refs)

    def on_member_select(self, _e):
        refs = {self._members[i] for i in selected_rows(self.lc_members) if i < len(self._members)}
        if refs:
            self.set_selection(refs)

    def on_rename(self, _e):
        ids = self.selected_block_ids()
        if not ids:
            return
        b = self.session.blocks[ids[0]]
        name = self.ask_text("New name for this block:", b.name)
        if name:
            self.run(lambda: self.session.edit("rename", {"block": b.id, "name": name}), "Renaming")

    def on_merge(self, _e):
        ids = self.selected_block_ids()
        if len(ids) < 2:
            wx.MessageBox("Select two or more blocks in the list (Ctrl+click) to merge.",
                          APP_NAME, wx.OK, self)
            return
        self.run(lambda: self.session.edit("merge", {"blocks": ids}), "Merging")

    def on_kicad_select(self, _e):
        ids = self.selected_block_ids()
        refs = [m for b in ids for m in self.session.blocks[b].members]
        if refs:
            self.run(lambda: self.session.backend.select(refs), "Selecting in KiCad")

    def on_reset(self, _e):
        if wx.MessageBox("Forget all manual block assignments, names and splits?", APP_NAME,
                         wx.YES_NO | wx.ICON_QUESTION, self) == wx.YES:
            self.run(lambda: self.session.edit("reset_groups", {}), "Resetting")

    def _member_or_canvas_refs(self) -> List[str]:
        refs = [self._members[i] for i in selected_rows(self.lc_members) if i < len(self._members)]
        return refs or sorted(self.selected)

    def on_move_parts(self, _e):
        self._move_refs(self._member_or_canvas_refs())

    def on_split(self, _e):
        refs = self._member_or_canvas_refs()
        if not refs:
            wx.MessageBox("Select parts in the list or on the board first.", APP_NAME, wx.OK, self)
            return
        name = self.ask_text(f"Name for the new block ({len(refs)} parts):", "New block")
        if name:
            self.run(lambda: self.session.edit("split", {"name": name, "refs": refs}), "Creating block")

    def on_replicate(self, _e):
        reps = getattr(self, "_reps", [])
        i = self.ch_rep.GetSelection()
        if not reps or i < 0 or i >= len(reps):
            return
        group = reps[i]
        names = [self.session.blocks[b].name for b in group]
        dlg = wx.SingleChoiceDialog(self, "Copy the arrangement of which channel?", "Repeated channels", names)
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            src = group[dlg.GetSelection()]
        finally:
            dlg.Destroy()
        self.run(lambda: self.session.replicate(src, group), "Copying arrangement",
                 after=lambda: self.nb.SetSelection(3))

    def on_tpl_save(self, _e):
        ids = self.selected_block_ids()
        if not ids:
            wx.MessageBox("Select a block first.", APP_NAME, wx.OK, self)
            return
        name = self.ask_text("Template name:", self.session.blocks[ids[0]].name)
        if name:
            self.run(lambda: self.session.save_template(ids[0], name), "Saving template")

    def on_tpl_apply(self, _e):
        from . import templates
        ids = self.selected_block_ids()
        if not ids:
            wx.MessageBox("Select a block first.", APP_NAME, wx.OK, self)
            return
        tpls = templates.list_templates(self.session.template_dir)
        if not tpls:
            wx.MessageBox("No templates saved yet.", APP_NAME, wx.OK, self)
            return
        dlg = wx.SingleChoiceDialog(self, "Apply which template?", "Templates",
                                    [f"{t['name']}  ({t['anchor'].get('value', '')}, {t['parts']} parts)" for t in tpls])
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            fn = tpls[dlg.GetSelection()]["file"]
        finally:
            dlg.Destroy()
        self.run(lambda: self.session.apply_template(ids[0], fn), "Applying template")

    # ------------------------------------------------------------------ handlers: constraints
    def on_lock(self, value: bool):
        refs = sorted(self.selected)
        if not refs:
            wx.MessageBox("Select parts on the board first (click, Shift+click).", APP_NAME,
                          wx.OK, self)
            return
        self.run(lambda: self.session.edit("lock", {"refs": refs, "value": value}), "Updating locks")

    def on_freeze(self, e: wx.CommandEvent):
        i = e.GetInt()
        blocks = self.ordered_blocks()
        if 0 <= i < len(blocks):
            val = self.clb_freeze.IsChecked(i)
            bid = blocks[i].id
            self.run(lambda: self.session.edit("freeze", {"block": bid, "value": val}), "Updating")

    def _highlighted_freeze_block(self) -> Optional[str]:
        i = self.clb_freeze.GetSelection()
        blocks = self.ordered_blocks()
        return blocks[i].id if 0 <= i < len(blocks) else None

    def on_draw_region(self, _e):
        bid = self._highlighted_freeze_block()
        if not bid:
            wx.MessageBox("Click a block name in the list first.", APP_NAME, wx.OK, self)
            return
        self.canvas.tool = ("region", bid)
        self.canvas.SetCursor(wx.Cursor(wx.CURSOR_CROSS))
        self.SetStatusText(f"Drag a rectangle on the board for '{self.session.blocks[bid].name}'")

    def on_clear_region(self, _e):
        bid = self._highlighted_freeze_block()
        if bid:
            self.run(lambda: self.session.edit("region", {"block": bid, "region": None}), "Clearing region")

    def on_save_rules(self, _e):
        g = self.grid
        g.SaveEditControlValue()
        rules = {}
        for i, n in enumerate(getattr(self, "_rule_names", [])):
            def f(col, default=0.0):
                try:
                    return float(g.GetCellValue(i, col) or default)
                except ValueError:
                    return default
            rules[n] = {"weight": f(0, 1.0), "spacing": f(1, 0.3), "max_length": f(2),
                        "noisy": g.GetCellValue(i, 3) == "1", "sensitive": g.GetCellValue(i, 4) == "1",
                        "pair_weight": f(5, 2.0)}
        d = {**self.session.rules.to_json(), "rules": rules, "gnd_weight": self.sp_gnd.GetValue(),
             "pwr_weight": self.sp_pwr.GetValue(), "decap_weight": self.sp_dec.GetValue()}
        self.run(lambda: self.session.set_rules(d), "Saving priorities")

    # ------------------------------------------------------------------ handlers: preview
    def scope_key(self) -> str:
        i = self.ch_scope.GetSelection()
        return SCOPES[i][0] if 0 <= i < len(SCOPES) else "all"

    def read_options(self) -> dict:
        blocks = self.ordered_blocks()
        return {
            "scope": self.scope_key(),
            "blocks": [blocks[i].id for i in self.clb_scope.GetCheckedItems() if i < len(blocks)],
            "refs": sorted(self.selected),
            "keep_connectors_fixed": self.cb_conn.GetValue(), "allow_rotation": self.cb_rot.GetValue(),
            "clearance": self.sp_clr.GetValue(), "routing_space": self.sp_room.GetValue(),
            "displacement_weight": self.sp_disp.GetValue(), "effort": self.sp_eff.GetValue(),
            "separation": self.sp_sep.GetValue(),
            "master_slave": self.cb_ms.GetValue(), "escape_gap": self.sp_gap.GetValue(),
            "auto_check": self.cb_auto.GetValue(),
            "use_ai": self.cb_ai4.GetValue(),
        }

    def on_propose(self, _e):
        o = self.read_options()
        if o["scope"] == "selection" and not o["refs"]:
            wx.MessageBox("Select parts on the board first.", APP_NAME, wx.OK, self)
            return
        if o["scope"] == "blocks" and not o["blocks"]:
            wx.MessageBox("Tick at least one block to place.", APP_NAME, wx.OK, self)
            return
        self._check_settings()
        self.run(lambda: self.session.propose(o), "Generating proposal" + (" and checking" if o["auto_check"] else ""))

    def _check_settings(self):
        s = self.session
        s.use_kicad_drc = self.cb_kdrc.GetValue()
        s.fix_rounds = int(round(self.sp_rounds.GetValue()))
        self._auto_check = self.cb_auto.GetValue()

    def on_check(self, _e):
        self._check_settings()
        self.run(lambda: self.session.check(), "Checking placement")

    def on_check_fix(self, _e):
        self._check_settings()
        self.run(lambda: self.session.check_and_fix(), "Checking and fixing")

    def on_fix_ground(self, _e):
        s = self.session
        box = {}

        def preview():
            box["r"] = s.fix_ground({"apply": False})

        def ask():
            r = box.get("r") or {}
            gp = getattr(s, "ground_plan", None)
            if gp is None or not gp.net or not gp.pieces:
                wx.MessageBox("\n".join(r.get("notes") or ["No filled copper zone found."]), "Fix ground",
                              wx.OK | wx.ICON_INFORMATION, self)
                return
            if not gp.fixes:
                wx.MessageBox(gp.summary() + ("\n\nNeeds a manual fix:\n" + "\n".join(gp.unsolved[:15])
                                              if gp.unsolved else ""), "Fix ground", wx.OK | wx.ICON_INFORMATION, self)
                return
            live = s.backend.can_apply
            msg = (f"{gp.summary()}\n\n"
                   + ("Yes: add the vias and tracks to the board open in KiCad (one Ctrl+Z undoes it).\n"
                      "No: write a fixed copy next to the board instead; the copy also gets the solid pad "
                      "connections.\nCancel: change nothing." if live else
                      "This writes a fixed copy next to the board (name ending in -gndfix). The original "
                      "file is not changed.\n\nContinue?"))
            if gp.unsolved:
                msg += f"\n\n{len(gp.unsolved)} item(s) will still need a manual fix; they are listed in the log."
            style = (wx.YES_NO | wx.CANCEL if live else wx.YES_NO) | wx.ICON_QUESTION
            ans = wx.MessageBox(msg, "Fix ground", style, self)
            if ans == wx.CANCEL or (not live and ans != wx.YES):
                return
            mode = "auto" if (live and ans == wx.YES) else "copy"
            self.run(lambda: s.fix_ground({"apply": True, "mode": mode, "refill": False}), "Fixing the ground pour")

        self.run(preview, "Reading the ground pour", after=ask)

    def on_viol_select(self, _e):
        rows = selected_rows(self.lc_viol)
        vs = getattr(self, "_viols", [])
        refs = {r for i in rows if i < len(vs) for r in vs[i].refs if r in self.session.board.components}
        if refs:
            self.set_selection(refs)
            v = vs[rows[0]]
            w, h = self.canvas.GetClientSize()
            self.canvas.ox = v.x - w / 2 / self.canvas.k
            self.canvas.oy = v.y - h / 2 / self.canvas.k
            self.canvas.Refresh()

    def on_clear_proposal(self, _e):
        def clear():
            with self.session.lock:
                self.session.proposal = {}
                self.session.metrics_after = None
                self.session.violations = []
                self.session.check_note = ""
        self.run(clear, "Clearing")

    def on_apply_sel(self, _e):
        ids = [self._apply_ids[i] for i in self.clb_apply.GetCheckedItems() if i < len(self._apply_ids)]
        if not ids:
            wx.MessageBox("Tick at least one block to apply.", APP_NAME, wx.OK, self)
            return
        self.run(lambda: self.session.apply({"blocks": ids}), "Applying to KiCad")

    def on_apply_all(self, _e):
        if self.session.proposal:
            self.run(lambda: self.session.apply({}), "Applying to KiCad")

    def on_undo(self, _e):
        self.run(self.session.undo, "Undoing")

    # ------------------------------------------------------------------ view / keys / close
    def on_auto_place(self, _e, ai: Optional[str] = None):
        s = self.session
        title = "AI auto place" if ai else "Auto place"
        src_txt = dict(self.AI_SOURCES).get(ai or "", "")
        if not s.backend.can_apply:
            msg = ("Offline mode: Auto place will build and check the placement but can't apply it, "
                   "because the board file is never modified here.\n\nContinue?")
        else:
            msg = ("Auto place will rearrange every unlocked footprint on this board and apply the result "
                   "to KiCad.\n\nLocked parts, frozen blocks and connectors already on the board stay put. "
                   "You can undo it afterwards (Undo here, or Ctrl+Z in KiCad).\n\nContinue?")
        if ai:
            msg = f"AI source: {src_txt}.\nFresh suggestions are collected first and all of them are used.\n\n" + msg
        if wx.MessageBox(msg, title, wx.YES_NO | wx.ICON_QUESTION, self) != wx.YES:
            return
        remove = False
        nt = getattr(s.board, "track_count", 0) if s.board else 0
        if s.backend.can_apply and nt:
            ans = wx.MessageBox(
                f"This board already has {nt} track segments and vias. After the parts move they won't "
                "connect any more.\n\nRemove the old tracks and vias and refill the zones (recommended; "
                "you route the board again afterwards)?\n\nYes: remove them   No: leave them   "
                "Cancel: stop", "Existing routing", wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION, self)
            if ans == wx.CANCEL:
                return
            remove = ans == wx.YES
        self._check_settings()

        def done():
            r = getattr(s, "last_auto", None) or {}
            rb, ra = r.get("ratsnest", (None, None))
            cb, ca = r.get("crossings", (None, None))
            lines = [f"Placed {r.get('applied') or r.get('moved', 0)} footprints"
                     + (" and applied them to KiCad." if r.get("applied") else " (not applied: offline)."),
                     f"Signal ratsnest: {rb} -> {ra} mm",
                     f"Ratsnest crossings: {cb} -> {ca}",
                     ("Checks passed: no placement errors." if not r.get("errors") else
                      f"{r['errors']} placement error(s) remain; see step 4.")
                     + (f" {r['warnings']} routability warning(s)." if r.get("warnings") else ""),
                     "", "Undo restores the previous layout."]
            if r.get("ai"):
                lines.insert(1, "Used " + r["ai"] + " (see step 5).")
            self.canvas.fit()
            wx.MessageBox("\n".join(lines), title + " finished", wx.OK | wx.ICON_INFORMATION, self)
            if r.get("errors"):
                self.nb.SetSelection(3)

        self.run(lambda: s.auto_place(apply=s.backend.can_apply, remove_tracks=remove, ai=ai),
                 ("AI auto placing" if ai else "Auto placing") + " (group, place, check, fix, apply)",
                 after=done)

    def on_view_opts(self, _e):
        c = self.canvas
        c.show_rats = self.cb_rats.GetValue()
        c.show_pads = self.cb_pads.GetValue()
        c.show_refs = self.cb_refs.GetValue()
        c.mode = ["both", "proposal", "current"][max(0, self.ch_mode.GetSelection())]
        c.invalidate()

    def on_key(self, e: wx.KeyEvent):
        focus = wx.Window.FindFocus()
        typing = isinstance(focus, (wx.TextCtrl, wx.SpinCtrlDouble)) or (
            focus is not None and isinstance(focus.GetParent(), gridlib.Grid))
        code = e.GetKeyCode()
        if typing:
            e.Skip()
            return
        if code in (ord("F"), ord("f")):
            self.canvas.fit()
        elif code in (ord("R"), ord("r")) and self.current_step() == 3 and self.selected:
            s = self.session
            moves = {}
            for r in self.selected:
                c = s.board.components[r]
                if c.locked or r in s.locked:
                    continue
                x, y, rot = self.canvas.pos_of(r)
                moves[r] = [x, y, ((rot + 90 + 180) % 360) - 180]
            if moves:
                self.run(lambda: s.edit_proposal({"moves": moves}), "Rotating")
        elif code == wx.WXK_ESCAPE:
            self.canvas.tool = None
            self.canvas.SetCursor(wx.Cursor(wx.CURSOR_ARROW))
            self.set_selection(set())
        else:
            e.Skip()

    def on_close(self, e: wx.CloseEvent):
        e.Skip()


def run(session, analyze=True, log=print) -> bool:
    """Open the window and block until it is closed. Returns False if wx can't start."""
    app = wx.App(False)
    app.SetAppName("FootprintPlacementManager")
    frame = MainFrame(session)
    frame.Show()
    frame.Raise()
    if analyze:
        frame.run(lambda: session.analyze(reload=True), "Reading the board from KiCad", refit=True)
    else:
        frame.refresh_all()
    log("Opened native window")
    app.MainLoop()
    return True
