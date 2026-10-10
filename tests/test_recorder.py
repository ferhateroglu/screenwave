"""Recorder state machine with the capture and pipeline faked out.

Covers the countdown (including commands arriving while the capture streams
are opening — MutterSession.open() iterates the main loop), pause timing and
overlapping takes.
"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from gi.repository import GLib

from screenwave import recorder as R
from screenwave.config import Config

MONITOR = {"connector": "HDMI-1", "width": 1920, "height": 1080}


class FakeSession:
    instances: list["FakeSession"] = []
    on_open = None  # hook run "while opening", like the real nested loop

    def __init__(self, connector, cursor_mode, timeout_ms):
        self.connector = connector
        self.node_id = None
        self.closed = False
        FakeSession.instances.append(self)

    def open(self):
        if FakeSession.on_open:
            FakeSession.on_open()
        self.node_id = 42
        return self.node_id

    def close(self):
        self.closed = True


class FakePipeline:
    instances: list["FakePipeline"] = []

    def __init__(self, node_id, out, w, h, **kw):
        self.node_id, self.out = node_id, out
        self.paused = False
        self.stopped = False
        self.on_eos = self.on_error = None
        FakePipeline.instances.append(self)

    def build(self):
        pass

    def start(self):
        pass

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def set_crop(self, *crop):
        pass

    def stop(self):
        self.stopped = True

    def finish(self):  # what EOS on the bus would do
        self.on_eos()


class FakeCursor:
    def read(self):
        return (960, 540)

    def reset(self, w, h):
        pass

    def describe(self):
        return "fake"

    def close(self):
        pass


def pump(until, timeout=3.0):
    ctx = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while not until() and time.monotonic() < end:
        if not ctx.iteration(False):
            time.sleep(0.005)
    return until()


class RecorderTest(unittest.TestCase):
    def setUp(self):
        FakeSession.instances, FakeSession.on_open = [], None
        FakePipeline.instances = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patches = [
            mock.patch.object(R, "list_monitors", lambda: [MONITOR]),
            mock.patch.object(R, "pick_monitor", lambda mons, pref="": mons[0] if mons else None),
            mock.patch.object(R, "MutterSession", FakeSession),
            mock.patch.object(R, "RecorderPipeline", FakePipeline),
            mock.patch.object(R, "default_audio_monitor", lambda: "sink.monitor"),
            mock.patch.object(R, "default_microphone", lambda: None),
            mock.patch.object(R.PipewireCursor, "available", staticmethod(lambda: False)),
            mock.patch.object(R, "create_backend", lambda kind, node=None: FakeCursor()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.cfg = Config()
        self.cfg.output_dir = Path(self.tmp.name)
        self.cfg.countdown_seconds = 0
        self.c = R.RecorderController(self.cfg)
        self.errors = []
        self.c.on_error = lambda msg: self.errors.append((msg, self.c.state))
        self.addCleanup(self._teardown_controller)

    def _teardown_controller(self):
        if self.c.state != R.STATE_IDLE:
            self.c.stop_recording()
        for pipe in FakePipeline.instances:
            if pipe.stopped and pipe.on_eos:
                pipe.finish()

    # --------------------------------------------------------------
    def test_start_and_stop_without_countdown(self):
        reply = self.c.handle({"action": "record-toggle"})
        self.assertTrue(reply["ok"])
        self.assertEqual(self.c.state, R.STATE_RECORDING)
        self.assertEqual(self.c.monitor, "HDMI-1")
        pipe = FakePipeline.instances[-1]

        reply = self.c.handle({"action": "record-toggle"})
        self.assertEqual(self.c.state, R.STATE_IDLE)
        self.assertTrue(pipe.stopped)
        self.assertTrue(self.c.finishing)
        pipe.finish()
        self.assertFalse(self.c.finishing)
        self.assertTrue(all(s.closed for s in FakeSession.instances))

    def test_countdown_then_recording(self):
        self.cfg.countdown_seconds = 1
        seen = []
        self.c.listeners.append(lambda: seen.append((self.c.state, self.c.countdown)))
        reply = self.c.start_recording()
        self.assertEqual((reply["state"], reply["countdown"]), (R.STATE_COUNTDOWN, 1))
        self.assertTrue(pump(lambda: self.c.state == R.STATE_RECORDING))
        self.assertIn((R.STATE_COUNTDOWN, 1), seen)
        self.assertEqual(len(FakePipeline.instances), 1)

    def test_cancel_countdown_closes_streams(self):
        self.cfg.countdown_seconds = 2
        self.c.start_recording()
        self.assertTrue(pump(lambda: self.c.take is not None and self.c.take.ready))
        self.c.handle({"action": "record-toggle"})  # same key cancels
        self.assertEqual(self.c.state, R.STATE_IDLE)
        self.assertTrue(all(s.closed for s in FakeSession.instances))
        pump(lambda: False, timeout=2.2)  # past the old deadline
        self.assertEqual(FakePipeline.instances, [])

    def test_cancel_while_streams_are_opening(self):
        self.cfg.countdown_seconds = 1
        FakeSession.on_open = lambda: self.c.stop_recording()
        self.c.start_recording()
        self.assertTrue(pump(lambda: self.c.state == R.STATE_IDLE))
        FakeSession.on_open = None
        pump(lambda: False, timeout=1.3)
        self.assertEqual(FakePipeline.instances, [])
        self.assertIsNone(self.c.take)
        self.assertTrue(all(s.closed for s in FakeSession.instances))

    def test_failed_prepare_reports_error_after_going_idle(self):
        with mock.patch.object(R, "pick_monitor", lambda mons, pref="": None):
            reply = self.c.start_recording()
        self.assertFalse(reply["ok"])
        self.assertEqual(self.errors, [("no monitors found", R.STATE_IDLE)])

    def test_no_monitor_fails_before_any_countdown(self):
        self.cfg.countdown_seconds = 3
        seen = []
        self.c.listeners.append(lambda: seen.append(self.c.state))
        with mock.patch.object(R, "pick_monitor", lambda mons, pref="": None):
            self.assertFalse(self.c.start_recording()["ok"])
        self.assertEqual(seen, [])
        self.assertEqual(self.errors, [("no monitors found", R.STATE_IDLE)])

    def test_capture_failure_during_countdown(self):
        self.cfg.countdown_seconds = 1

        def boom():
            raise RuntimeError("PipeWireStreamAdded never arrived")

        FakeSession.on_open = boom
        self.c.start_recording()
        self.assertTrue(pump(lambda: self.errors))
        self.assertEqual(self.errors, [("PipeWireStreamAdded never arrived", R.STATE_IDLE)])
        self.assertIsNone(self.c.take)
        self.assertTrue(all(s.closed for s in FakeSession.instances))

    def test_monitor_is_known_from_the_first_countdown_signal(self):
        self.cfg.countdown_seconds = 2
        seen = []
        self.c.listeners.append(lambda: seen.append((self.c.state, self.c.monitor)))
        self.c.start_recording()
        self.assertEqual(seen[0], (R.STATE_COUNTDOWN, "HDMI-1"))

    def test_cancel_and_restart_while_streams_are_opening(self):
        self.cfg.countdown_seconds = 1
        calls = []

        def cancel_then_restart():
            if not calls:
                calls.append(1)
                self.c.stop_recording()
                self.c.start_recording()

        FakeSession.on_open = cancel_then_restart
        self.c.start_recording()
        self.assertTrue(pump(lambda: self.c.state == R.STATE_RECORDING))
        self.assertEqual(len(FakePipeline.instances), 1)
        self.assertFalse(self.c.take.session.closed)
        first_takes_sessions = [s for s in FakeSession.instances if s is not self.c.take.session
                                and s is not self.c.take.cursor_session]
        self.assertTrue(all(s.closed for s in first_takes_sessions))

    def test_pause_resume_and_elapsed_excludes_pauses(self):
        clock = [1000.0]
        with mock.patch.object(R.time, "monotonic", lambda: clock[0]):
            self.c.start_recording()
            clock[0] += 5
            self.c.handle({"action": "toggle"})
            self.assertEqual(self.c.state, R.STATE_PAUSED)
            self.assertTrue(FakePipeline.instances[-1].paused)
            clock[0] += 100  # paused time doesn't count
            self.assertAlmostEqual(self.c.elapsed(), 5)
            self.c.handle({"action": "toggle"})
            clock[0] += 2
            self.assertAlmostEqual(self.c.elapsed(), 7)

    def test_stop_from_paused(self):
        self.c.start_recording()
        self.c.pause_recording()
        self.c.handle({"action": "record-toggle"})
        self.assertEqual(self.c.state, R.STATE_IDLE)

    def test_new_take_survives_previous_finalize(self):
        self.c.start_recording()
        first = FakePipeline.instances[-1]
        self.c.stop_recording()
        self.c.start_recording()  # before the first file is finalized
        first.finish()
        self.assertEqual(self.c.state, R.STATE_RECORDING)
        self.assertFalse(self.c.take.session.closed)

    def test_zoom_and_unknown_action(self):
        self.assertFalse(self.c.handle({"action": "zoom"})["ok"])  # nothing recording
        self.c.start_recording()
        self.assertTrue(self.c.handle({"action": "zoom"})["zoom"])
        self.assertTrue(self.c.zoom_on)
        self.assertFalse(self.c.handle({"action": "bogus"})["ok"])

    def test_shutdown_waits_for_finalize(self):
        self.c.start_recording()
        pipe = FakePipeline.instances[-1]
        GLib.timeout_add(100, lambda: pipe.finish() or False)
        self.c.shutdown(timeout_s=2)
        self.assertFalse(self.c.finishing)


if __name__ == "__main__":
    unittest.main()
