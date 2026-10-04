"""
Tests for renaming and moving. The Document Manager is simulated: a "SOLIDWORKS file" in these tests is a
JSON list of the paths it references. Everything happens in a temporary folder.
"""
import json
import os
import sys
import threading
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import SWhereUsed as wu  # noqa: E402
from test_whereused import Base  # noqa: E402


class FakeProps:
    """Custom properties of a document or a configuration, as the Document Manager offers them."""

    def __init__(self, store):
        self.store = store

    def GetCustomPropertyNames(self):
        return tuple(self.store)

    def GetCustomProperty(self, name):
        return (self.store[name], 30) if name in self.store else ("", 0)

    def SetCustomProperty(self, name, value):
        if name in self.store:
            self.store[name] = value
            return True
        return False

    def AddCustomProperty(self, name, kind, value):
        self.store[name] = value
        return True

    def DeleteCustomProperty(self, name):
        return self.store.pop(name, None) is not None


class FakeDoc:
    fail_save_on = None                               # file name whose Save fails
    saves = []                                        # file names saved, in order

    def __init__(self, path):
        self.path = path
        try:
            self.refs = json.loads(Path(path).read_text(encoding="utf-8"))
        except ValueError:
            self.refs = None                              # a part: no references in this fake
        side = Path(path + ".props.json")                 # custom properties: file and per configuration
        self.props = json.loads(side.read_text(encoding="utf-8")) if side.exists() else {"file": {}, "configs": {"Default": {}}}
        self._props_at_start = json.dumps(self.props)
        self._file = FakeProps(self.props["file"])

    def GetCustomPropertyNames(self):
        return self._file.GetCustomPropertyNames()

    def GetCustomProperty(self, name):
        return self._file.GetCustomProperty(name)

    def SetCustomProperty(self, name, value):
        return self._file.SetCustomProperty(name, value)

    def AddCustomProperty(self, name, kind, value):
        return self._file.AddCustomProperty(name, kind, value)

    def DeleteCustomProperty(self, name):
        return self._file.DeleteCustomProperty(name)

    @property
    def ConfigurationManager(self):
        props = self.props["configs"]
        return type("Mgr", (), {"GetConfigurationNames": lambda _s: tuple(props),
                                "GetConfigurationByName": lambda _s, n: FakeProps(props[n])})()

    def GetAllExternalReferences4(self, _opt):
        return (tuple(self.refs), (), ())

    def ReplaceReference(self, old, new):
        self.refs = [new if r == old else r for r in self.refs]

    def Save(self):
        if FakeDoc.fail_save_on and Path(self.path).name == FakeDoc.fail_save_on:
            return 3
        if self.refs is not None:
            Path(self.path).write_text(json.dumps(self.refs), encoding="utf-8")
        if json.dumps(self.props) != self._props_at_start:   # only when the properties changed
            Path(self.path + ".props.json").write_text(json.dumps(self.props), encoding="utf-8")
        FakeDoc.saves.append(Path(self.path).name)
        return 0

    def CloseDoc(self):
        pass


def fake_read(path):
    refs = json.loads(Path(path).read_text(encoding="utf-8"))
    return [(path.lower(), "", wu._fname(r).lower(), r, "", 1, 0, 0) for r in refs], 1, None


class RenameBase(Base):
    def setUp(self):
        super().setUp()
        FakeDoc.fail_save_on = None
        FakeDoc.saves = []
        v = self.dir / "vault"
        (v / "new").mkdir(parents=True)
        self.part = str(v / "Bolt.SLDPRT")
        self.drw = str(v / "Bolt.SLDDRW")
        self.sub = str(v / "Sub.SLDASM")
        self.top = str(v / "Top.SLDASM")
        Path(self.part).write_text("part", encoding="utf-8")
        for f, refs in ((self.drw, [self.part]), (self.sub, [self.part, str(v / "Nut.SLDPRT")]), (self.top, [self.sub])):
            Path(f).write_text(json.dumps(refs), encoding="utf-8")
        self.ref(self.sub, "Default", self.part, qty=2)
        self.ref(self.drw, "", self.part)
        self.ref(self.top, "Default", self.sub)
        self.patch("sw_problem", lambda: None)
        self.patch("RENAME_ALLOWED", "yes")
        self.patch("_open_sw", lambda path, readonly=True: FakeDoc(path))
        self.patch("_sw_app", lambda: mock.Mock(GetSearchOptionObject=lambda: None))
        self.patch("read_document", fake_read)
        self.patch("_root_reachable_cached", lambda *a, **k: True)
        self.patch("update_index", lambda *a, **k: True)
        # The worker process is run in this process instead
        def popen(args, **kw):                                     # the job runs right here, at once
            wu._run_rename_job(args[-1])
            return mock.Mock(wait=lambda timeout=None: 0, pid=0)
        self.patch("subprocess", mock.Mock(Popen=popen, TimeoutExpired=TimeoutError))

    def refs(self, f):
        return json.loads(Path(f).read_text(encoding="utf-8"))


class RenameTests(RenameBase):

    def test_plan_changes_nothing(self):
        plan = wu.rename_plan(self.part, "Bolt-M8", None, True)
        self.assertEqual(plan["blockers"], [])
        self.assertEqual(plan["counts"], {"assemblies": 1, "drawings": 1})
        self.assertEqual(plan["drawing"]["new"], str(Path(self.drw).with_name("Bolt-M8.SLDDRW")))
        self.assertTrue(Path(self.part).exists())

    def test_rename_with_drawing(self):
        res = wu.rename_execute(self.part, "Bolt-M8", None, True)
        self.assertTrue(res["executed"])
        self.assertTrue(res["result"]["ok"], res["result"])
        new_part = str(Path(self.part).with_name("Bolt-M8.SLDPRT"))
        new_drw = str(Path(self.drw).with_name("Bolt-M8.SLDDRW"))
        self.assertTrue(Path(new_part).exists() and Path(new_drw).exists())
        self.assertFalse(Path(self.part).exists() or Path(self.drw).exists())
        self.assertEqual(self.refs(self.sub)[0], new_part)
        self.assertTrue(self.refs(self.sub)[1].endswith("Nut.SLDPRT"))      # other references untouched
        self.assertEqual(self.refs(new_drw), [new_part])
        self.assertEqual(list(Path(self.part).parent.glob("~$*")), [])       # lock files gone
        backups = list(Path(res["result"]["backup_dir"]).iterdir())
        self.assertEqual(len(backups), 4)                                   # part, drawing, sub + what_happened.txt
        # The index knows the new name right away
        d = wu.whereused(new_part)
        self.assertEqual(sorted(wu._fname(x["path"]) for x in d["direct"]), ["Bolt-M8.SLDDRW", "Sub.SLDASM"])
        self.assertEqual(d["top"][0]["qty"], 2)
        self.assertEqual(wu.whereused(self.part)["direct"], [])
        log = (self.dir / "data" / "rename_log.csv").read_text(encoding="utf-8-sig")
        self.assertIn("ok, 2 file(s) updated", log)

    def test_move_to_other_folder_without_drawing(self):
        folder = str(Path(self.part).parent / "new")
        res = wu.rename_execute(self.part, "Bolt", folder, False)
        self.assertTrue(res["result"]["ok"], res["result"])
        moved = str(Path(folder) / "Bolt.SLDPRT")
        self.assertEqual(self.refs(self.sub)[0], moved)
        self.assertTrue(Path(self.drw).exists())                            # drawing stayed ...
        self.assertEqual(self.refs(self.drw), [moved])                      # ... but points to the new place

    def test_moving_an_assembly_keeps_its_own_references_in_the_index(self):
        res = wu.rename_execute(self.sub, "Sub-2", None, True)
        self.assertTrue(res["result"]["ok"], res["result"])
        new_sub = str(Path(self.sub).with_name("Sub-2.SLDASM"))
        self.assertEqual(self.refs(self.top), [new_sub])
        d = wu.whereused(self.part)
        self.assertIn(new_sub, [x["path"] for x in d["direct"]])
        self.assertEqual(d["top"][0]["path"], self.top)

    def test_failure_puts_everything_back(self):
        before = {f: Path(f).read_text(encoding="utf-8") for f in (self.part, self.drw, self.sub)}
        FakeDoc.fail_save_on = "Sub.SLDASM"
        res = wu.rename_execute(self.part, "Bolt-M8", None, True)
        r = res["result"]
        self.assertFalse(r["ok"])
        self.assertTrue(r["rolled_back"], r)
        self.assertIn("saving failed", r["error"])
        for f, text in before.items():
            self.assertEqual(Path(f).read_text(encoding="utf-8"), text, f)
        self.assertEqual(sorted(p.name for p in Path(self.part).parent.iterdir() if p.is_file()),
                         ["Bolt.SLDDRW", "Bolt.SLDPRT", "Sub.SLDASM", "Top.SLDASM"])
        self.assertEqual(len(wu.whereused(self.part)["direct"]), 2)          # index unchanged

    def test_failure_after_drawing_was_saved_restores_it_in_its_old_place(self):
        before = Path(self.drw).read_text(encoding="utf-8")
        FakeDoc.fail_save_on = "Sub.SLDASM"                                  # the drawing is saved before Sub? make sure
        plan = wu.rename_plan(self.part, "Bolt-M8", None, True)
        order = [wu._fname(p["path"]) for p in plan["parents"]]
        self.assertEqual(order[0], "Bolt.SLDDRW")                            # drawing first: saved, then Sub fails
        wu.rename_execute(self.part, "Bolt-M8", None, True)
        self.assertEqual(Path(self.drw).read_text(encoding="utf-8"), before)
        self.assertFalse(Path(self.drw).with_name("Bolt-M8.SLDDRW").exists())

    def test_blockers(self):
        self.assertIn("enter a new name", wu.rename_plan(self.part, "", None, True)["blockers"])
        self.assertTrue(any("cannot contain" in b for b in wu.rename_plan(self.part, "a/b", None, True)["blockers"]))
        self.assertTrue(any("must end with .SLDPRT" in b for b in wu.rename_plan(self.part, "Sub.SLDASM", None, True)["blockers"]))
        Path(self.part).with_name("Taken.SLDPRT").write_text("x")
        self.assertTrue(any("already is a file" in b for b in wu.rename_plan(self.part, "Taken", None, True)["blockers"]))
        self.assertTrue(any("does not exist" in b for b in wu.rename_plan(self.part, "X", str(self.dir / "nope"), True)["blockers"]))
        with mock.patch.object(wu, "RENAME_ALLOWED", "no"):
            self.assertTrue(any("switched off" in b for b in wu.rename_plan(self.part, "X", None, True)["blockers"]))
            self.assertFalse(wu.rename_execute(self.part, "X", None, True)["executed"])
        self.assertTrue(Path(self.part).exists())

    def test_leftover_lock_blocks_and_can_be_removed(self):
        lock = Path(self.sub).with_name("~$Sub.SLDASM")
        lock.write_bytes("jan\x00PC-12\x00".encode("utf-16-le"))
        plan = wu.rename_plan(self.part, "Bolt-M8", None, True)
        self.assertTrue(any(b.startswith("Sub.SLDASM: leftover lock file from jan / PC-12 (0 min old")
                            for b in plan["blockers"]), plan["blockers"])
        wu.remove_stale_lock(self.sub)
        self.assertFalse(lock.exists())
        self.assertEqual(wu.rename_plan(self.part, "Bolt-M8", None, True)["blockers"], [])

    def test_lock_is_kept_when_anything_is_still_open(self):
        lock = Path(self.sub).with_name("~$Sub.SLDASM")
        lock.write_bytes("jan\x00PC-12\x00".encode("utf-16-le"))
        old = time.time() - 3 * 86400
        os.utime(lock, (old, old))
        self.assertIn("(3 days old)", wu.blocking_reason(wu.access_info(self.sub)))
        for busy, msg in ((self.sub, "still open somewhere"), (str(lock), "holds the lock file")):
            with mock.patch.object(wu, "_file_in_use", lambda p, busy=busy: True if str(p) == busy else False):
                with self.assertRaisesRegex(RuntimeError, msg):
                    wu.remove_stale_lock(self.sub)
            self.assertTrue(lock.exists())
        with mock.patch.object(wu, "_file_in_use", lambda p: None):          # cannot tell: keep it
            with self.assertRaises(RuntimeError):
                wu.remove_stale_lock(self.sub)
        self.assertTrue(lock.exists())
        wu.remove_stale_lock(self.sub)
        self.assertFalse(lock.exists())
        self.assertIn("(3 days old)", (self.dir / "data" / "rename_log.csv").read_text(encoding="utf-8-sig"))

    def test_own_leftover_lock_is_cleaned_up(self):
        lock = Path(self.sub).with_name("~$Sub.SLDASM")
        lock.write_bytes(f"jan\x00PC-12\x00{wu.APP_LOCK_MARKER}\x00".encode("utf-16-le"))
        self.assertIsNone(wu.blocking_reason(wu.access_info(self.sub)))
        self.assertFalse(lock.exists())

    def test_case_only_rename(self):
        res = wu.rename_execute(self.part, "BOLT", None, False)
        self.assertTrue(res["result"]["ok"], res["result"])
        self.assertIn("BOLT.SLDPRT", [p.name for p in Path(self.part).parent.iterdir()])
        self.assertEqual(wu._fname(self.refs(self.sub)[0]), "BOLT.SLDPRT")

    def test_old_backups_are_removed(self):
        root = self.dir / "data" / "backups"
        (root / "20200101" / "101010").mkdir(parents=True)
        (root / time.strftime("%Y%m%d")).mkdir(parents=True)
        (root / "keep-me").mkdir()
        self.assertEqual(wu.cleanup_backups(), 1)
        self.assertEqual(sorted(p.name for p in root.iterdir()), sorted([time.strftime("%Y%m%d"), "keep-me"]))


class BatchRenameTests(RenameBase):
    """Several files at once. Same fake vault: Top -> Sub -> Bolt (+ Nut), Bolt.SLDDRW -> Bolt."""

    def setUp(self):
        super().setUp()
        self.nut = str(Path(self.part).with_name("Nut.SLDPRT"))
        Path(self.nut).write_text("part", encoding="utf-8")
        self.ref(self.sub, "Default", self.nut)
        wu.conn().execute("DELETE FROM refs WHERE child_stored_path LIKE ?", ("%Nut.SLDPRT",))
        self.ref(self.sub, "Default", self.nut, qty=6)

    def test_counts_before_doing_anything(self):
        r = wu.rename_batch([{"path": self.part, "name": "Bolt-M8"}, {"path": self.nut, "name": "Nut-M8"},
                             {"path": self.sub, "name": "Sub"}], True, True)          # Sub unchanged: skipped
        self.assertFalse(r["executed"])
        self.assertEqual(r["blockers"], [])
        c = r["counts"]
        self.assertEqual((c["renamed"], c["drawings"]), (2, 1))                         # Bolt (+ its drawing), Nut
        self.assertEqual(sorted(wu._fname(u) for u in r["updated"]), ["Bolt.SLDDRW", "Sub.SLDASM"])
        self.assertEqual(c["updated_not_renamed"], 1)                                   # Sub; the drawing moves too
        self.assertEqual(c["total_changed"], 4)                                         # Bolt, drawing, Nut, Sub
        self.assertTrue(Path(self.part).exists())

    def test_rename_several_files_in_one_go(self):
        r = wu.rename_batch([{"path": self.part, "name": "Bolt-M8"}, {"path": self.nut, "name": "Nut-M8"},
                             {"path": self.sub, "name": "Sub-2"}], True, False)
        self.assertTrue(r["executed"])
        self.assertTrue(r["result"]["ok"], r["result"])
        v = Path(self.part).parent
        new_sub = str(v / "Sub-2.SLDASM")
        self.assertEqual(sorted(self.refs(new_sub)), sorted([str(v / "Bolt-M8.SLDPRT"), str(v / "Nut-M8.SLDPRT")]))
        self.assertEqual(self.refs(self.top), [new_sub])
        self.assertEqual(self.refs(str(v / "Bolt-M8.SLDDRW")), [str(v / "Bolt-M8.SLDPRT")])
        self.assertEqual(FakeDoc.saves.count("Sub-2.SLDASM"), 1)                        # opened and saved once
        self.assertEqual(sorted(p.name for p in v.iterdir() if p.is_file()),
                         ["Bolt-M8.SLDDRW", "Bolt-M8.SLDPRT", "Nut-M8.SLDPRT", "Sub-2.SLDASM", "Top.SLDASM"])
        # The index follows: where used of the new names
        self.assertEqual([wu._fname(x["path"]) for x in wu.whereused(str(v / "Nut-M8.SLDPRT"))["direct"]], ["Sub-2.SLDASM"])
        tops = wu.whereused(str(v / "Bolt-M8.SLDPRT"))["top"]
        self.assertEqual([(wu._fname(t["path"]), t["qty"]) for t in tops], [("Top.SLDASM", 2)])
        log = (self.dir / "data" / "rename_log.csv").read_text(encoding="utf-8-sig")
        self.assertEqual(log.count("rename (batch of 3)"), 3)

    def test_batch_failure_puts_everything_back(self):
        before = {f.name: f.read_text(encoding="utf-8") for f in Path(self.part).parent.iterdir() if f.is_file()}
        FakeDoc.fail_save_on = "Top.SLDASM"                                             # the last file fails
        r = wu.rename_batch([{"path": self.part, "name": "Bolt-M8"}, {"path": self.sub, "name": "Sub-2"}], True, False)
        self.assertFalse(r["result"]["ok"])
        self.assertTrue(r["result"]["rolled_back"], r["result"])
        after = {f.name: f.read_text(encoding="utf-8") for f in Path(self.part).parent.iterdir() if f.is_file()}
        self.assertEqual(after, before)

    def test_batch_conflicts(self):
        r = wu.rename_batch([{"path": self.part, "name": "Same"}, {"path": self.nut, "name": "Same"}], True, False)
        self.assertFalse(r["executed"])
        self.assertTrue(any("new name of two files" in b for b in r["blockers"]), r["blockers"])
        r = wu.rename_batch([{"path": self.part, "name": "Nut"}, {"path": self.nut, "name": "Bolt"}], True, False)
        self.assertTrue(any("current name of another file" in b or "already is a file" in b for b in r["blockers"]))
        self.assertTrue(Path(self.part).exists() and Path(self.nut).exists())
        self.assertEqual(wu.rename_batch([], True, True)["blockers"], ["no new names given"])


class ReadinessTests(RenameBase):
    """Before any name is typed: can each file be renamed, with its drawing and the files that refer to it?"""

    def setUp(self):
        super().setUp()
        wu._fstatus_cache.clear()

    def test_all_free(self):
        r = wu.rename_readiness([self.part, self.sub], True)
        part, sub = r["files"][self.part.lower()], r["files"][self.sub.lower()]
        self.assertTrue(part["ok"], part)
        self.assertEqual((part["updates"], part["drawing"]), (2, self.drw[:-7] + ".SLDDRW"))   # Sub + drawing
        self.assertEqual((sub["updates"], sub["drawing"]), (1, None))                           # Top; no drawing
        self.assertIsNone(r["problem"])

    def test_open_referencing_file_is_shown_before_typing(self):
        lock = Path(self.sub).with_name("~$Sub.SLDASM")
        lock.write_bytes("jan\x00PC-12\x00".encode("utf-16-le"))
        with mock.patch.object(wu, "_file_in_use", lambda p: str(p) == self.sub or None):
            r = wu.rename_readiness([self.part, self.sub], True)
        part = r["files"][self.part.lower()]
        self.assertFalse(part["ok"])
        self.assertEqual([(wu._fname(b["path"]), b["reason"]) for b in part["blocked"]],
                         [("Sub.SLDASM", "open in SOLIDWORKS by jan / PC-12")])
        self.assertEqual(r["files"][self.sub.lower()]["reason"], "open in SOLIDWORKS by jan / PC-12")

    def test_drawing_along_and_leftover_locks(self):
        lock = Path(self.drw).with_name("~$Bolt.SLDDRW")
        lock.write_bytes("piet\x00PC-3\x00".encode("utf-16-le"))
        r = wu.rename_readiness([self.part], True)["files"][self.part.lower()]
        self.assertFalse(r["ok"])
        self.assertTrue(r["drawing_reason"].startswith("leftover lock file from piet / PC-3"))
        self.assertEqual(r["blocked"], [])                     # the drawing is listed once, as the drawing
        r = wu.rename_readiness([self.part], False)["files"][self.part.lower()]
        self.assertIsNone(r["drawing"])                        # not renamed along: only its reference matters
        self.assertFalse(r["ok"])
        lock.write_bytes(f"me\x00PC\x00{wu.APP_LOCK_MARKER}\x00".encode("utf-16-le"))  # our own leftover lock
        wu._fstatus_cache.clear()
        self.assertTrue(wu.rename_readiness([self.part], True)["files"][self.part.lower()]["ok"])

    def test_each_folder_is_listed_once(self):
        calls = []
        real = wu._folder_status
        with mock.patch.object(wu, "_folder_status", lambda f: calls.append(f) or real(f)):
            wu.rename_readiness([self.part, self.sub, self.top], True)
        self.assertEqual(len(calls), len(set(calls)))
        self.assertEqual(len(calls), 1)                        # everything is in one folder here

    def test_switched_off(self):
        with mock.patch.object(wu, "RENAME_ALLOWED", "no"):
            self.assertIn("switched off", wu.rename_readiness([self.part])["problem"])


class ComponentListStaysOldTests(RenameBase):
    """As confirmed on a real Document Manager: ReplaceReference changes the EXTERNAL references (what SOLIDWORKS
    loads); the component list keeps the old name until SOLIDWORKS itself saves the file. Simulated here: the
    external references are the file, the component list is a side file that ReplaceReference does not touch."""

    def setUp(self):
        super().setUp()
        for f in (self.sub, self.drw, self.top):
            Path(f + ".components").write_text(Path(f).read_text(encoding="utf-8"), encoding="utf-8")

        def read_components(path):
            side = Path(path + ".components")
            refs = json.loads((side if side.exists() else Path(path)).read_text(encoding="utf-8"))
            return [(path.lower(), "Default", wu._fname(r).lower(), r, "", 1, 0, 0) for r in refs], 1, None, 17000
        self.patch("read_document", read_components)
        self.new_part = str(Path(self.part).with_name("Bolt-M8.SLDPRT"))

    def test_rename_passes_the_check(self):
        r = wu.rename_execute(self.part, "Bolt-M8", None, False)
        self.assertTrue(r["result"]["ok"], r["result"])                       # was: "check failed: ... does not point to"
        self.assertEqual(self.refs(self.sub)[0], self.new_part)                  # external references: new
        self.assertEqual(json.loads(Path(self.sub + ".components").read_text())[0], self.part)   # components: still old
        log = (self.dir / "data" / "rename_debug.log").read_text(encoding="utf-8")
        self.assertIn("ReplaceReference(", log)
        self.assertIn("Save returned", log)

    def test_index_finds_the_new_file_after_reading_again(self):
        wu.rename_execute(self.part, "Bolt-M8", None, False)
        # The next index run reads Sub again (it changed); its component list still says Bolt.SLDPRT
        files = {self.sub: (2, 2), self.top: (1, 1), self.drw: (1, 1)}
        self.patch("sw_problem", lambda: None)
        self.patch("index_source", lambda: ("everything", "test"))
        self.patch("_reachable", lambda root, timeout=10: True)
        self.patch("_com_init", lambda: None)
        self.patch("_probe_part_references", lambda parts: None)
        self.patch("everything_list", lambda q: [dict(type="file", path=p, modified=m, size=sz) for p, (m, sz) in files.items()])
        wu.run_index()
        self.assertIn(self.sub, [x["path"] for x in wu.whereused(self.new_part)["direct"]])
        self.assertEqual(wu.whereused(self.part)["direct"], [])
        st = wu.structure(self.top)
        bolt = next(n for n in st["tree"][0]["children"] if n["name"].startswith("Bolt"))
        self.assertEqual((bolt["name"], bolt["how"]), ("Bolt-M8.SLDPRT", "stored"))   # not "missing"

    def test_structure_shows_the_new_name_marked_renamed(self):
        """The assembly still names the old file; the file has the new name. The structure shows the new name,
        the mark 'renamed' (old name on hover), and nothing of the old name next to it."""
        wu.rename_execute(self.part, "Bolt-M8", None, False)
        files = {self.sub: (2, 2), self.top: (1, 1), self.drw: (1, 1)}
        self.patch("sw_problem", lambda: None)
        self.patch("index_source", lambda: ("everything", "test"))
        self.patch("_reachable", lambda root, timeout=10: True)
        self.patch("_com_init", lambda: None)
        self.patch("_probe_part_references", lambda parts: None)
        self.patch("everything_list", lambda q: [dict(type="file", path=p, modified=m, size=sz) for p, (m, sz) in files.items()])
        wu.run_index()
        sub = wu.structure(self.top)["tree"][0]
        bolt = next(n for n in sub["children"] if n["name"].lower().startswith("bolt"))
        self.assertEqual((bolt["name"], bolt["kind"]), ("Bolt-M8.SLDPRT", "part"))
        self.assertEqual(bolt["renamed_from"], self.part)
        self.assertEqual([n["name"] for n in sub["children"]].count("Bolt.SLDPRT"), 0)
        compact = wu.compact_structure(wu.structure(self.top))
        self.assertEqual(compact["tree"][0]["k"][0]["rf"], self.part)
        # SOLIDWORKS saves Sub later: its component list is right again, so no more mark
        db = wu.conn()
        db.execute("UPDATE files SET mtime=?, indexed_at=? WHERE path=?", (time.time() + 100, time.time() + 100, self.sub.lower()))
        db.commit()
        sub = wu.structure(self.top)["tree"][0]
        bolt = next(n for n in sub["children"] if n["name"] == "Bolt-M8.SLDPRT")
        self.assertNotIn("renamed_from", bolt)

    def test_a_new_file_with_the_old_name_is_left_alone(self):
        wu.rename_execute(self.part, "Bolt-M8", None, False)
        Path(self.part).write_text("a new, different part", encoding="utf-8")
        wu._renamed_cache["t"] = 0.0
        self.assertEqual(wu.apply_rename(self.part), self.part)

    def test_renamed_twice(self):
        wu.rename_execute(self.part, "Bolt-M8", None, False)
        wu.rename_execute(self.new_part, "Bolt-M8x20", None, False)
        wu._renamed_cache["t"] = 0.0
        self.assertEqual(wu.apply_rename(self.part), str(Path(self.part).with_name("Bolt-M8x20.SLDPRT")))

    def test_reading_the_map_inside_the_lock_would_hang(self):
        # apply_rename reads the database itself; the index calls it BEFORE taking the lock. Make sure that holds:
        # with the lock taken elsewhere for a moment, storing must not deadlock.
        wu.rename_execute(self.part, "Bolt-M8", None, False)
        wu._renamed_cache["t"] = 0.0
        done = []
        t = threading.Thread(target=lambda: done.append(wu._apply_renames_to_rows(
            [(self.sub.lower(), "Default", "bolt.sldprt", self.part, "", 1, 0, 0)])))
        t.start()
        t.join(5)
        self.assertTrue(done and done[0][0][3] == self.new_part)


class OwnFoldersExcludedTests(RenameBase):
    def test_data_folder_and_backups_are_never_indexed(self):
        self.patch("BACKUP_FOLDER", str(self.dir / "elsewhere" / "bk"))
        for p in (str(self.dir / "data" / "backups" / "20260930" / "101010" / "000_Bolt.SLDPRT"),
                  str(self.dir / "elsewhere" / "bk" / "20260930" / "x" / "001_Sub.SLDASM"),
                  str(self.dir / "data" / "jobs" / "A.SLDASM")):
            self.assertTrue(wu._excluded(p), p)
        self.assertFalse(wu._excluded(self.part))
        own = wu._own_folders()[0]
        self.assertIn(f'!"{own}"', wu._everything_query())
        self.assertIn(f'!"{own}"', wu._ev_search_query("bolt"))


class ProbeExitCodeTests(RenameBase):
    def test_ordinary_error_is_not_remembered(self):
        from types import SimpleNamespace
        self.patch("find_swdm_dll", lambda: __file__)
        with mock.patch.dict(wu.PART_REFS, {"enabled": True, "why": None}):
            with mock.patch.object(wu.subprocess, "run", lambda *a, **k: SimpleNamespace(
                    returncode=1, stderr="Traceback ...\nRuntimeError: no Document Manager license key\n")):
                wu._probe_part_references(["G:\\V\\A.SLDPRT"])
            self.assertFalse(wu.PART_REFS["enabled"])
            self.assertIn("no Document Manager license key", wu.PART_REFS["why"])
            self.assertIsNone(wu._meta_get("part_refs_probe"))                     # not remembered
            with mock.patch.object(wu.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stderr="")):
                wu._probe_part_references(["G:\\V\\A.SLDPRT"])               # solved: next run tries again
            self.assertTrue(wu.PART_REFS["enabled"])


class EnvelopeTests(RenameBase):
    def test_flags_from_the_component(self):
        from types import SimpleNamespace as NS
        self.patch("_best", lambda obj, base: obj)
        comps = [NS(PathName="G:\\V\\Bolt.SLDPRT", ConfigurationName="", IsSuppressed=False, IsVirtual=False,
                    IsEnvelope=lambda: True),
                 NS(PathName="G:\\V\\Nut.SLDPRT", ConfigurationName="", IsSuppressed=True, IsVirtual=False,
                    IsEnvelope=lambda: False, ExcludeFromBOM=True),
                 NS(PathName="G:\\V\\Pin.SLDPRT", ConfigurationName="", IsSuppressed=False, IsVirtual=False)]
        out, how, _e = wu._read_components(NS(GetComponents=lambda: comps))
        self.assertEqual([wu._flags(c["suppressed"]) for c in out], [
            {"suppressed": False, "envelope": True, "not_in_bom": False},
            {"suppressed": True, "envelope": False, "not_in_bom": True},
            {"suppressed": False, "envelope": False, "not_in_bom": False}])

    def test_used_but_not_counted(self):
        self.ref(self.top, "Default", self.part, qty=3, supp=wu.FLAG_ENVELOPE)    # an envelope in Top
        self.patch("_existing_files", lambda paths: {p.lower() for p in paths})
        d = wu.whereused(self.part)
        top = next(x for x in d["direct"] if x["path"] == self.top)
        self.assertTrue(top["configs"][0]["envelope"])                             # still listed: it IS used
        self.assertEqual([(wu._fname(t["path"]), t["qty"]) for t in d["top"]], [("Top.SLDASM", 2)])  # only via Sub
        flat = {f["name"]: f for f in wu.structure(self.top)["flat"]}
        self.assertEqual(flat["Bolt.SLDPRT"]["qty"], 2)                            # 1 Sub x 2, not + 3 envelopes
