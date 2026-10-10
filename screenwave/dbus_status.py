"""Recorder status and control on the session bus.

Exported on the app's own bus name (``APP_ID``) at ``OBJECT_PATH``, from the
application's dbus_register hook, i.e. before the name is owned: a D-Bus
activated call (hotkey or top-bar indicator while the app isn't running)
must find the object as soon as the name appears.

Properties are read-only and announced with the standard PropertiesChanged
signal. ``Command`` runs the controller actions: record-toggle, record, stop,
toggle (pause/resume), pause, resume, zoom, status.
"""
from __future__ import annotations

import json
import logging

from gi.repository import Gio, GLib

from .ids import APP_ID, STATUS_IFACE as INTERFACE, STATUS_PATH as OBJECT_PATH
from .recorder import ACTIVE_STATES, RecorderController

__all__ = ["APP_ID", "StatusService"]

log = logging.getLogger("screenwave.dbus")
XML = f"""
<node>
  <interface name="{INTERFACE}">
    <property name="State" type="s" access="read"/>
    <property name="Zoom" type="b" access="read"/>
    <property name="Elapsed" type="d" access="read"/>
    <property name="Countdown" type="i" access="read"/>
    <property name="Monitor" type="s" access="read"/>
    <method name="Command">
      <arg name="action" type="s" direction="in"/>
      <arg name="reply" type="s" direction="out"/>
    </method>
  </interface>
</node>
"""


class StatusService:
    def __init__(self, controller: RecorderController) -> None:
        self.controller = controller
        self._conn: Gio.DBusConnection | None = None
        self._reg_id = 0
        self._last: dict = {}

    def export(self, conn: Gio.DBusConnection | None) -> None:
        if conn is None:
            log.warning("no session bus; panel indicator disabled")
            return
        info = Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0]
        try:
            self._reg_id = conn.register_object(
                OBJECT_PATH, info, self._on_call, self._on_get, None
            )
        except GLib.Error as exc:
            log.warning("could not export status: %s", exc.message)
            return
        self._conn = conn
        self.controller.listeners.append(self._emit_changes)

    def unexport(self) -> None:
        if self._conn is not None and self._reg_id:
            self._conn.unregister_object(self._reg_id)
        self._reg_id = 0
        self._conn = None

    # ------------------------------------------------------------------
    def _values(self) -> dict:
        c = self.controller
        return {
            "State": GLib.Variant("s", c.state),
            "Zoom": GLib.Variant("b", c.state in ACTIVE_STATES and c.zoom_on),
            "Elapsed": GLib.Variant("d", c.elapsed()),
            "Countdown": GLib.Variant("i", c.countdown),
            "Monitor": GLib.Variant("s", c.monitor),
        }

    def _on_get(self, _conn, _sender, _path, _iface, name):
        return self._values().get(name)

    def _on_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        if method != "Command":
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.UnknownMethod", method
            )
            return
        (action,) = params.unpack()
        try:
            reply = self.controller.handle({"action": action})
        except Exception as exc:  # never kill the bus call
            reply = {"ok": False, "error": str(exc)}
        invocation.return_value(GLib.Variant("(s)", (json.dumps(reply),)))

    def _emit_changes(self) -> None:
        if self._conn is None:
            return
        values = self._values()
        # Elapsed always rides along so the panel clock re-syncs on each change
        snapshot = {k: v.unpack() for k, v in values.items() if k != "Elapsed"}
        if snapshot == self._last:
            return
        self._last = snapshot
        try:
            self._conn.emit_signal(
                None,
                OBJECT_PATH,
                "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                GLib.Variant("(sa{sv}as)", (INTERFACE, values, [])),
            )
        except GLib.Error as exc:
            log.warning("PropertiesChanged failed: %s", exc.message)
