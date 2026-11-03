"""Local web UI server (stdlib only). Binds to 127.0.0.1 on a free port."""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .session import Session

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
IDLE_EXIT_SECONDS = 15 * 60       # quit when the browser tab has been closed this long


def make_handler(session: Session, stop_event: threading.Event):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode("utf-8"))

        def do_GET(self):
            session.last_seen = time.time()
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                with open(os.path.join(WEB, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._json(session.state())
            if path == "/api/ping":
                return self._json({"ok": True})
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):
            session.last_seen = time.time()
            n = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                data = {}
            path = self.path.split("?")[0]
            try:
                if path == "/api/analyze":
                    session.analyze(reload=True)
                elif path == "/api/edit":
                    session.edit(data["op"], data)
                elif path == "/api/group_source":
                    session.set_group_source(data.get("source", "schematic"))
                elif path == "/api/rules":
                    session.set_rules(data)
                elif path == "/api/options":
                    session.set_options(data)
                elif path == "/api/propose":
                    session.propose(data)
                elif path == "/api/proposal":
                    session.edit_proposal(data)
                elif path == "/api/clear_proposal":
                    with session.lock:
                        session.proposal = {}
                        session.metrics_after = None
                elif path == "/api/replicate":
                    session.replicate(data["source"], data["targets"])
                elif path == "/api/template/save":
                    session.save_template(data["block"], data["name"])
                elif path == "/api/template/apply":
                    session.apply_template(data["block"], data["file"])
                elif path == "/api/apply":
                    session.apply(data)
                elif path == "/api/undo":
                    session.undo()
                elif path == "/api/select":
                    session.backend.select(data.get("refs", []))
                    return self._json({"ok": True})
                elif path == "/api/shutdown":
                    self._json({"ok": True})
                    stop_event.set()
                    return
                else:
                    return self._json({"error": "unknown endpoint"}, 404)
                return self._json(session.state())
            except Exception as e:  # report to the UI instead of crashing the plugin
                session.log(f"ERROR: {e}")
                try:
                    traceback.print_exc()
                except Exception:
                    pass
                st = session.state()
                st["error"] = str(e)
                return self._json(st, 400)
    return H


def _say(msg):
    try:
        print(msg, flush=True)
    except (OSError, ValueError, AttributeError):
        pass


def _open(url):
    """Open the UI. Under KiCad's pythonw.exe, webbrowser.open can silently do nothing,
    so on Windows ask the shell directly first."""
    opened = False
    if os.name == "nt":
        try:
            os.startfile(url)
            opened = True
        except OSError as e:
            _say(f"os.startfile failed: {e!r}")
        if not opened:
            try:
                import subprocess
                subprocess.Popen(["cmd", "/c", "start", "", url], creationflags=0x08000000)
                opened = True
            except Exception as e:
                _say(f"start command failed: {e!r}")
    if not opened:
        try:
            opened = webbrowser.open(url)
        except Exception as e:
            _say(f"webbrowser failed: {e!r}")
    _say("Browser launch " + ("requested" if opened else "FAILED - open the address above manually"))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def serve(backend, open_browser=True, port=0, analyze=True, ui="window"):
    """ui='window' opens a native window (falls back to the browser), ui='browser' uses the browser."""
    session = Session(backend)
    stop = threading.Event()
    port = port or _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(session, stop))
    url = f"http://127.0.0.1:{port}/"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _say(f"Footprint Placement Manager running at {url}")

    def first_analysis():
        try:
            session.analyze(reload=True)
        except Exception as e:
            session.log(f"ERROR during analysis: {e}")
            try:
                traceback.print_exc()
            except Exception:
                pass

    if analyze:
        # hold the session lock so the page's first request waits for the result
        t = threading.Thread(target=first_analysis, daemon=True)
        t.start()
        t.join(0.2)

    shown = False
    if open_browser and ui == "window":
        from . import window
        try:
            shown = window.run(url, stop, log=_say)
        except Exception as e:
            _say(f"Native window failed: {e!r}")
            shown = False
        if not shown:
            _say("Falling back to the web browser")
    if shown:
        stop.set()
    else:
        if open_browser:
            _open(url)
        try:
            while not stop.is_set():
                time.sleep(1)
                if time.time() - session.last_seen > IDLE_EXIT_SECONDS:
                    _say("No browser activity; exiting.")
                    break
        except KeyboardInterrupt:
            pass
    httpd.shutdown()
    return session
