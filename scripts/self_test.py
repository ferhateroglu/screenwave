#!/usr/bin/env python3
"""Headless end-to-end self test.

--fake:  uses videotestsrc + audiotestsrc — verifies the whole pipeline
         (appsink → zoom-follow → appsrc → x264+voaacenc → mp4) plus
         pause/resume, on any machine (no GNOME needed).
default: real capture through Mutter ScreenCast (external monitor, no share
         dialog) + system audio, ~4 s, then verifies the file.

Run from the project root:

    python3 scripts/self_test.py [--fake]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import GLib, Gst


def _print_env() -> None:
    import os
    import platform

    import gi as _gi

    _gi.require_version("Gio", "2.0")
    from gi.repository import Gio

    print("--- environment ---")
    print(f"system  : {platform.system()} {platform.release()}")
    print(f"session : {os.environ.get('XDG_SESSION_TYPE', '?')} / {os.environ.get('XDG_CURRENT_DESKTOP', '?')}")
    print(f"python  : {platform.python_version()}")
    print(f"gst     : {Gst.version_string()}")
    try:
        conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        res = conn.call_sync(
            "org.gnome.Mutter.ScreenCast", "/org/gnome/Mutter/ScreenCast",
            "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", ("org.gnome.Mutter.ScreenCast", "Version")),
            None, Gio.DBusCallFlags.NONE, 5000, None,
        )
        v = res.unpack()
        v = v[0] if isinstance(v, (tuple, list)) else v
        print(f"mutter  : ScreenCast API v{v}")
    except Exception as exc:
        print(f"mutter  : unavailable ({exc})")


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fake", action="store_true", help="test sources, no capture")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--zoom", type=float, default=2.0)
    parser.add_argument("--stop-zoomed", action="store_true", help="stop while zoom is on")
    parser.add_argument("--no-zoom", action="store_true")
    parser.add_argument("--no-pause", action="store_true")
    parser.add_argument("--mic", action="store_true", help="also record the microphone")
    args = parser.parse_args()

    Gst.init(None)

    from screenwave.config import Config
    from screenwave.cursor_tracker import create_backend
    from screenwave.mutter_capture import MutterSession, list_monitors, pick_monitor
    from screenwave.pipeline import RecorderPipeline, default_audio_monitor, default_microphone
    from screenwave.renderer import ZoomFollow, ZoomSettings

    _print_env()

    out = Path("/tmp/screenwave-selftest.mp4")
    out.unlink(missing_ok=True)

    cfg = Config()
    session: MutterSession | None = None
    node = None
    audio: list[str] = []
    mic = None
    w, h = 1280, 720
    if args.fake:
        print("fake sources (videotestsrc + audiotestsrc)")
    else:
        mon = pick_monitor(list_monitors(), cfg.monitor_connector)
        if mon is None:
            print("FAIL: no monitors found")
            return 1
        w, h = mon["width"], mon["height"]
        print(f"monitor : {mon['connector']} — {mon['model']} ({w}x{h})")
        session = MutterSession(connector=mon["connector"], cursor_mode=1, timeout_ms=15000)
        try:
            node = session.open()
        except Exception as exc:
            print(f"FAIL: mutter open: {exc}")
            return 1
        print(f"node    : {node}")
        audio = [d for d in (default_audio_monitor(),) if d]
        mic = default_microphone() if args.mic else None
        print(f"audio   : {audio or 'off'}")

    pipe = RecorderPipeline(node, out, w, h, fps=args.fps, bitrate_kbps=cfg.video_bitrate_kbps,
                            audio_sources=audio, mic_source=mic, fake=args.fake)
    cursor = create_backend("mock")
    cursor.reset(w, h)
    zoomer = ZoomFollow(w, h, ZoomSettings(zoom=args.zoom))

    loop = GLib.MainLoop()
    finished = {"ok": False}
    errors: list[str] = []
    paused = {"v": False}

    def on_finished() -> None:
        finished["ok"] = True
        print("EOS, muxer closed")
        loop.quit()

    def tick() -> bool:
        if not paused["v"]:
            pipe.set_crop(*zoomer.tick(cursor.read()))
        return True

    def do_pause() -> None:
        paused["v"] = True
        pipe.pause()

    def do_resume() -> None:
        paused["v"] = False
        pipe.resume()

    pipe.on_eos = on_finished
    pipe.on_error = lambda m: (errors.append(m), loop.quit())
    pipe.build()
    pipe.start()
    print("recording started")

    # 1s plain -> zoom on 2s -> pause 1s -> resume, zoom off -> stop (≈5s of content)
    GLib.timeout_add(33, tick)
    if not args.no_zoom:
        GLib.timeout_add(1000, lambda: zoomer.toggle() and False)
    if not args.no_pause:
        GLib.timeout_add(3000, lambda: do_pause() or False)
        GLib.timeout_add(4000, lambda: do_resume() or False)
    if not args.no_zoom and not args.stop_zoomed:
        GLib.timeout_add(4100, lambda: zoomer.toggle() and False)
    GLib.timeout_add(6000, lambda: pipe.stop() or False)
    GLib.timeout_add(30000, loop.quit)  # safety net
    t0 = time.monotonic()
    loop.run()

    if session is not None:
        session.close()
    if errors:
        print(f"FAIL: pipeline errors: {errors}")
        return 1
    if not finished["ok"]:
        print(f"FAIL: no EOS within {(time.monotonic() - t0):.1f}s")
        return 1
    if not out.exists() or out.stat().st_size < 10_000:
        print(f"FAIL: output file missing/too small: {out}")
        return 1
    print(f"OK: {out} ({out.stat().st_size} bytes)")

    # verify streams (video + audio, duration)
    try:
        gi.require_version("GstPbutils", "1.0")
        from gi.repository import GstPbutils

        d = GstPbutils.Discoverer()
        info = d.discover_uri(out.as_uri())
        print(f"duration: {info.get_duration() / 1_000_000_000:.2f}s")
        for v in info.get_video_streams():
            print(f"video   : {v.get_width()}x{v.get_height()} {v.get_caps().to_string().split(',')[0]}")
        for a in info.get_audio_streams():
            print(f"audio   : {a.get_caps().to_string().split(',')[0]} channels={a.get_channels()}")
    except Exception as exc:
        print(f"(stream info unavailable: {exc})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())