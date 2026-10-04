"""Tests for the structure of an assembly (tree and flat list), built from the index."""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import SWhereUsed as wu  # noqa: E402
from test_whereused import Base  # noqa: E402

V = "G:\\V\\"
REAL_EXISTING_FILES = wu._existing_files          # setUp replaces it; one test checks the real one


class StructureTests(Base):
    def setUp(self):
        super().setUp()
        top, sub, sub2 = V + "Top.SLDASM", V + "Sub.SLDASM", V + "Sub2.SLDASM"
        # Top, config A: 3x Sub, 2x Bolt, 1x Washer (suppressed), a virtual part, Sub2 saved at an old place
        self.ref(top, "A", sub, qty=3, ccfg="Default")
        self.ref(top, "A", V + "Bolt.SLDPRT", qty=2, ccfg="M8")
        self.ref(top, "A", V + "Washer.SLDPRT", qty=1, supp=1)
        self.ref(top, "A", "C:\\x\\swx12\\IC~~\\Skeleton^Top.SLDPRT", virt=1)
        self.ref(top, "A", "X:\\Old\\Sub2.SLDASM", qty=1, ccfg="Default")
        self.ref(top, "B", sub, qty=1, ccfg="Default")
        # Sub: 4x Bolt, 2x Nut; a part that no longer exists
        self.ref(sub, "Default", V + "Bolt.SLDPRT", qty=4, ccfg="M8")
        self.ref(sub, "Default", V + "Nut.SLDPRT", qty=2)
        self.ref(sub, "Default", V + "Gone.SLDPRT", qty=1)
        self.ref(sub, "Default", "E:\\Old\\Bolt.SLDPRT", qty=1, ccfg="M8")   # old path; Bolt is loaded already
        # Sub2 contains Top: a loop (can happen with broken files); must not hang
        self.ref(sub2, "Default", top, qty=1, ccfg="A")
        self.ref(sub2, "Default", V + "Nut.SLDPRT", qty=5)
        on_disk = {(V + n).lower() for n in ("Bolt.SLDPRT", "Nut.SLDPRT", "Washer.SLDPRT")}
        self.patch("_existing_files", lambda paths: {p.lower() for p in paths} & on_disk)
        self.patch("_root_reachable_cached", lambda *a, **k: True)

    def test_tree(self):
        s = wu.structure(V + "Top.SLDASM", "A")
        self.assertEqual(s["configs"], ["A", "B"])
        names = [(n["name"], n["qty"], n["suppressed"], n["virtual"]) for n in s["tree"]]
        self.assertEqual(names, [("Sub.SLDASM", 3, False, False), ("Bolt.SLDPRT", 2, False, False),
                                 ("Washer.SLDPRT", 1, True, False), ("Skeleton^Top.SLDPRT", 1, False, True),
                                 ("Sub2.SLDASM", 1, False, False)])
        sub = s["tree"][0]
        self.assertEqual([c["name"] for c in sub["children"]], ["Bolt.SLDPRT", "Nut.SLDPRT", "Gone.SLDPRT", "Bolt.SLDPRT"])
        self.assertEqual((sub["children"][3]["how"], sub["children"][3]["path"]), ("by_name", V + "Bolt.SLDPRT"))
        self.assertEqual(sub["children"][2]["how"], "missing")
        self.assertEqual(sub["children"][0]["how"], "stored")
        sub2 = s["tree"][4]
        self.assertEqual((sub2["how"], sub2["path"]), ("by_name", V + "Sub2.SLDASM"))      # found where it is now
        self.assertTrue(sub2["children"][0].get("cycle"))                                   # Top inside Sub2: stop

    def test_flat_quantities(self):
        flat = {f["name"]: f for f in wu.structure(V + "Top.SLDASM", "A")["flat"]}
        self.assertEqual(flat["Bolt.SLDPRT"]["qty"], 2 + 3 * (4 + 1))    # directly + in each Sub (once by name)
        self.assertEqual(flat["Nut.SLDPRT"]["qty"], 3 * 2 + 1 * 5)       # in Sub, and in Sub2
        self.assertEqual(flat["Sub.SLDASM"]["qty"], 3)
        self.assertEqual((flat["Washer.SLDPRT"]["qty"], flat["Washer.SLDPRT"]["suppressed_only"]), (0, True))
        self.assertEqual(flat["Nut.SLDPRT"]["parents"], 2)
        self.assertNotIn("Skeleton^Top.SLDPRT", flat)                    # virtual: not a file
        self.assertEqual(flat["Bolt.SLDPRT"]["configs"], ["M8"])
        self.assertEqual(flat["Bolt.SLDPRT"]["used_in"], 2)              # Top and Sub, in the whole index

    def test_other_configuration(self):
        s = wu.structure(V + "Top.SLDASM", "B")
        flat = {f["name"]: f["qty"] for f in s["flat"]}
        self.assertEqual(flat, {"Sub.SLDASM": 1, "Bolt.SLDPRT": 5, "Nut.SLDPRT": 2, "Gone.SLDPRT": 1})
        self.assertEqual(wu.structure(V + "Top.SLDASM", "Nope")["config"], "A")     # unknown: first one

    def test_not_in_index(self):
        s = wu.structure(V + "Unknown.SLDASM")
        self.assertFalse(s["known"])

    def test_existing_files_lists_each_folder_once(self):
        calls = []

        class Entries:
            def __init__(self, names):
                self.entries = [type("E", (), {"name": n})() for n in names]

            def __enter__(self):
                return iter(self.entries)

            def __exit__(self, *a):
                return False
        disk = {"g:\\v": ["Bolt.SLDPRT", "Nut.SLDPRT"], "g:\\w": ["A.SLDPRT"]}
        with mock.patch.object(wu.os, "scandir", lambda p: calls.append(p) or Entries(disk.get(p.lower(), []))):
            found = REAL_EXISTING_FILES([V + "Bolt.SLDPRT", V + "Nut.SLDPRT", V + "Missing.SLDPRT", "G:\\W\\A.SLDPRT"])
        self.assertEqual(found, {"g:\\v\\bolt.sldprt", "g:\\v\\nut.sldprt", "g:\\w\\a.sldprt"})
        self.assertEqual(len(calls), 2)                     # two folders: two listings, not four questions
