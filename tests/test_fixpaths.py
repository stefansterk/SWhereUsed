"""Fix paths: references whose saved path is gone get the path where the file really is (found by name)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import SWhereUsed as wu  # noqa: E402
from test_rename import RenameBase, FakeDoc  # noqa: E402

OLD = "X:\\\\Old cloud\\\\Work\\\\"


class FixPathsTests(RenameBase):
    def setUp(self):
        super().setUp()
        self.searches_anyway = False

        def ext_refs(p, timeout=60, saved_only=False):
            # As in the reported case: SEARCHING, the Document Manager reports the file found in the assembly's own
            # folder; without searching, the path as saved (which is gone)
            saved = json.loads(Path(p).read_text(encoding="utf-8"))
            if saved_only and not self.searches_anyway:
                return saved
            own = lambda r: str(Path(p).parent / Path(r.replace("\\", "/")).name)   # noqa: E731
            return [own(r) if not Path(r).exists() and Path(own(r)).exists() else r for r in saved]
        self.patch("_ext_refs_isolated", ext_refs)
        v = Path(self.part).parent
        self.elsewhere = v / "Library"
        self.elsewhere.mkdir()
        (self.elsewhere / "Washer.SLDPRT").write_text("part")
        for d in ("A", "B"):
            (v / d).mkdir()
            (v / d / "Clip.SLDPRT").write_text("part")
        db = wu.conn()
        for f in (self.elsewhere / "Washer.SLDPRT", v / "A" / "Clip.SLDPRT", v / "B" / "Clip.SLDPRT"):
            db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?)", (str(f).lower(), str(f), 1, 1, 1, "ok", None, 1, None))
        db.commit()
        Path(self.sub).write_text(json.dumps([OLD + "Bolt.SLDPRT", OLD + "Washer.SLDPRT", OLD + "Clip.SLDPRT", OLD + "Gone.SLDPRT"]))
        self.before_sub = Path(self.sub).read_text()

    def test_plan(self):
        p = wu.fix_paths_plan([self.sub, self.top])
        fixes = {Path(f["old"].replace("\\\\", "/")).name: (f["new"], f["how"]) for f in p["items"][0]["fixes"]}
        self.assertEqual(fixes["Bolt.SLDPRT"], (self.part, "in the assembly's own folder"))
        self.assertEqual(fixes["Washer.SLDPRT"], (str(self.elsewhere / "Washer.SLDPRT"), "the only file with this name"))
        self.assertNotIn("Clip.SLDPRT", fixes)                                   # twice: no guessing
        self.assertTrue(any("Clip.SLDPRT exists 2 times" in u for u in p["unsure"]))
        self.assertTrue(any("Gone.SLDPRT is nowhere" in n for n in p["notfound"]))
        self.assertEqual((p["counts"]["assemblies"], p["counts"]["references"]), (1, 2))   # Top: nothing to fix

    def test_fix_and_undo(self):
        r = wu.fix_paths([self.sub], dry_run=False)
        self.assertTrue(r["result"]["ok"], r["result"])
        refs = self.refs(self.sub)
        self.assertEqual(refs[:2], [self.part, str(self.elsewhere / "Washer.SLDPRT")])
        self.assertEqual(refs[2:], [OLD + "Clip.SLDPRT", OLD + "Gone.SLDPRT"])  # left alone
        entry = wu.history()[0]
        self.assertEqual(entry["kind"], "fixpaths")
        u = wu.undo(entry["id"], dry_run=False)
        self.assertTrue(u["result"]["ok"], u["result"])
        self.assertEqual(self.refs(self.sub)[:2], [OLD + "Bolt.SLDPRT", OLD + "Washer.SLDPRT"])

    def test_failure_puts_the_backup_back(self):
        FakeDoc.fail_save_on = "Sub.SLDASM"
        r = wu.fix_paths([self.sub], dry_run=False)
        self.assertFalse(r["result"]["ok"])
        self.assertEqual(Path(self.sub).read_text(), self.before_sub)

    def test_not_in_a_pdm_vault_or_when_open(self):
        self.patch("pdm_vault", lambda p: "C:\\\\Vault")
        p = wu.fix_paths_plan([self.sub])
        self.assertTrue(any("PDM" in b for b in p["blockers"]))
        self.assertFalse(wu.fix_paths([self.sub], dry_run=False)["executed"])


    def test_when_the_document_manager_searches_anyway(self):
        """Then the saved paths come from the index (the component list as read), and are matched by name."""
        self.searches_anyway = True
        db = wu.conn()
        db.execute("DELETE FROM refs WHERE parent=?", (self.sub.lower(),))
        db.commit()
        self.ref(self.sub, "Default", OLD + "Bolt.SLDPRT")
        p = wu.fix_paths_plan([self.sub])
        self.assertEqual([(f["old"], f["new"]) for f in p["items"][0]["fixes"] if "Bolt" in f["new"]], [(OLD + "Bolt.SLDPRT", self.part)])
        r = wu.fix_paths([self.sub], dry_run=False)
        self.assertTrue(r["result"]["ok"], r["result"])
        self.assertEqual(self.refs(self.sub)[0], self.part)

    def test_the_structure_shows_it_at_once(self):
        db = wu.conn()
        db.execute("DELETE FROM refs WHERE parent=?", (self.sub.lower(),))
        db.commit()
        self.ref(self.sub, "Default", OLD + "Bolt.SLDPRT")
        self.patch("_existing_files", lambda paths: {p.lower() for p in paths if Path(p).exists()})
        before = wu.structure(self.top)                                 # kept in the cache from now on
        bolt = lambda st: next(n for n in st["tree"][0]["children"] if n["name"].lower().startswith("bolt"))  # noqa: E731
        self.assertEqual(bolt(before)["how"], "by_name")
        self.assertTrue(wu.fix_paths([self.sub], dry_run=False)["result"]["ok"])
        after = wu.structure(self.top)                                  # no index run in between
        self.assertEqual((bolt(after)["path"], bolt(after)["how"] != "by_name"), (self.part, True))
