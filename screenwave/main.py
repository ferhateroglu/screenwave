"""Screenwave entry point.

Runs as a D-Bus activatable background app: the top-bar indicator and the
GNOME hotkeys call it over D-Bus, which starts it on demand. It quits on its
own after IDLE_EXIT_MS with nothing recording and no window open. (GLib's
inactivity timeout isn't used: in service mode it quits after ~10 s.)

    screenwave                    open the window
    screenwave --apply-hotkeys    register the saved hotkeys with GNOME, exit
    screenwave --remove-hotkeys   remove them again (used by uninstall), exit
"""
from __future__ import annotations

import logging
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gst

from .config import Config
from .dbus_status import APP_ID, StatusService
from .hotkeys import apply_hotkeys, remove_hotkeys
from .logs import install_crash_hooks, setup_logging
from .recorder import STATE_IDLE, RecorderController

IDLE_EXIT_MS = 10 * 60 * 1000

log = logging.getLogger("screenwave")


class ScreenwaveApp(Adw.Application):
    def __init__(self, cfg: Config, controller: RecorderController) -> None:
        super().__init__(application_id=APP_ID)
        self.cfg = cfg
        self.controller = controller
        self.status = StatusService(controller)
        self._idle_exit_id = 0
        controller.listeners.append(self._schedule_idle_exit)
        self.connect("window-removed", lambda *_: self._schedule_idle_exit())

    def do_dbus_register(self, connection, object_path) -> bool:
        # before the bus name is owned, so activated calls find the object
        self.status.export(connection)
        return Adw.Application.do_dbus_register(self, connection, object_path)

    def do_dbus_unregister(self, connection, object_path) -> None:
        self.status.unexport()
        Adw.Application.do_dbus_unregister(self, connection, object_path)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        Gst.init(None)
        try:
            apply_hotkeys(self.cfg.hotkeys)  # keep GNOME's copy in sync
        except Exception:
            log.exception("could not register hotkeys")
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.hold()  # lifetime is ours: see _schedule_idle_exit
        self._schedule_idle_exit()

    def do_activate(self) -> None:
        window = self.get_active_window()
        if window is None:
            from .ui.window import MainWindow

            window = MainWindow(self, self.controller, self.cfg)
        window.present()
        self._schedule_idle_exit()

    def do_shutdown(self) -> None:
        self.controller.shutdown()  # finalizes a running recording
        self.cfg.save()
        Adw.Application.do_shutdown(self)

    def _busy(self) -> bool:
        c = self.controller
        return c.state != STATE_IDLE or c.finishing or bool(self.get_windows())

    def _schedule_idle_exit(self) -> None:
        """(Re)start the idle-exit timer, or cancel it while busy."""
        if self._idle_exit_id:
            GLib.source_remove(self._idle_exit_id)
            self._idle_exit_id = 0
        if not self._busy():
            self._idle_exit_id = GLib.timeout_add(IDLE_EXIT_MS, self._idle_exit)

    def _idle_exit(self) -> bool:
        self._idle_exit_id = 0
        if not self._busy():
            log.info("idle for %d min, quitting", IDLE_EXIT_MS // 60000)
            self.quit()
        return False


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    cfg = Config.load()
    if "--apply-hotkeys" in argv:
        apply_hotkeys(cfg.hotkeys)
        return 0
    if "--remove-hotkeys" in argv:
        remove_hotkeys()
        return 0
    setup_logging()  # the app only: one-shot commands must not reset the log
    install_crash_hooks(log)
    app = ScreenwaveApp(cfg, RecorderController(cfg))
    # only GApplication's own options (e.g. --gapplication-service from the
    # D-Bus service file) are passed on
    return app.run([argv[0]] + [a for a in argv[1:] if a.startswith("--gapplication")])


if __name__ == "__main__":
    raise SystemExit(main())
