"""Running in the background: helpers use python.exe without a window, autostart in two ways, and Stop."""
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import SWhereUsed as wu  # noqa: E402
from test_whereused import Base  # noqa: E402


class BackgroundTests(Base):
    def test_helpers_use_python_exe(self):
        scripts = self.dir / "venv" / "Scripts"
        scripts.mkdir(parents=True)
        for n in ("python.exe", "pythonw.exe"):
            (scripts / n).write_text("")
        with mock.patch.object(wu.sys, "executable", str(scripts / "pythonw.exe")):
            self.assertEqual(Path(wu._console_python()).name, "python.exe")
        with mock.patch.object(wu.sys, "executable", str(scripts / "python.exe")):
            self.assertEqual(Path(wu._console_python()).name, "python.exe")

    def test_autostart_in_a_window_or_in_the_background(self):
        appdata = self.dir / "appdata"
        with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
            (self.dir / "Start SWhereUsed.bat").write_text("bat")
            self.patch("HERE", self.dir)
            st = wu.set_autostart(True, "window")
            self.assertEqual((st["on"], st["mode"]), (True, "window"))
            self.assertTrue(wu._startup_file().is_file())
            wu._startup_file().unlink()                              # as if made in the background way
            wu._startup_link().write_text("shortcut")
            st = wu.autostart_state()
            self.assertEqual((st["on"], st["mode"]), (True, "background"))
            st = wu.set_autostart(False)                              # off: both kinds removed
            self.assertEqual(st["on"], False)
            self.assertFalse(wu._startup_link().exists())

    def test_stop_also_stops_what_is_busy(self):
        busy = mock.MagicMock()
        busy.poll.return_value = None
        wu._children.add(busy)
        killed = []
        self.patch("_kill_tree", lambda p: killed.append(p))
        with mock.patch.object(wu.os, "_exit", side_effect=SystemExit) as ex:
            with self.assertRaises(SystemExit):
                wu.shutdown_app()
        self.assertIn(busy, killed)
        ex.assert_called_once_with(0)
        wu._children.discard(busy)

    def test_autostart_moves_to_the_new_name(self):
        appdata = self.dir / "appdata"
        with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
            (self.dir / "Start SWhereUsed.bat").write_text("bat")
            self.patch("HERE", self.dir)
            startup = wu._startup_file().parent
            startup.mkdir(parents=True)
            (startup / "WhereUsed.cmd").write_text("start the old WhereUsed")      # as the old version left it
            wu._migrate_autostart()
            self.assertFalse((startup / "WhereUsed.cmd").exists())
            self.assertTrue((startup / "SWhereUsed.cmd").is_file())
            self.assertIn("Start SWhereUsed.bat", (startup / "SWhereUsed.cmd").read_text())
            wu._migrate_autostart()                                                # nothing left to move: no change
            self.assertTrue((startup / "SWhereUsed.cmd").is_file())
