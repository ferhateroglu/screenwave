"""GNOME Mutter ScreenCast backend.

This is the exact same path xdg-desktop-portal-gnome uses internally
(verified against its 48.0 source): the portal does NOT hand out a PipeWire
fd on GNOME 48 — instead Mutter exposes the stream node on the default
PipeWire instance and announces it via the stream object's
``PipeWireStreamAdded(u node_id)`` signal. We then connect with
``pipewiresrc target-object=<node_id>``.

Advantages: no share dialog, fully deterministic, and direct monitor
selection (RecordMonitor(connector)) — e.g. the external HDMI display.

Verified end-to-end on Debian 13 / GNOME 48 (HDMI-5 → node → 30 fps frames).
"""
from __future__ import annotations

import logging

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

log = logging.getLogger("screenwave.mutter")

SC_BUS = "org.gnome.Mutter.ScreenCast"
SC_PATH = "/org/gnome/Mutter/ScreenCast"
SESSION_IFACE = "org.gnome.Mutter.ScreenCast.Session"
STREAM_IFACE = "org.gnome.Mutter.ScreenCast.Stream"
DC_BUS = "org.gnome.Mutter.DisplayConfig"
DC_PATH = "/org/gnome/Mutter/DisplayConfig"

# Mutter cursor modes (from xdg-desktop-portal-gnome source)
CURSOR_HIDDEN = 0
CURSOR_EMBEDDED = 1
CURSOR_METADATA = 2

_BUILTIN_PREFIXES = ("eDP", "LVDS", "DSI", "Virtual-")  # laptop panels


class MutterError(RuntimeError):
    pass


def _val(v):
    return v.unpack() if hasattr(v, "unpack") else v


def _path(v) -> str:
    """Object path reply: some Gio versions wrap it in a 1-tuple."""
    if hasattr(v, "unpack"):
        v = v.unpack()
    if isinstance(v, (tuple, list)):
        v = v[0] if v else None
    if not isinstance(v, str):
        raise MutterError(f"expected object path, got {v!r}")
    return v


def _call(conn, obj, iface, method, params=None, timeout_ms: int = 10000):
    return conn.call_sync(
        SC_BUS, obj, iface, method, params, None,
        Gio.DBusCallFlags.NONE, timeout_ms, None,
    )


# ----------------------------------------------------------------------
# Monitor listing (org.gnome.Mutter.DisplayConfig)
# ----------------------------------------------------------------------
def list_monitors() -> list[dict]:
    """All monitors: connector, model, position, resolution, primary flag."""
    conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    res = conn.call_sync(
        DC_BUS, DC_PATH, "org.gnome.Mutter.DisplayConfig", "GetCurrentState",
        None, None, Gio.DBusCallFlags.NONE, 10000, None,
    )
    state = _val(res)
    if isinstance(state, (tuple, list)) and state and isinstance(state[0], (tuple, list)):
        state = state[0]
    _serial, monitors, logicals = state[0], state[1], state[2]

    out: list[dict] = []
    for m in monitors:
        ident, modes = m[0], m[1]
        connector, vendor, model, mserial = (
            _val(ident[0]), _val(ident[1]), _val(ident[2]), _val(ident[3])
        )
        x = y = 0
        primary = False
        for lm in logicals:
            lm_monitors = lm[5] if len(lm) > 5 else []
            if any(
                _val(mo[0]) == connector
                for mo in lm_monitors
                if isinstance(mo, (tuple, list)) and mo
            ):
                x, y, primary = _val(lm[0]), _val(lm[1]), _val(lm[4])
                break
        width = height = 0
        for mode in modes:
            flags = mode[6] if len(mode) > 6 else {}
            if isinstance(flags, dict) and flags.get("is-current"):
                width, height = _val(mode[1]), _val(mode[2])
                break
        out.append({
            "connector": connector,
            "vendor": vendor,
            "model": model,
            "serial": mserial,
            "x": x,
            "y": y,
            "primary": bool(primary),
            "width": width,
            "height": height,
            "builtin": connector.startswith(_BUILTIN_PREFIXES),
        })
    return out


def pick_monitor(monitors: list[dict], preferred: str = "") -> dict | None:
    """Choose the monitor to record: preferred > external > primary > first."""
    if not monitors:
        return None
    if preferred:
        for m in monitors:
            if m["connector"] == preferred:
                return m
    externals = [m for m in monitors if not m["builtin"]]
    pool = externals or monitors
    for m in pool:
        if m["primary"]:
            return m
    return pool[0]


# ----------------------------------------------------------------------
# Session
# ----------------------------------------------------------------------
class MutterSession:
    """One recording session on one monitor connector."""

    def __init__(
        self,
        connector: str,
        cursor_mode: int = CURSOR_EMBEDDED,
        timeout_ms: int = 10000,
    ):
        self._conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._timeout_ms = timeout_ms
        self.connector = connector
        self.cursor_mode = cursor_mode
        self.session_path: str | None = None
        self.stream_path: str | None = None
        self.node_id: int | None = None
        self._sub = None

    def open(self) -> int:
        """Create session → RecordMonitor → Start → wait for the node id."""
        sess = _call(
            self._conn, SC_PATH, "org.gnome.Mutter.ScreenCast", "CreateSession",
            GLib.Variant("(a{sv})", ({},)),
        )
        self.session_path = _path(sess)
        stream = _call(
            self._conn, self.session_path, SESSION_IFACE, "RecordMonitor",
            GLib.Variant(
                "(sa{sv})",
                (self.connector, {"cursor-mode": GLib.Variant("u", self.cursor_mode)}),
            ),
        )
        self.stream_path = _path(stream)

        loop = GLib.MainLoop()
        result: dict = {}

        def on_added(_c, _s, _p, _i, _sn, params, _u):
            v = _val(params)
            result["node"] = v[0] if isinstance(v, (tuple, list)) else v
            loop.quit()

        self._sub = self._conn.signal_subscribe(
            SC_BUS, STREAM_IFACE, "PipeWireStreamAdded", self.stream_path,
            None, Gio.DBusSignalFlags.NONE, on_added, None,
        )

        def _timeout() -> bool:
            result["err"] = "timeout"
            loop.quit()
            return False

        t_id = GLib.timeout_add(self._timeout_ms, _timeout)
        _call(self._conn, self.session_path, SESSION_IFACE, "Start")
        loop.run()
        try:
            GLib.source_remove(t_id)
        except Exception:
            pass
        self._unsubscribe()
        if "err" in result:
            self.close()
            raise MutterError(
                f"PipeWireStreamAdded never arrived (connector={self.connector})"
            )
        self.node_id = result["node"]
        log.info(
            "mutter stream ready: connector=%s node=%s cursor=%d",
            self.connector, self.node_id, self.cursor_mode,
        )
        return self.node_id

    def _unsubscribe(self) -> None:
        if self._sub is not None:
            try:
                self._conn.signal_unsubscribe(self._sub)
            except Exception:
                pass
            self._sub = None

    def close(self) -> None:
        self._unsubscribe()
        if self.session_path is not None:
            try:
                _call(
                    self._conn, self.session_path, SESSION_IFACE, "Stop",
                    timeout_ms=5000,
                )
            except Exception:
                pass
        self.session_path = None
        self.stream_path = None
        self.node_id = None