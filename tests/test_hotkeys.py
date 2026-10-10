"""GNOME keybinding helpers (no gsettings calls)."""
import shlex
import unittest

from screenwave import hotkeys
from screenwave.ids import APP_ID, STATUS_IFACE, STATUS_PATH


class HotkeysTest(unittest.TestCase):
    def test_parse_list_shapes(self):
        p = hotkeys._parse_list
        self.assertEqual(p("@as []"), [])
        self.assertEqual(p("['/a/', '/b/']"), ["/a/", "/b/"])
        self.assertEqual(p("['/a/', 'junk']"), ["/a/"])
        self.assertEqual(p("garbage"), [])

    def test_commands_call_the_app_over_dbus(self):
        argv = shlex.split(hotkeys.command_for("record"))
        self.assertEqual(argv[:3], ["gdbus", "call", "--session"])
        self.assertIn(APP_ID, argv)
        self.assertIn(STATUS_PATH, argv)
        self.assertEqual(argv[-2:], [f"{STATUS_IFACE}.Command", "record-toggle"])
        self.assertTrue(hotkeys.command_for("pause").endswith(" toggle"))

    def test_own_paths_never_touch_custom_n(self):
        for action in hotkeys.ACTIONS:
            self.assertIn("/screenwave-", hotkeys._path(action))

    def test_normalize(self):
        self.assertEqual(hotkeys.normalize("<Ctrl><Alt>r"), "<Control><Alt>r")


if __name__ == "__main__":
    unittest.main()
