"""Native desktop window for the manager, using the wxPython that ships with KiCad.

The interface is the same page as the browser version, shown in a wx WebView
backed by Microsoft Edge WebView2 (built into Windows 10/11). If that isn't
available, run() returns False and the caller falls back to the browser.
"""
from __future__ import annotations

import os

ICON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icons", "fpm-48.png")


def available() -> bool:
    try:
        import wx  # noqa: F401
        import wx.html2 as h2
        return bool(h2.WebView.IsBackendAvailable(h2.WebViewBackendEdge))
    except Exception:
        return False


def run(url: str, stop_event, title: str = "Footprint Placement Manager", log=print) -> bool:
    """Show the window and block until it is closed. Returns False if it couldn't open."""
    try:
        import wx
        import wx.html2 as h2
    except Exception as e:
        log(f"wxPython not available: {e!r}")
        return False
    try:
        if not h2.WebView.IsBackendAvailable(h2.WebViewBackendEdge):
            log("Edge WebView2 backend not available in this wxPython build")
            return False
    except Exception as e:
        log(f"WebView backend check failed: {e!r}")
        return False

    # WebView2 needs a writable profile folder; never the KiCad program folder.
    data_dir = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "FootprintPlacementManager")
    os.makedirs(data_dir, exist_ok=True)
    os.environ.setdefault("WEBVIEW2_USER_DATA_FOLDER", data_dir)

    app = wx.App(False)
    app.SetAppName("FootprintPlacementManager")

    frame = wx.Frame(None, title=title, size=(1440, 900))
    try:
        frame.SetIcon(wx.Icon(ICON, wx.BITMAP_TYPE_PNG))
    except Exception:
        pass
    try:
        view = h2.WebView.New(frame, url=url, backend=h2.WebViewBackendEdge)
    except Exception as e:
        log(f"Could not create the web view: {e!r}")
        frame.Destroy()
        return False

    sizer = wx.BoxSizer(wx.VERTICAL)
    sizer.Add(view, 1, wx.EXPAND)
    frame.SetSizer(sizer)
    frame.SetMinSize((900, 600))

    def on_close(evt):
        stop_event.set()
        timer.Stop()
        evt.Skip()

    def on_tick(_evt):
        if stop_event.is_set():          # the page's own Close button
            frame.Close()

    timer = wx.Timer(frame)
    frame.Bind(wx.EVT_TIMER, on_tick, timer)
    frame.Bind(wx.EVT_CLOSE, on_close)
    timer.Start(500)

    frame.Centre()
    frame.Show()
    frame.Raise()
    log("Opened native window")
    app.MainLoop()
    return True
