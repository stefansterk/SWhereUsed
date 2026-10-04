"""Check for updates and install one: download, check, back up, put in place (restart is not done in a test)."""
import io
import json
import sys
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import SWhereUsed as wu  # noqa: E402
from test_whereused import Base  # noqa: E402


def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


class UpdateInstallTests(Base):
    def setUp(self):
        super().setUp()
        self.prog = self.dir / "program"
        self.prog.mkdir()
        for name, text in (("SWhereUsed.py", 'APP_VERSION = "1.33.0"\n'), ("SWhereUsed.html", "old page"),
                           ("Start SWhereUsed.bat", "old bat"), ("README.md", "old readme")):
            (self.prog / name).write_text(text, encoding="utf-8")
        self.patch("HERE", self.prog)
        self.patch("APP_VERSION", "1.33.0")
        self.patch("UPDATE_REPO", "stefansterk/whereused")
        self.patch("_restart_app", lambda: None)
        self.restarts = []
        self.zip = make_zip({"SWhereUsed/SWhereUsed.py": 'APP_VERSION = "1.34.0"\n', "SWhereUsed/SWhereUsed.html": "new page",
                             "SWhereUsed/Start SWhereUsed.bat": "new bat", "SWhereUsed/docs/new.png": "png"})

    def serve(self, zip_bytes, tag="v1.34.0"):
        release = json.dumps({"tag_name": tag, "html_url": "https://github.com/x/releases/tag/" + tag, "body": "- New things",
                              "assets": [{"name": "SWhereUsed-1.34.0.zip", "browser_download_url": "https://github.com/x/SWhereUsed-1.34.0.zip"}]}).encode()

        def urlopen(req, timeout=0):
            url = req.full_url if hasattr(req, "full_url") else req
            body = release if "api.github.com" in url else zip_bytes
            return mock.MagicMock(__enter__=lambda s: io.BytesIO(body), __exit__=lambda *a: False)
        self.patch_obj = mock.patch.object(wu.urllib.request, "urlopen", urlopen)
        self.patch_obj.start()
        self.addCleanup(self.patch_obj.stop)

    def test_check_and_install(self):
        self.serve(self.zip)
        st = wu.update_status(force=True)
        self.assertEqual((st["available"], st["latest"], st["notes"]), (True, "1.34.0", "- New things"))
        r = wu.install_update()
        self.assertEqual(r["version"], "1.34.0")
        self.assertEqual((self.prog / "SWhereUsed.html").read_text(), "new page")
        self.assertEqual((self.prog / "docs" / "new.png").read_text(), "png")
        self.assertEqual((self.prog / "Start SWhereUsed.bat").read_text(), "old bat")           # the running .bat: left alone
        self.assertEqual((self.prog / "Start SWhereUsed.bat.new").read_text(), "new bat")       # swapped by the .bat itself
        self.assertEqual((Path(r["backup"]) / "SWhereUsed.html").read_text(), "old page")      # the old version kept
        self.assertEqual((self.prog / "README.md").read_text(), "old readme")                 # not in the download: stays

    def test_refused_downloads(self):
        bad = {"no page": make_zip({"SWhereUsed/SWhereUsed.py": 'APP_VERSION = "1.34.0"\n'}),
               "not newer": make_zip({"SWhereUsed/SWhereUsed.py": 'APP_VERSION = "1.33.0"\n', "SWhereUsed/SWhereUsed.html": "x"}),
               "outside": make_zip({"SWhereUsed/SWhereUsed.py": 'APP_VERSION = "1.34.0"\n', "SWhereUsed/SWhereUsed.html": "x",
                                    "SWhereUsed/../../evil.txt": "x"}),
               "not a zip": b"<html>an error page</html>"}
        for why, data in bad.items():
            with self.subTest(why):
                self.serve(data)
                wu.update_status(force=True)
                with self.assertRaises(ValueError):
                    wu.install_update()
                self.patch_obj.stop()
        self.assertFalse((self.dir / "evil.txt").exists())
        self.assertEqual((self.prog / "SWhereUsed.html").read_text(), "old page")

    def test_not_while_busy_and_off_without_repository(self):
        self.serve(self.zip)
        wu.update_status(force=True)
        with mock.patch.dict(wu._state, {"running": True}):
            with self.assertRaises(ValueError):
                wu.install_update()
        with mock.patch.object(wu, "UPDATE_REPO", ""):
            self.assertFalse(wu.update_status()["enabled"])
