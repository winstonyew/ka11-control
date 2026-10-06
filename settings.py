"""User settings, stored as JSON in %APPDATA%\\KA11 Control\\settings.json."""
import json
import os
from pathlib import Path

DEFAULTS = {
    "keep_in_tray": True,  # closing the window hides it to the tray instead of quitting
    "hotkeys": True,  # Ctrl+Alt+Up/Down/M for device volume
    "check_updates": True,  # look for a newer GitHub release on startup
    "volume_limit": 50,  # highest device volume level the app will set (50 = no limit)
    "profiles": [],  # [{"name", "level", "filter", "led"}]
}
MAX_PROFILES = 5


def path():
    return Path(os.environ.get("APPDATA", Path.home())) / "KA11 Control" / "settings.json"


def load():
    data = dict(DEFAULTS)
    try:
        data.update(json.loads(path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    data["volume_limit"] = max(1, min(50, int(data["volume_limit"])))
    return data


def save(data):
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)  # atomic, so a crash mid-write can't leave a half-written file
