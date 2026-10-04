"""The files of the app refer to each other by name: every file a .bat starts or needs must be there (a rename once
left "%~dp0WhereUsed.py" behind, and Start SWhereUsed.bat then said it ran from inside the zip)."""
import re
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
ONLY_DURING_AN_UPDATE = {"Start SWhereUsed.bat.new"}


class FilesTests(unittest.TestCase):
    def test_every_file_a_bat_refers_to_is_there(self):
        for bat in APP.glob("*.bat"):
            for name in re.findall(r'%~dp0([^"%\r\n]+)', bat.read_text(encoding="utf-8", errors="replace")):
                if name in ONLY_DURING_AN_UPDATE:
                    continue
                self.assertTrue((APP / name).exists(), f"{bat.name} refers to {name}, which is not in the app folder")

    def test_the_server_serves_the_page_that_is_there(self):
        src = (APP / "SWhereUsed.py").read_text(encoding="utf-8")
        for name in re.findall(r'HERE / "([^"]+)"', src):
            if name.endswith((".html", ".ico", ".bat")):
                self.assertTrue((APP / name).exists(), f"SWhereUsed.py uses {name}, which is not in the app folder")
