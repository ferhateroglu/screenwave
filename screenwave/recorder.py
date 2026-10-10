"""RecorderController — single control point.

State machine: idle → countdown → recording ⇄ paused → idle.
All methods run on the GLib main loop; the window, the D-Bus interface and
the hotkeys all go through ``handle()`` / the public methods here.

Each recording ("take") owns its capture sessions, cursor source, zoom state
and pipeline. Stopping hands the take over to the finishing pipeline, so a
new take can start while the previous file is still being finalized.
"""
from __future__ import annotations

import logging
import math
import time
from pathlib import Path
from typing import Callable

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

from .config import Config
from .cursor_tracker import PipewireCursor, create_backend
from .mutter_capture import (
    CURSOR_EMBEDDED,
    CURSOR_METADATA,
    MutterSession,
    list_monitors,
    pick_monitor,
)
from .pipeline import RecorderPipeline, default_audio_monitor, default_microphone
from .renderer import ZoomFollow, ZoomSettings

log = logging.getLogger("screenwave.recorder")

STATE_IDLE = "idle"
STATE_COUNTDOWN = "countdown"
STATE_RECORDING = "recording"
STATE_PAUSED = "paused"
ACTIVE_STATES = (STATE_RECORDING, STATE_PAUSED)


class Take:
    """Everything one recording needs; closed once its file is finalized."""

    def __init__(self, mon: dict) -> None:
        self.mon = mon
        self.session: MutterSession | None = None
        self.cursor_session: MutterSession | None = None
        self.cursor = None
        self.zoomer: ZoomFollow | None = None
        self.pipeline: RecorderPipeline | None = None
        self.file: Path | None = None
        self.tick_id = 0
        self.ready = False  # capture streams are open

    def stop_ticking(self) -> None:
        if self.tick_id:
            GLib.source_remove(self.tick_id)
            self.tick_id = 0

    def close(self) -> None:
        self.stop_ticking()
        if self.cursor is not None:
            self.cursor.close()
            self.cursor = None
        for session in (self.session, self.cursor_session):
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
        self.session = self.cursor_session = None


class RecorderController:
    def __init__(self, config: Config):
        self.cfg = config
        self.state = STATE_IDLE
        self.take: Take | None = None
        self.last_error: str | None = None
        self.countdown = 0  # seconds left while in STATE_COUNTDOWN
        self.on_state_changed: Callable[[str], None] | None = None
        self.on_error: Callable[[str], None] | None = None
        # any visible change (state, zoom, countdown) -> D-Bus, window
        self.listeners: list[Callable[[], None]] = []
        self._rec_started = 0.0
        self._paused_total = 0.0
        self._paused_at = 0.0
        self._deadline = 0.0
        self._countdown_id = 0
        self._preparing = False
        self._finishing: set[Take] = set()  # stopped, file not finalized yet

    # ------------------------------------------------------------------
    def _set_state(self, state: str) -> None:
        if state == self.state:
            return
        now = time.monotonic()
        if state == STATE_RECORDING and self.state not in ACTIVE_STATES:
            self._rec_started, self._paused_total = now, 0.0
        elif state == STATE_PAUSED:
            self._paused_at = now
        elif state == STATE_RECORDING and self.state == STATE_PAUSED:
            self._paused_total += now - self._paused_at
        self.state = state
        if self.on_state_changed:
            self.on_state_changed(state)
        self._notify()

    def _notify(self) -> None:
        for listener in self.listeners:
            try:
                listener()
            except Exception:
                log.exception("state listener failed")

    def _fail(self, message: str) -> dict:
        self.last_error = message
        if self.on_error is not None:
            self.on_error(message)
        return {"ok": False, "error": message}

    @property
    def zoomer(self) -> ZoomFollow | None:
        return self.take.zoomer if self.take else None

    @property
    def zoom_on(self) -> bool:
        return self.zoomer is not None and self.zoomer.zoom_on

    @property
    def current_file(self) -> Path | None:
        return self.take.file if self.take else None

    @property
    def finishing(self) -> bool:
        """A stopped recording is still being written."""
        return bool(self._finishing)

    @property
    def monitor(self) -> str:
        """Connector being (or about to be) recorded, e.g. HDMI-1."""
        return self.take.mon["connector"] if self.take else ""

    def elapsed(self) -> float:
        """Recorded seconds so far, pauses excluded."""
        if self.state not in ACTIVE_STATES:
            return 0.0
        end = self._paused_at if self.state == STATE_PAUSED else time.monotonic()
        return max(end - self._rec_started - self._paused_total, 0.0)

    def _new_output_path(self) -> Path:
        out_dir = self.cfg.output_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
        return out_dir / f"screenwave-{ts}.{self.cfg.container}"

    # ------------------------------------------------------------------
    # Commands -> dict (shared D-Bus/UI protocol)
    # ------------------------------------------------------------------
    def handle(self, cmd: dict) -> dict:
        action = cmd.get("action")
        handler = {
            "record": self.start_recording,
            "record-toggle": self.toggle_record,
            "pause": self.pause_recording,
            "resume": self.resume_recording,
            "toggle": self.toggle_pause,
            "zoom": self.toggle_zoom,
            "stop": self.stop_recording,
            "status": self.status,
        }.get(action)
        if handler is None:
            return {"ok": False, "error": f"unknown action: {action}"}
        return handler()

    def start_recording(self) -> dict:
        """Start, after the configured countdown (if any)."""
        if self.state != STATE_IDLE:
            return {"ok": False, "error": f"state: {self.state}"}
        try:
            # quick, and lets the countdown show on the right monitor
            mon = pick_monitor(list_monitors(), self.cfg.monitor_connector)
            if mon is None:
                raise RuntimeError("no monitors found")
        except Exception as exc:
            log.exception("picking the monitor failed")
            return self._fail(str(exc))
        take = Take(mon)
        self.take = take
        seconds = max(int(self.cfg.countdown_seconds), 0)
        if seconds == 0:
            if not self._prepare(take):
                return self._fail(self.last_error)
            return self._begin()
        self._deadline = time.monotonic() + seconds
        self.countdown = seconds
        self._set_state(STATE_COUNTDOWN)
        self._countdown_id = GLib.timeout_add(50, self._countdown_tick)
        # open the capture streams while the numbers run, so frame one lands
        # right at zero; deferred so the caller gets its reply first
        GLib.idle_add(self._prepare_during_countdown)
        log.info("countdown %ds on %s", seconds, mon["connector"])
        return {"ok": True, "state": self.state, "countdown": seconds}

    def cancel_countdown(self) -> dict:
        if self.state != STATE_COUNTDOWN:
            return {"ok": False, "error": f"state: {self.state}"}
        self._stop_countdown()
        take, self.take = self.take, None
        if not self._preparing:
            take.close()  # otherwise _prepare_during_countdown closes it
        self._set_state(STATE_IDLE)
        log.info("countdown cancelled")
        return {"ok": True, "state": self.state}

    def _stop_countdown(self) -> None:
        if self._countdown_id:
            GLib.source_remove(self._countdown_id)
            self._countdown_id = 0
        self.countdown = 0

    def _countdown_tick(self) -> bool:
        left = max(math.ceil(self._deadline - time.monotonic()), 0)
        if left and left != self.countdown:
            self.countdown = left
            self._notify()
        if left == 0 and self.take is not None and self.take.ready:
            self._countdown_id = 0
            self.countdown = 0
            self._begin()
            return False
        return True  # at zero but streams not ready yet: start when they are

    def _prepare_during_countdown(self) -> bool:
        take = self.take
        if self.state != STATE_COUNTDOWN or take is None or take.ready or self._preparing:
            return False  # cancelled, done, or another prepare is running
        ok = self._prepare(take)
        if self.take is not take:
            take.close()  # cancelled while the streams were opening
            if self.state == STATE_COUNTDOWN:  # ...and a new countdown began
                GLib.idle_add(self._prepare_during_countdown)
        elif not ok:
            self.take = None
            self._stop_countdown()
            self._set_state(STATE_IDLE)
            self._fail(self.last_error)
        return False

    def _prepare(self, take: Take) -> bool:
        """Open the capture streams (the slow part).

        MutterSession.open() iterates the main loop, so commands and countdown
        ticks can run meanwhile: ``_preparing`` and ``take.ready`` keep them
        off this take until it's done.
        """
        self._preparing = True
        try:
            mon = take.mon
            take.cursor = self._make_cursor(take)
            take.zoomer = ZoomFollow(
                mon["width"],
                mon["height"],
                ZoomSettings(
                    zoom=self.cfg.zoom,
                    follow_cursor=self.cfg.follow_cursor,
                    smoothing=self.cfg.smoothing,
                ),
            )
            take.session = MutterSession(
                connector=mon["connector"],
                cursor_mode=CURSOR_EMBEDDED,
                timeout_ms=self.cfg.capture_timeout_ms,
            )
            take.session.open()
            take.ready = True
            return True
        except Exception as exc:  # never crash the UI
            log.exception("preparing the capture failed")
            take.close()
            if self.take is take and self.state != STATE_COUNTDOWN:
                self.take = None
            self.last_error = str(exc)  # callers report it once state is settled
            return False
        finally:
            self._preparing = False

    def _begin(self) -> dict:
        take = self.take
        try:
            audio_sources = []
            if self.cfg.record_audio:
                dev = default_audio_monitor()
                if dev:
                    audio_sources.append(dev)
                else:
                    log.warning("no default sink monitor found")
            mic = None
            if self.cfg.record_mic:
                mic = default_microphone()
                if not mic:
                    log.warning("no microphone found")

            mon = take.mon
            out = self._new_output_path()
            pipe = RecorderPipeline(
                take.session.node_id,
                out,
                mon["width"],
                mon["height"],
                fps=self.cfg.fps,
                bitrate_kbps=self.cfg.video_bitrate_kbps,
                audio_sources=audio_sources,
                mic_source=mic,
                denoise_mic=self.cfg.mic_noise_suppression,
            )
            pipe.on_eos = lambda: self._on_finished(take)
            pipe.on_error = lambda message: self._on_pipe_error(take, message)
            pipe.build()
            pipe.start()
            take.pipeline, take.file = pipe, out
            # one crop update per output frame
            take.tick_id = GLib.timeout_add(
                max(1000 // self.cfg.fps, 8), self._tick, take
            )
            self._set_state(STATE_RECORDING)
            log.info(
                "recording started -> %s (monitor=%s %dx%d node=%d audio=%s mic=%s cursor=%s)",
                out, mon["connector"], mon["width"], mon["height"],
                take.session.node_id, audio_sources or "off", mic or "off",
                take.cursor and take.cursor.describe(),
            )
            return {
                "ok": True,
                "state": self.state,
                "file": str(out),
                "monitor": mon["connector"],
            }
        except Exception as exc:  # never crash the UI
            log.exception("starting the pipeline failed")
            take.close()
            self.take = None
            self._set_state(STATE_IDLE)
            return self._fail(str(exc))

    def _make_cursor(self, take: Take):
        """Real pointer via a second, metadata-only stream when the helper exists."""
        node = None
        if self.cfg.cursor_backend in ("auto", "pipewire") and PipewireCursor.available():
            try:
                take.cursor_session = MutterSession(
                    connector=take.mon["connector"],
                    cursor_mode=CURSOR_METADATA,
                    timeout_ms=self.cfg.capture_timeout_ms,
                )
                node = take.cursor_session.open()
            except Exception:
                log.exception("cursor metadata stream failed; falling back")
        cursor = create_backend(self.cfg.cursor_backend, node)
        cursor.reset(take.mon["width"], take.mon["height"])
        return cursor

    def pause_recording(self) -> dict:
        if self.state != STATE_RECORDING:
            return {"ok": False, "error": f"state: {self.state}"}
        self.take.pipeline.pause()
        self._set_state(STATE_PAUSED)
        log.info("paused")
        return {"ok": True, "state": self.state}

    def resume_recording(self) -> dict:
        if self.state != STATE_PAUSED:
            return {"ok": False, "error": f"state: {self.state}"}
        self.take.pipeline.resume()
        self._set_state(STATE_RECORDING)
        log.info("resumed")
        return {"ok": True, "state": self.state}

    def toggle_pause(self) -> dict:
        if self.state == STATE_RECORDING:
            return self.pause_recording()
        if self.state == STATE_PAUSED:
            return self.resume_recording()
        return {"ok": False, "error": f"state: {self.state}"}

    def toggle_record(self) -> dict:
        """One key for everything: start, cancel the countdown, or stop."""
        if self.state == STATE_IDLE:
            return self.start_recording()
        return self.stop_recording()

    def stop_recording(self) -> dict:
        if self.state == STATE_COUNTDOWN:
            return self.cancel_countdown()
        if self.state not in ACTIVE_STATES:
            return {"ok": False, "error": "no active recording"}
        take, self.take = self.take, None
        take.stop_ticking()
        self._finishing.add(take)
        take.pipeline.stop()  # async EOS; _on_finished closes the take
        self._set_state(STATE_IDLE)
        log.info("stop requested, finalizing -> %s", take.file)
        return {"ok": True, "state": self.state, "file": str(take.file)}

    def toggle_zoom(self) -> dict:
        if self.zoomer is None:
            return {"ok": False, "error": "no active recording"}
        on = self.zoomer.toggle()
        log.info("zoom %s", "on" if on else "off")
        self._notify()
        return {"ok": True, "zoom": on, "state": self.state}

    def _tick(self, take: Take) -> bool:
        """Drive videocrop from the zoom/cursor state (once per frame)."""
        if self.state == STATE_RECORDING and take is self.take:
            cursor = take.cursor.read() if take.cursor else None
            take.pipeline.set_crop(*take.zoomer.tick(cursor))
        return True

    def status(self) -> dict:
        return {
            "ok": True,
            "state": self.state,
            "file": str(self.current_file) if self.current_file else None,
        }

    def shutdown(self, timeout_s: float = 10.0) -> None:
        """App is quitting: stop and wait until every file is finalized."""
        if self.state == STATE_COUNTDOWN:
            self.cancel_countdown()
        elif self.state in ACTIVE_STATES:
            self.stop_recording()
        ctx = GLib.MainContext.default()
        deadline = time.monotonic() + timeout_s
        while self._finishing and time.monotonic() < deadline:
            if not ctx.iteration(False):
                time.sleep(0.01)

    # ------------------------------------------------------------------
    def _on_finished(self, take: Take) -> None:
        log.info("finalized %s", take.file)
        self._finishing.discard(take)
        take.close()
        self._notify()

    def _on_pipe_error(self, take: Take, message: str) -> None:
        log.error("pipeline error: %s", message)
        self._finishing.discard(take)
        take.close()
        if take is self.take:
            self.take = None
            self._set_state(STATE_IDLE)
        self._notify()
        self._fail(message)

    # ------------------------------------------------------------------
    # Live settings from the UI
    # ------------------------------------------------------------------
    def set_zoom(self, zoom: float) -> None:
        self.cfg.zoom = zoom
        if self.zoomer is not None:
            self.zoomer.settings.zoom = zoom

    def set_follow(self, follow: bool) -> None:
        self.cfg.follow_cursor = follow
        if self.zoomer is not None:
            self.zoomer.settings.follow_cursor = follow
