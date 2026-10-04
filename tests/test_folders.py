"""The folder picker: folders on disk (also empty ones), up, and New folder."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import SWhereUsed as wu  # noqa: E402
from test_whereused import Base  # noqa: E402


class FolderPickerTests(Base):
    def setUp(self):
        super().setUp()
        self.patch("_root_reachable_cached", lambda *a, **k: True)
        self.root = self.dir / "Projects"
        for sub in ("P26050", "P26051", "$RECYCLE.BIN", "Empty"):
            (self.root / sub).mkdir(parents=True)
        (self.root / "file.txt").write_text("not a folder")

    def test_subfolders_also_empty_ones(self):
        d = wu.folders_on_disk(str(self.root))
        self.assertEqual([Path(x).name for x in d["folders"]], ["Empty", "P26050", "P26051"])
        self.assertEqual(d["parent"], str(self.dir))
        self.assertFalse(d["missing"])

    def test_a_folder_that_does_not_exist_yet_opens_above_it(self):
        d = wu.folders_on_disk(str(self.root / "P26050" / "copy" / "deeper"))
        self.assertEqual((d["path"], d["missing"]), (str(self.root / "P26050"), True))

    def test_new_folder(self):
        new = wu.make_folder(str(self.root), "P26052 Cabinet")
        self.assertTrue(Path(new).is_dir())
        for bad in ("", "a/b", "x:y", "trailing.", ".."):
            with self.assertRaises(ValueError, msg=bad):
                wu.make_folder(str(self.root), bad)
        with self.assertRaises(ValueError):
            wu.make_folder(str(self.root), "P26052 Cabinet")              # already there
        with self.assertRaises(ValueError):
            wu.make_folder(str(self.root / "nope"), "x")                   # parent must exist
