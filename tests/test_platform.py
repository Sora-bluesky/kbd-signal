"""Contract tests for kbd_signal._platform.

The darwin state_dir branch and the relative-XDG guard are never exercised by
the windows/ubuntu CI cells, so their contracts are pinned here by patching
sys.platform. The lock and detach checks run against whatever OS hosts the
test."""

import os
import sys
import tempfile
import unittest
from unittest import mock

from kbd_signal import _platform


class StateDirTests(unittest.TestCase):
    def _resolve(self, platform, environ, home="/home/u"):
        with mock.patch.object(_platform.sys, "platform", platform), \
                mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(os.path, "expanduser",
                                  lambda p: p.replace("~", home, 1)):
            return _platform.state_dir()

    def test_windows_uses_localappdata(self):
        local = os.path.join(os.sep, "AppData", "Local")
        self.assertEqual(
            self._resolve("win32", {"LOCALAPPDATA": local}),
            os.path.join(local, "kbd-signal"),
        )

    def test_macos_uses_application_support(self):
        self.assertEqual(
            self._resolve("darwin", {}),
            os.path.join("/home/u", "Library", "Application Support",
                         "kbd-signal"),
        )

    def test_linux_uses_absolute_xdg(self):
        # Absolute per the *host* os.path (ntpath on Windows, posixpath on the
        # POSIX CI cells) so isabs() agrees regardless of where the test runs.
        xdg = os.path.abspath(os.path.join("custom", "xdg"))
        self.assertEqual(
            self._resolve("linux", {"XDG_STATE_HOME": xdg}),
            os.path.join(xdg, "kbd-signal"),
        )

    def test_linux_ignores_relative_xdg(self):
        # A relative XDG_STATE_HOME must not be honoured (it would make the
        # state dir depend on the process CWD); fall back to ~/.local/state.
        self.assertEqual(
            self._resolve("linux", {"XDG_STATE_HOME": os.path.join("rel", "x")}),
            os.path.join("/home/u", ".local", "state", "kbd-signal"),
        )

    def test_linux_default_without_xdg(self):
        self.assertEqual(
            self._resolve("linux", {}),
            os.path.join("/home/u", ".local", "state", "kbd-signal"),
        )


class LockTests(unittest.TestCase):
    def test_try_lock_unlock_roundtrip(self):
        # Mirrors states._state_lock: an "a+b" handle on a fresh lock file.
        # A free lock must acquire without raising, and re-acquiring after
        # unlock proves the release actually happened.
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "state.lock"), "a+b") as f:
                _platform.try_lock(f)
                _platform.unlock(f)
                _platform.try_lock(f)
                _platform.unlock(f)


class DetachKwargsTests(unittest.TestCase):
    def test_keys_match_platform(self):
        kw = _platform.detach_kwargs()
        if sys.platform == "win32":
            self.assertIn("creationflags", kw)
            self.assertNotIn("start_new_session", kw)
        else:
            self.assertTrue(kw.get("start_new_session"))
            self.assertNotIn("creationflags", kw)


if __name__ == "__main__":
    unittest.main()
