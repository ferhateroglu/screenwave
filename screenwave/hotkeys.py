"""GNOME custom-keybinding manager.

Wayland doesn't let apps grab global keys, but GNOME's own shortcuts work.
We register one GNOME custom keybinding per action (record/pause/stop/zoom),
each calling the app's D-Bus ``Command`` method with ``gdbus`` — D-Bus
activation starts Screenwave if it isn't running. Assignments live in
settings.json and are re-applied on every launch.

Our entries use their own paths (`.../custom-keybindings/screenwave-<action>/`)
and never touch the user's other custom shortcuts.
"""
from __future__ import annotations

import ast
import logging
import shlex
import shutil
import subprocess

from .ids import APP_ID, STATUS_IFACE, STATUS_PATH

log = logging.getLogger("screenwave.hotkeys")

SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
ITEM_SCHEMA = SCHEMA + ".custom-keybinding"
BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
ACTIONS = ("record", "pause", "stop", "zoom")
# hotkey action -> Command action: the record and pause keys act as toggles
COMMANDS = {"record": "record-toggle", "pause": "toggle", "stop": "stop", "zoom": "zoom"}


def command_for(action: str) -> str:
    return shlex.join([
        "gdbus", "call", "--session", "--dest", APP_ID,
        "--object-path", STATUS_PATH,
        "--method", f"{STATUS_IFACE}.Command", COMMANDS[action],
    ])


def _path(action: str) -> str:
    return f"{BASE}screenwave-{action}/"


def _item(action: str) -> str:
    return f"{ITEM_SCHEMA}:{_path(action)}"


def _gsettings(*args: str) -> str:
    return subprocess.run(
        ["gsettings", *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _parse_list(raw: str) -> list[str]:
    """gsettings prints `['/a/', '/b/']` or `@as []`; drop anything that isn't a path."""
    raw = raw.strip()
    if raw.startswith("@as"):
        raw = raw[3:].strip()
    try:
        value = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return []
    return [v for v in value if isinstance(v, str) and v.startswith("/")]


def current_custom_keybindings() -> list[str]:
    try:
        return _parse_list(_gsettings("get", SCHEMA, "custom-keybindings"))
    except subprocess.CalledProcessError:
        return []


def ensure_listed(paths: list[str]) -> None:
    """Add our entries to the custom-keybindings list (others kept intact)."""
    current = current_custom_keybindings()
    merged = current + [p for p in paths if p not in current]
    _gsettings(
        "set", SCHEMA, "custom-keybindings",
        "[" + ", ".join(f"'{p}'" for p in merged) + "]",
    )


def normalize(accel: str) -> str:
    """GNOME wants <Control>; GTK also accepts <Ctrl>."""
    return accel.replace("<Ctrl>", "<Control>")


def apply_hotkeys(hotkeys: dict[str, str]) -> None:
    """Register each action as its own GNOME custom shortcut."""
    if shutil.which("gsettings") is None:
        return
    ensure_listed([_path(a) for a in ACTIONS])
    for action in ACTIONS:
        accel = hotkeys.get(action, "")
        try:
            item = _item(action)
            _gsettings("set", item, "name", f"Screenwave {action}")
            _gsettings("set", item, "command", command_for(action))
            _gsettings("set", item, "binding", normalize(accel))
        except subprocess.CalledProcessError as exc:
            log.error("could not register hotkey %s: %s", action, exc.stderr)


def remove_hotkeys() -> None:
    """Drop our entries from GNOME's list and reset their keys."""
    if shutil.which("gsettings") is None:
        return
    ours = {_path(a) for a in ACTIONS}
    kept = [p for p in current_custom_keybindings() if p not in ours]
    _gsettings(
        "set", SCHEMA, "custom-keybindings",
        "[" + ", ".join(f"'{p}'" for p in kept) + "]" if kept else "@as []",
    )
    for action in ACTIONS:
        for key in ("name", "command", "binding"):
            try:
                _gsettings("reset", _item(action), key)
            except subprocess.CalledProcessError:
                pass


def current_gsettings_binding(action: str) -> str | None:
    try:
        return _gsettings("get", _item(action), "binding").strip("'")
    except subprocess.CalledProcessError:
        return None


def find_conflict(accel: str, own_action: str) -> str | None:
    """Name of another GNOME custom shortcut already using this accelerator."""
    wanted = normalize(accel)
    for path in current_custom_keybindings():
        if path == _path(own_action):
            continue
        item = f"{ITEM_SCHEMA}:{path}"
        try:
            if _gsettings("get", item, "binding").strip("'") == wanted:
                return _gsettings("get", item, "name").strip("'")
        except subprocess.CalledProcessError:
            continue
    return None
