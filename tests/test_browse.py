"""Tests for browsing the folder structure of the index. Disk access is simulated."""
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import SWhereUsed as wu  # noqa: E402
from test_whereused import Base  # noqa: E402


class FakeEntry(SimpleNamespace):
    def is_file(self):
        return not getattr(self, "folder", False)

    def is_dir(self):
        return getattr(self, "folder", False)

    def stat(self):
        return SimpleNamespace(st_mtime=1000.0, st_size=10)


class FakeScandir:
    def __init__(self, entries):
        self.entries = entries

    @staticmethod
    def disk(tree):
        """A fake disk: {folder: [names]}; names ending in \\ are folders. Counts the listings."""
        calls = []

        def scandir(p):
            calls.append(p)
            names = tree.get(str(p).rstrip("\\").lower(), [])
            return FakeScandir([FakeEntry(name=n.rstrip("\\"), path=str(p).rstrip("\\") + "\\" + n.rstrip("\\"),
                                          folder=n.endswith("\\")) for n in names])
        return scandir, calls

    def __enter__(self):
        return iter(self.entries)

    def __exit__(self, *a):
        return False


class BrowseTests(Base):
    def setUp(self):
        super().setUp()
        wu._tree.update(key=None, folders={}, roots=[])
        wu._isdir_cache.clear()
        self.ref("G:\\V\\Top.SLDASM", "Default", "G:\\V\\Sub\\Sub.SLDASM")
        self.ref("G:\\V\\Sub\\Sub.SLDASM", "Default", "G:\\V\\Parts\\Bolt.SLDPRT", qty=4)
        self.ref("G:\\V\\Sub\\Sub.SLDDRW", "", "G:\\V\\Sub\\Sub.SLDASM")
        self.ref("G:\\V\\Sub\\Sub.SLDASM", "Default", "X:\\Old\\Gone.SLDPRT")
        self.ref("G:\\V\\Sub\\Sub.SLDASM", "Default", "C:\\Users\\a\\AppData\\Local\\Temp\\swx12\\Tmp.SLDPRT")
        states = {"x:\\": "unreachable", "x:\\old": "unreachable", "g:\\v\\oldstuff": "missing"}
        self.patch("_folder_state", lambda p, ttl=300: states.get(p.lower(), "ok"))

    def test_roots_and_counts(self):
        r = wu.browse("")
        self.assertEqual([(e["path"], e["state"]) for e in r["subfolders"]], [("G:\\", "ok"), ("X:\\", "unreachable")])
        g = wu.browse("G:\\", with_files=False)
        v = g["subfolders"][0]
        self.assertEqual((v["name"], v["assemblies"], v["drawings"], v["referenced"], v["subfolders"]),
                         ("V", 2, 1, 2, 2))        # referenced files: Sub.SLDASM (counted once) and Bolt
        self.assertNotIn("temp", str(wu._build_tree()["folders"].keys()).lower())

    def test_folder_contents_from_disk_and_index(self):
        scandir, calls = FakeScandir.disk({"g:\\v": ["Parts\\", "Sub\\", "Top.SLDASM", "New.SLDASM", "Unused.SLDPRT",
                                                     "~$Top.SLDASM", "notes.txt"]})
        with mock.patch.object(wu.os, "scandir", scandir):
            r = wu.browse("G:\\V")
        self.assertEqual(len(calls), 1)                                  # one listing for folders AND files
        self.assertEqual([s["name"] for s in r["subfolders"]], ["Parts", "Sub"])
        files = {wu._fname(f["path"]): f for f in r["files"]}
        self.assertEqual(sorted(files), ["New.SLDASM", "Top.SLDASM", "Unused.SLDPRT"])
        self.assertEqual(files["Top.SLDASM"]["index_status"], "ok")
        self.assertEqual(files["New.SLDASM"]["index_status"], "new")
        self.assertEqual(files["Unused.SLDPRT"]["used_in"], 0)
        self.assertEqual(files["Unused.SLDPRT"]["index_status"], "new")          # parts are indexed too now
        scandir, _calls = FakeScandir.disk({"g:\\v": ["Unused.SLDPRT"]})
        with mock.patch.object(wu.os, "scandir", scandir), mock.patch.object(wu, "INCLUDE_PARTS", "no"):
            r = wu.browse("G:\\V")
        unused = next(f for f in r["files"] if f["path"].endswith("Unused.SLDPRT"))
        self.assertNotIn("index_status", unused)
        self.assertEqual(r["parent"], "G:\\")
        self.assertEqual(r["ancestors"], ["G:\\", "G:\\V"])

    def test_used_in_and_missing_subfolders(self):
        self.ref("G:\\V\\Top.SLDASM", "Default", "G:\\V\\OldStuff\\Thing.SLDPRT")   # folder no longer exists
        scandir, _calls = FakeScandir.disk({"g:\\v": ["Parts\\", "Sub\\"], "g:\\v\\sub": ["Sub.SLDASM"]})
        with mock.patch.object(wu.os, "scandir", scandir):
            sub = wu.browse("G:\\V\\Sub")
            v = wu.browse("G:\\V")
        self.assertEqual({wu._fname(f["path"]): f["used_in"] for f in sub["files"]}["Sub.SLDASM"], 2)
        self.assertNotIn("OldStuff", [s["name"] for s in v["subfolders"]])

    def test_many_subfolders_cost_one_listing(self):
        for i in range(300):
            self.ref(f"G:\\V\\Projects\\P{i:03d}\\A.SLDASM", "Default", "G:\\V\\Parts\\Bolt.SLDPRT")
        states = []
        self.patch("_folder_state", lambda p, ttl=300: states.append(p) or "ok")
        disk = {"g:\\v\\projects": [f"P{i:03d}\\" for i in range(299)]}   # P299 was deleted from disk
        scandir, calls = FakeScandir.disk(disk)
        with mock.patch.object(wu.os, "scandir", scandir):
            r = wu.browse("G:\\V\\Projects", with_files=False)
        self.assertEqual(len(r["subfolders"]), 299)
        self.assertEqual((len(calls), len(states)), (1, 1))            # not 300 separate questions

    def test_tree_follows_the_index(self):
        before = wu._build_tree()
        self.assertIs(wu._build_tree(), before)
        self.assertNotIn("g:\\w", before["folders"])
        time.sleep(0.01)
        self.ref("G:\\W\\Other.SLDASM", "Default", "G:\\V\\Parts\\Bolt.SLDPRT")
        self.assertIn("g:\\w", wu._build_tree()["folders"])

    def test_fast_folder_of_matches_pathlib(self):
        import random
        random.seed(3)
        cases = ["D:\\a\\b.SLDPRT", "D:\\b.SLDPRT", "D:\\", "D:\\a\\", "D:\\a", "d:\\x\\y\\z\\q.sldasm",
                 "\\\\srv\\share\\a\\b.SLDPRT", "\\\\srv\\share\\b.SLDPRT", "\\\\srv\\share\\", "\\\\srv\\share",
                 "\\\\srv\\share\\a\\", "\\\\srv.company.local\\CAD Data\\Proj 1\\Part (2).SLDPRT",
                 "D:/mixed/slashes.SLDPRT", "D:\\double\\\\slash.SLDPRT", "relative\\path.SLDPRT", "",
                 "C:\\Users\\a\\AppData\\Local\\Temp\\swx1\\x.SLDPRT", "\\\\?\\D:\\long\\path.SLDPRT", "D:", "D:x.SLDPRT"]
        pieces = ["Vault", "Parts", "Proj 12", "A.B", "x", "Sub ass'y", "ÄÖÜ", "  sp ", "end."]
        for _ in range(3000):
            depth = random.randint(0, 5)
            body = "\\".join(random.choice(pieces) for _ in range(depth))
            root = random.choice(["D:\\", "z:\\", "\\\\srv\\share\\", "\\\\s1\\c$\\"])
            cases.append(root + body + random.choice(["", "\\", "\\f.SLDPRT"]))
        for c in cases:
            self.assertEqual(wu._folder_of(c), wu._folder_of_slow(c), repr(c))

    def test_tree_is_not_rebuilt_on_every_click_while_indexing(self):
        wu._build_tree()
        built = wu._tree["built"]
        self.ref("G:\\V\\Extra.SLDASM", "Default", "G:\\V\\Parts\\Bolt.SLDPRT")   # the index changes...
        with mock.patch.object(wu, "_rebuild_tree_bg", lambda: None):            # (no background thread here)
            tree = wu._build_tree(allow_stale=True)
        self.assertEqual(tree["built"], built)                                    # ...but the click does not wait
        self.assertEqual(wu._build_tree()["folders"]["g:\\v"]["asm"], 3)          # a normal call is up to date

    def test_unknown_folder(self):
        with mock.patch.object(wu.os, "scandir", lambda p: FakeScandir([])):
            r = wu.browse("G:\\Nowhere")
        self.assertFalse(r["known"])
        self.assertEqual(r["subfolders"], [])


class NotOnThisPcTests(Base):
    """Old locations named in saved references (a drive letter this PC does not have, a server that is gone) are
    not shown in Browse unless asked for; a known drive that is offline stays (it comes back)."""

    def test_states(self):
        self.patch("_drive_letters", lambda: {"C", "D", "G"})
        self.patch("_root_reachable_cached", lambda root, *a, **k: root.upper().startswith(("C:", "D:")))
        wu._isdir_cache.clear()
        self.assertEqual(wu._folder_state("E:\\\\Old\\\\Parts"), "missing")              # no E: on this PC
        self.assertEqual(wu._folder_state("G:\\\\Vault"), "unreachable")                 # mapped, but offline

    def test_roots_hidden_unless_asked(self):
        roots = {"c:\\\\": "C:\\\\", "e:\\\\": "E:\\\\", "\\\\\\\\oldserver\\\\cad": "\\\\\\\\oldserver\\\\cad", "g:\\\\": "G:\\\\"}
        tree = {"roots": list(roots), "folders": {k: {"path": v, "asm": 1, "drw": 0, "prt": 0, "refs": 0, "children": []}
                                                  for k, v in roots.items()}}
        states = {"C:\\\\": "ok", "E:\\\\": "missing", "\\\\\\\\oldserver\\\\cad": "unreachable", "G:\\\\": "unreachable"}
        self.patch("_build_tree", lambda allow_stale=False: tree)
        self.patch("_folder_state", lambda p, ttl=300: states[p])
        shown = wu.browse("")
        self.assertEqual([e["path"] for e in shown["subfolders"]], ["C:\\\\", "G:\\\\"])
        self.assertEqual(shown["hidden"], 2)
        self.assertEqual(len(wu.browse("", show_all=True)["subfolders"]), 4)
