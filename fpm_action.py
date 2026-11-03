"""KiCad IPC plugin entry point (PCB editor toolbar button).

KiCad launches this with its managed Python environment and passes the API
socket/token through environment variables. The manager opens as a native
window (wxPython, bundled with KiCad) and talks to the open board until you
close it.
"""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _console_to_log():
    """pythonw.exe (KiCad's default on Windows) has no usable console; printing to it can
    raise OSError. Send all output to fpm.log in the plugin folder instead."""
    try:
        log = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fpm.log"),
                   "w", encoding="utf-8", buffering=1)
        sys.stdout = log
        sys.stderr = log
    except OSError:
        pass


def main():
    _console_to_log()
    try:
        import fpm
        print(f"{fpm.APP_NAME} {fpm.__version__} from {fpm.PLUGIN_DIR}", flush=True)
        from fpm.kicad_ipc import LiveBackend
        backend = LiveBackend()
        try:
            from fpm import gui                     # native window (wxPython bundled with KiCad)
        except Exception:
            traceback.print_exc()
            print("wxPython not available; falling back to the browser interface", flush=True)
            from fpm.server import serve
            serve(backend, open_browser=True, ui="browser")
            return
        from fpm.session import Session
        gui.run(Session(backend))
    except Exception:
        # Leave a trace next to the plugin; KiCad does not show plugin output.
        log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fpm_error.log")
        with open(log, "w", encoding="utf-8") as f:
            traceback.print_exc(file=f)
        raise


if __name__ == "__main__":
    main()
