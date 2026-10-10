#!/usr/bin/env python3
"""Drives the real window: clicks Record, Zoom, Pause, Resume, Stop buttons.

Run from the project root with an isolated config/app-id so it never touches a
running Screenwave:

    XDG_CONFIG_HOME=/some/dir SCREENWAVE_OUT_DIR=/some/out python3 scripts/ui_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, GLib, Gst

from screenwave.config import Config
from screenwave.recorder import RecorderController
from screenwave.ui.window import MainWindow

Gst.init(None)
cfg = Config.load()
cfg.countdown_seconds = 0  # click timings below assume an immediate start
controller = RecorderController(cfg)
app = Adw.Application(application_id="io.github.ferhateroglu.Screenwave.UiTest")


def click(win, name):
    btn = getattr(win, name)
    print(f"click {name:10s} sensitive={btn.get_sensitive()} state={controller.state} "
          f"zoom={controller.zoomer.zoom_on if controller.zoomer else None}", flush=True)
    btn.emit("clicked")
    return False


def on_activate(app_):
    win = MainWindow(app_, controller, cfg)
    win.present()
    steps = [(1500, "record_btn"), (4000, "zoom_btn"), (7000, "pause_btn"),
             (8500, "pause_btn"), (10000, "stop_btn")]
    for ms, name in steps:
        GLib.timeout_add(ms, click, win, name)
    GLib.timeout_add(13000, lambda: (print("final state:", controller.state, flush=True), app_.quit()))


app.connect("activate", on_activate)
app.run(None)
