"""Copies of a project in two folders, each with its own file of the same name. The saved path in both assemblies
is gone (an old location); SOLIDWORKS then takes the file in the assembly's own folder. So folder B's assembly
uses folder B's file, not folder A's: it must not be counted for, or updated with, folder A's file."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import SWhereUsed as wu  # noqa: E402
from test_whereused import Base  # noqa: E402

OLD = "X:\\\\Old cloud\\\\Cabinet\\\\Montageplaat.SLDPRT"           # where both assemblies once found it


class OwnCopyTests(Base):
    def setUp(self):
        super().setUp()
        self.patch("_root_reachable_cached", lambda *a, **k: True)
        self.f = {}
        for m in ("A", "B"):
            d = self.dir / m
            d.mkdir()
            (d / "Montageplaat.SLDPRT").write_text("part " + m)
            (d / "CB-000.SLDASM").write_text("asm " + m)
            self.f[m] = (str(d / "CB-000.SLDASM"), str(d / "Montageplaat.SLDPRT"))
            self.ref(self.f[m][0], "Default", OLD)
            db = wu.conn()
            db.execute("UPDATE files SET display_path=? WHERE path=?", (self.f[m][0], self.f[m][0].lower()))
            db.commit()

    def test_where_used_counts_only_its_own_folder(self):
        a_asm, a_part = self.f["A"]
        b_asm, b_part = self.f["B"]
        self.assertEqual([x["path"] for x in wu.whereused(a_part)["direct"]], [a_asm])
        self.assertEqual([x["path"] for x in wu.whereused(b_part)["direct"]], [b_asm])
        shown = {x["path"]: x["match"] for x in wu.whereused(a_part, include_other=True)["direct"]}
        self.assertEqual(shown, {a_asm: "path_missing", b_asm: "own_copy"})       # visible with "Other copies"
        self.assertEqual(wu.usage_for_paths([a_part])[a_part.lower()], (1, 1))

    def test_rename_leaves_the_other_copy_alone_unless_chosen(self):
        a_asm, a_part = self.f["A"]
        b_asm, _ = self.f["B"]
        plan = wu.rename_plan(a_part, "1131111M_Montageplaat", with_drawing=False)
        self.assertEqual([p["path"] for p in plan["parents"]], [a_asm])
        self.assertEqual(plan["own_copies"], [b_asm])
        self.assertTrue(any("their own file with this name" in w for w in plan["warnings"]))
        both = wu.rename_plan(a_part, "1131111M_Montageplaat", with_drawing=False, include_own=True)
        self.assertEqual(sorted(p["path"] for p in both["parents"]), sorted([a_asm, b_asm]))

    def test_without_its_own_file_it_is_still_found_by_name(self):
        b_asm, b_part = self.f["B"]
        Path(b_part).unlink()                                       # folder B has no plate of its own
        a_asm, a_part = self.f["A"]
        self.assertEqual(sorted(x["path"] for x in wu.whereused(a_part)["direct"]), sorted([a_asm, b_asm]))

    def test_structure_shows_its_own_file(self):
        b_asm, b_part = self.f["B"]
        node = wu.structure(b_asm)["tree"][0]
        self.assertEqual((node["path"], node["how"]), (b_part, "by_name"))
