"""Settings load/save and migrations."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from screenwave.config import DEFAULT_HOTKEYS, Config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, data):
        d = Path(self.tmp.name) / "screenwave"
        d.mkdir(parents=True, exist_ok=True)
        (d / "settings.json").write_text(json.dumps(data))

    def test_defaults_without_file(self):
        cfg = Config.load()
        self.assertEqual(cfg.countdown_seconds, 3)
        self.assertEqual(cfg.zoom, 1.5)
        self.assertEqual(cfg.hotkeys, DEFAULT_HOTKEYS)

    def test_v2_file_is_migrated(self):
        self.write({
            "settings_version": 2, "zoom": 2.5, "fps": 60,
            "socket_path": "/old/control.sock", "restore_token": "x",
            "hotkeys": {"record": "<Ctrl><Alt>x"},
        })
        cfg = Config.load()
        self.assertEqual(cfg.zoom, 1.5)             # v3: gentler zoom
        self.assertEqual(cfg.countdown_seconds, 3)  # v4: countdown
        self.assertEqual(cfg.settings_version, 4)
        self.assertEqual(cfg.fps, 60)
        self.assertFalse(hasattr(cfg, "socket_path"))
        self.assertEqual(cfg.hotkeys["record"], "<Ctrl><Alt>x")
        self.assertEqual(cfg.hotkeys["pause"], DEFAULT_HOTKEYS["pause"])

    def test_current_file_is_kept_as_is(self):
        self.write({"settings_version": 4, "zoom": 2.0, "countdown_seconds": 0})
        cfg = Config.load()
        self.assertEqual((cfg.zoom, cfg.countdown_seconds), (2.0, 0))

    def test_save_round_trip(self):
        cfg = Config.load()
        cfg.countdown_seconds = 5
        cfg.save()
        self.assertEqual(Config.load().countdown_seconds, 5)

    def test_broken_file_falls_back_to_defaults(self):
        d = Path(self.tmp.name) / "screenwave"
        d.mkdir(parents=True)
        (d / "settings.json").write_text("{not json")
        self.assertEqual(Config.load().countdown_seconds, 3)


if __name__ == "__main__":
    unittest.main()
