"""KiCad Footprint Placement Manager."""
import json as _json
import os as _os

__version__ = "0.5.0"
PLUGIN_DIR = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))


def _app_name() -> str:
    """The toolbar name from this copy's plugin.json, so window titles match the button."""
    try:
        with open(_os.path.join(PLUGIN_DIR, "plugin.json"), encoding="utf-8") as f:
            return str(_json.load(f).get("name") or "Footprint Placement Manager")
    except (OSError, ValueError):
        return "Footprint Placement Manager"


APP_NAME = _app_name()
