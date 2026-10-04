"""History and Undo: a rename is undone by renaming back (references and all); Pack & Go by removing the copies."""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import SWhereUsed as wu  # noqa: E402
from test_rename import RenameBase  # noqa: E402


class UndoRenameTests(RenameBase):
    def test_rename_and_undo(self):
        before = {f: Path(f).read_text(encoding="utf-8") for f in (self.sub, self.drw, self.top)}
        r = wu.rename_execute(self.part, "Bolt-M8", None, True)
        self.assertTrue(r["result"]["ok"], r["result"])
        entry = wu.history()[0]
        self.assertEqual((entry["kind"], entry["items"][0]["new"]), ("rename", str(Path(self.part).with_name("Bolt-M8.SLDPRT"))))
        plan = wu.undo(entry["id"], dry_run=True)["plan"]
        self.assertEqual(plan["blockers"], [])
        u = wu.undo(entry["id"], dry_run=False)
        self.assertTrue(u["result"]["ok"], u["result"])
        self.assertTrue(Path(self.part).exists() and Path(self.drw).exists())          # name and drawing back
        self.assertEqual(self.refs(self.sub)[0], self.part)                              # references back
        self.assertEqual(json.loads(Path(self.drw).read_text())[0], self.part)
        h = wu.history()
        self.assertEqual(h[0]["undo_of"], entry["id"])
        self.assertIsNotNone(next(e for e in h if e["id"] == entry["id"])["undone"])
        with self.assertRaises(ValueError):
            wu.undo(entry["id"], dry_run=True)                                          # not twice

    def test_move_and_undo(self):
        target = str(Path(self.part).parent / "moved")
        Path(target).mkdir()
        r = wu.rename_batch([{"path": self.part, "name": "Bolt", "folder": target}], True, False)
        self.assertTrue(r["result"]["ok"], r["result"])
        u = wu.undo(wu.history()[0]["id"], dry_run=False)
        self.assertTrue(u["result"]["ok"], u["result"])
        self.assertTrue(Path(self.part).exists())
        self.assertFalse((Path(target) / "Bolt.SLDPRT").exists())

    def test_undo_is_stopped_when_the_old_name_is_taken_again(self):
        wu.rename_execute(self.part, "Bolt-M8", None, True)
        Path(self.part).write_text("someone made a new Bolt", encoding="utf-8")
        plan = wu.undo(wu.history()[0]["id"], dry_run=True)["plan"]
        self.assertTrue(plan["blockers"])
        self.assertFalse(wu.undo(wu.history()[0]["id"], dry_run=False)["executed"])


class UndoPackGoTests(RenameBase):
    def items(self):
        return [{"path": p, "name": "NEW-" + Path(p).stem, "copy": True} for p in (self.top, self.sub, self.part)]

    def test_undo_removes_the_copies_and_the_folders_made(self):
        target = self.dir / "copy" / "P26050"
        r = wu.packgo(self.top, self.items(), str(target), True, True, dry_run=False)
        self.assertTrue(r["result"]["ok"], r["result"])
        entry = wu.history()[0]
        self.assertEqual(entry["kind"], "packgo")
        u = wu.undo(entry["id"], dry_run=False)
        self.assertTrue(u["result"]["ok"], u)
        self.assertFalse((self.dir / "copy").exists())                                  # copies and new folders gone
        self.assertTrue(Path(self.top).exists())                                          # originals of course still there

    def test_changed_copy_stops_unless_forced(self):
        target = self.dir / "copy"
        wu.packgo(self.top, self.items(), str(target), True, True, dry_run=False)
        f = target / "NEW-Bolt.SLDPRT"
        f.write_text("worked on after Pack & Go", encoding="utf-8")
        os.utime(f, (time.time() + 60, time.time() + 60))
        entry = wu.history()[0]
        plan = wu.undo(entry["id"], dry_run=True)
        self.assertTrue(any("changed after Pack & Go" in b for b in plan["blockers"]), plan)
        self.assertFalse(wu.undo(entry["id"], dry_run=False)["executed"])
        self.assertTrue(f.exists())
        self.assertTrue(wu.undo(entry["id"], dry_run=False, force=True)["executed"])
        self.assertFalse(f.exists())

    def test_overwritten_file_comes_back(self):
        target = self.dir / "copy"
        target.mkdir()
        (target / "NEW-Bolt.SLDPRT").write_text("the file that was there", encoding="utf-8")
        r = wu.packgo(self.top, self.items(), str(target), True, True, dry_run=False, overwrite=True)
        self.assertTrue(r["result"]["ok"], r["result"])
        u = wu.undo(wu.history()[0]["id"], dry_run=False)
        self.assertTrue(u["result"]["ok"], u)
        self.assertEqual((target / "NEW-Bolt.SLDPRT").read_text(encoding="utf-8"), "the file that was there")
