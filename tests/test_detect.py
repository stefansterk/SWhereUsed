"""Renames done by ANOTHER tool (SOLIDWORKS Explorer, PDM, a Document Manager tool): the component list of an
assembly still names the old file, its external references the new one. SWhereUsed finds out and follows."""
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import SWhereUsed as wu  # noqa: E402
from test_whereused import Base  # noqa: E402

V = "G:\\V\\"


class PairingTests(Base):
    def test_one_gone_one_new(self):
        self.assertEqual(wu._pair_renames([V + "Old.SLDPRT"], [V + "Parts\\New.SLDPRT"]), [(V + "Old.SLDPRT", V + "Parts\\New.SLDPRT")])

    def test_several_paired_per_folder_only_when_certain(self):
        gone = [V + "A\\Old1.SLDPRT", V + "B\\Old2.SLDPRT", V + "C\\Old3.SLDPRT", V + "C\\Old4.SLDPRT"]
        new = [V + "A\\New1.SLDPRT", V + "B\\New2.SLDPRT", V + "C\\New3.SLDPRT", V + "C\\New4.SLDPRT"]
        pairs = dict(wu._pair_renames(gone, new))
        self.assertEqual(pairs, {V + "A\\Old1.SLDPRT": V + "A\\New1.SLDPRT", V + "B\\Old2.SLDPRT": V + "B\\New2.SLDPRT"})  # C: two by two, not guessed

    def test_type_must_match(self):
        self.assertEqual(wu._pair_renames([V + "Old.SLDPRT"], [V + "New.SLDASM"]), [])


class DetectTests(Base):
    def setUp(self):
        super().setUp()
        self.patch("_existing_files", lambda paths: {p.lower() for p in paths if "Old" not in p})
        self.patch("_sw_app", lambda: SimpleNamespace(GetSearchOptionObject=lambda: None))
        self.rows = [("g:\\v\\top.sldasm", "Default", "bolt-old.sldprt", V + "Bolt-Old.SLDPRT", "", 4, 0, 0),
                     ("g:\\v\\top.sldasm", "Default", "nut.sldprt", V + "Nut.SLDPRT", "", 2, 0, 0)]
        self.asked = []
        self.patch("_ext_refs_isolated", lambda path, timeout=60: self.asked.append(path) or [V + "Bolt-M8.SLDPRT", V + "Nut.SLDPRT"])

    def test_found_in_the_external_references(self):
        self.assertEqual(wu._detect_renames(V + "Top.SLDASM", self.rows), [(V + "Bolt-Old.SLDPRT", V + "Bolt-M8.SLDPRT")])
        self.assertEqual(self.asked, [V + "Top.SLDASM"])               # read in the helper, not in the index process

    def test_external_references_only_read_when_something_is_missing(self):
        wu._detect_renames(V + "Top.SLDASM", [r for r in self.rows if "Nut" in r[3]])
        self.assertEqual(self.asked, [])

    def test_switched_off(self):
        with mock.patch.object(wu, "DETECT_RENAMES", "no"):
            self.assertEqual(wu._detect_renames(V + "Top.SLDASM", self.rows), [])
        self.assertEqual(self.asked, [])

    def test_helper_crash_only_loses_this_detection(self):
        self.patch("_ext_refs_isolated", lambda path, timeout=60: None)
        self.assertEqual(wu._detect_renames(V + "Top.SLDASM", self.rows), [])

    def test_the_index_follows_everywhere(self):
        drw = "G:\\V\\Bolt-Old.SLDDRW"
        self.ref(drw, "", V + "Bolt-Old.SLDPRT")                        # stored earlier: the drawing names the old file
        files = {V + "Top.SLDASM": (2, 2), V + "Bolt-M8.SLDPRT": (1, 1), V + "Nut.SLDPRT": (1, 1), drw: (3, 3)}
        self.patch("sw_problem", lambda: None)
        self.patch("index_source", lambda: ("everything", "test"))
        self.patch("_reachable", lambda root, timeout=10: True)
        self.patch("_com_init", lambda: None)
        self.patch("_probe_part_references", lambda parts: None)
        self.patch("everything_list", lambda q: [dict(type="file", path=p, modified=m, size=sz) for p, (m, sz) in files.items()])
        rows = self.rows

        def read(p):
            if p.endswith("Top.SLDASM"):
                return rows, 1, None, 17000, {}, [(V + "Bolt-Old.SLDPRT", V + "Bolt-M8.SLDPRT")]
            if p.endswith(".SLDDRW"):                                   # a drawing cannot tell; the assembly did
                return [(p.lower(), "", "bolt-old.sldprt", V + "Bolt-Old.SLDPRT", "", 1, 0, 0)], 1, None, 17000
            return [], 1, None, 17000, {}
        self.patch("read_document", read)
        self.patch("_root_reachable_cached", lambda *a, **k: True)
        wu.run_index()
        used = sorted(wu._fname(x["path"]) for x in wu.whereused(V + "Bolt-M8.SLDPRT")["direct"])
        self.assertEqual(used, ["Bolt-Old.SLDDRW", "Top.SLDASM"])        # not "not used"; the drawing too
        bolt = next(n for n in wu.structure(V + "Top.SLDASM")["tree"] if n["name"].startswith("Bolt"))
        self.assertEqual((bolt["name"], bolt["how"] != "missing", bolt.get("renamed_from")), ("Bolt-M8.SLDPRT", True, V + "Bolt-Old.SLDPRT"))
        self.assertEqual(wu.broken_references()["files"], 0)            # not a broken reference


class ReportedCaseTests(Base):
    """Mounting_plate_A in ASM-1000_LIGHTING, renamed by another app to 4411001M_Mounting_plate_A."""
    F = "G:\\Vault\\Purchased\\Lighting (traffic)\\"

    def test_several_renamed_in_one_folder_paired_by_name(self):
        gone = [self.F + "Mounting_plate_A.SLDPRT", self.F + "Bracket_A.SLDPRT"]
        new = [self.F + "4411001M_Mounting_plate_A.SLDPRT", self.F + "4411002M_Bracket_A.SLDPRT"]
        self.assertEqual(dict(wu._pair_renames(gone, new)), dict(zip(gone, new)))

    def test_not_guessed_when_two_new_names_both_fit(self):
        gone = [self.F + "Plaat.SLDPRT", self.F + "Strip.SLDPRT"]
        new = [self.F + "Plaat_A.SLDPRT", self.F + "Plaat_B.SLDPRT"]
        self.assertEqual(wu._pair_renames(gone, new), [])
        self.assertEqual(wu._pair_renames([self.F + "A1.SLDPRT", self.F + "B2.SLDPRT"],
                                          [self.F + "XA1.SLDPRT", self.F + "XB2.SLDPRT"]), [])   # too short to be sure

    def test_assembly_read_by_an_older_version_is_read_once_more(self):
        asm, old_part = self.F + "ASM-1000_LIGHTING.SLDASM", self.F + "Mounting_plate_A.SLDPRT"
        new_part = self.F + "4411001M_Mounting_plate_A.SLDPRT"
        files = {asm: (5, 5), new_part: (1, 1)}
        reads = []
        self.patch("sw_problem", lambda: None)
        self.patch("index_source", lambda: ("everything", "test"))
        self.patch("_reachable", lambda root, timeout=10: True)
        self.patch("_com_init", lambda: None)
        self.patch("_probe_part_references", lambda parts: None)
        self.patch("_root_reachable_cached", lambda *a, **k: True)
        self.patch("_existing_files", lambda paths: {p.lower() for p in paths if "\\Mounting_plate" not in p})
        self.patch("everything_list", lambda q: [dict(type="file", path=p, modified=m, size=sz) for p, (m, sz) in files.items()])
        row = (asm.lower(), "Default", "mounting_plate_a.sldprt", old_part, "", 2, 0, 0)

        def read(p):
            reads.append(wu._fname(p))
            if p == asm:
                return [row], 1, None, 17000, {}, [(old_part, new_part)]
            return [], 1, None, 17000, {}
        self.patch("read_document", read)
        wu.run_index()
        # As if a version without recognising had read the assembly: old rows, marked as read with version 2
        db = wu.conn()
        db.execute("DELETE FROM renamed")
        db.execute("UPDATE refs SET child_name=?, child_stored_path=? WHERE parent=?", ("mounting_plate_a.sldprt", old_part, asm.lower()))
        db.execute("UPDATE readver SET v=2 WHERE path=?", (asm.lower(),))
        db.commit()
        wu._renamed_cache["t"] = 0.0
        self.assertEqual(wu.whereused(new_part)["direct"], [])           # the reported problem
        reads.clear()
        wu.run_index()
        self.assertEqual(reads, ["ASM-1000_LIGHTING.SLDASM"])                # unchanged, but read once more
        self.assertEqual([wu._fname(x["path"]) for x in wu.whereused(new_part)["direct"]], ["ASM-1000_LIGHTING.SLDASM"])
        reads.clear()
        wu.run_index()
        self.assertEqual(reads, [])                                      # and not again


class HelperProcessTests(Base):
    """The real helper process, with a stand-in script: a crash in it must not reach the caller."""

    def run_helper(self, script):
        fake = self.dir / "helper.py"
        fake.write_text(script, encoding="utf-8")
        real_popen = wu.subprocess.Popen
        with mock.patch.object(wu.subprocess, "Popen", lambda args, **kw: real_popen([sys.executable, str(fake)], **kw)):
            return wu._ext_refs_isolated("G:\\V\\Top.SLDASM", timeout=10)

    def tearDown(self):
        if wu._ext["proc"] is not None:
            wu._ext["proc"].kill()
            wu._ext["proc"] = None
        super().tearDown()

    def test_answer(self):
        refs = self.run_helper("import sys, json\nfor line in sys.stdin:\n    print(json.dumps({'ok': True, 'refs': ['G:/V/A.SLDPRT']}), flush=True)\n")
        self.assertEqual(refs, ["G:/V/A.SLDPRT"])

    def test_crash_returns_none_and_restarts(self):
        start = time.time()
        refs = self.run_helper("import sys, os\nsys.stdin.readline()\nos._exit(3221226356 & 0xFF)\n")
        self.assertIsNone(refs)
        self.assertLess(time.time() - start, 5)                         # noticed right away, not after the time-out
        self.assertIsNone(wu._ext["proc"])


class CrashResetTests(Base):
    def test_files_marked_by_the_old_problem_are_tried_again_once(self):
        db = wu.conn()
        for name, st in (("A.SLDASM", "crash"), ("B.SLDASM", "suspect"), ("C.SLDASM", "ok")):
            p = V + name
            db.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?)", (p.lower(), p, 1, 1, time.time(), st, "x", 1, None))
            db.execute("INSERT INTO versions VALUES (?,?)", (p.lower(), 17000))
            db.execute("INSERT INTO readver VALUES (?,?)", (p.lower(), wu.ASM_READ_VERSION))
        db.commit()
        reads = []
        self.patch("sw_problem", lambda: None)
        self.patch("index_source", lambda: ("everything", "test"))
        self.patch("_reachable", lambda root, timeout=10: True)
        self.patch("_com_init", lambda: None)
        self.patch("_probe_part_references", lambda parts: None)
        self.patch("everything_list", lambda q: [dict(type="file", path=V + n, modified=1, size=1) for n in ("A.SLDASM", "B.SLDASM", "C.SLDASM")])
        self.patch("read_document", lambda p: reads.append(wu._fname(p)) or ([], 1, None, 17000, {}))
        wu.run_index()
        self.assertEqual(sorted(reads), ["A.SLDASM", "B.SLDASM"])
        db.execute("UPDATE files SET status='crash' WHERE path=?", ((V + "A.SLDASM").lower(),))   # a real crash later on
        db.commit()
        reads.clear()
        wu.run_index()
        self.assertEqual(reads, [])                                     # the reset happens only once


class LargeIndexTests(Base):
    def test_storing_recognised_renames_stays_quick_on_a_big_index(self):
        db = wu.conn()
        rows = [(f"g:\\v\\asm{i}.sldasm", "Default", f"part{i % 20000}.sldprt", f"G:\\V\\Parts\\Part{i % 20000}.SLDPRT", "", 1, 0, 0)
                for i in range(300000)]
        db.executemany("INSERT INTO refs VALUES (?,?,?,?,?,?,?,?)", rows)
        db.commit()
        pairs = [(f"G:\\V\\Parts\\Part{i}.SLDPRT", f"G:\\V\\Parts\\X_Part{i}.SLDPRT") for i in range(60)]
        start = time.time()
        wu._remember_detected("g:\\v\\asm7.sldasm", pairs)
        self.assertLess(time.time() - start, 1.0)
        # only that assembly: the others that name the same path are left as they are
        self.assertEqual(db.execute("SELECT COUNT(*) FROM refs WHERE child_name='x_part7.sldprt'").fetchone()[0], 0)

    def test_open_means_being_read(self):
        """Read but not yet stored is not "open": the watchdog must not blame such a file."""
        files = {f"G:\\V\\A{i}.SLDASM": (1, 1) for i in range(12)}
        seen = []
        self.patch("sw_problem", lambda: None)
        self.patch("index_source", lambda: ("everything", "test"))
        self.patch("_reachable", lambda root, timeout=10: True)
        self.patch("_com_init", lambda: None)
        self.patch("_probe_part_references", lambda parts: None)
        self.patch("everything_list", lambda q: [dict(type="file", path=p, modified=m, size=sz) for p, (m, sz) in files.items()])
        self.patch("WORKERS", 2)

        def read(p):
            with wu._db_lock:
                seen.append(wu.conn().execute("SELECT COUNT(*) FROM inflight").fetchone()[0])
            time.sleep(0.01)
            return [], 1, None, 17000, {}
        self.patch("read_document", read)
        real_store_delay = wu._apply_renames_to_rows
        self.patch("_apply_renames_to_rows", lambda rows: (time.sleep(0.05), real_store_delay(rows))[1])   # slow storing
        wu.run_index()
        self.assertLessEqual(max(seen), 2)                                # never more than the readers


class CopiesSharingAnOldPathTests(Base):
    """As in the test output: copies of a project in several folders, all naming the same old location
    (D:\\Old Cloud\\...). Each copy's external references point to its OWN files. Nothing may be carried over
    from one copy to another."""

    def test_each_copy_keeps_its_own_files(self):
        old = "D:\\Old Cloud\\Work\\Cabinet\\CB-150.SLDPRT"
        copies = {}
        for m in ("CopyA", "CopyB", "PackGo"):
            d = self.dir / m
            d.mkdir()
            (d / "CB-000.SLDASM").write_text("asm")
            (d / "CB-150.SLDPRT").write_text("part " + m)
            copies[m] = (str(d / "CB-000.SLDASM"), str(d / "CB-150.SLDPRT"))
        files = {p: (2, 2) for pair in copies.values() for p in pair}
        self.patch("sw_problem", lambda: None)
        self.patch("index_source", lambda: ("everything", "test"))
        self.patch("_reachable", lambda root, timeout=10: True)
        self.patch("_com_init", lambda: None)
        self.patch("_probe_part_references", lambda parts: None)
        self.patch("_root_reachable_cached", lambda *a, **k: True)
        self.patch("everything_list", lambda q: [dict(type="file", path=p, modified=m, size=sz) for p, (m, sz) in files.items()])
        by_asm = {asm: part for asm, part in copies.values()}

        def read(p):
            if p in by_asm:                                   # each copy finds ITS file in its external references
                return [(p.lower(), "Default", "cb-150.sldprt", old, "", 1, 0, 0)], 1, None, 17000, {}, [(old, by_asm[p])]
            return [], 1, None, 17000, {}
        self.patch("read_document", read)
        wu.run_index()
        for asm, part in copies.values():
            self.assertEqual([x["path"] for x in wu.whereused(part)["direct"]], [asm])
        self.assertEqual(wu._hint_cache["map"], {})                         # they disagree: no hint for others

    def test_cleanup_of_what_earlier_versions_did(self):
        """An earlier version stored 'old -> CopyA copy' for everyone and pointed the other copies at it."""
        db = wu.conn()
        old, copy_a = "d:\\old cloud\\cb-150.sldprt", "G:\\CopyA\\CB-150.SLDPRT"
        db.execute("INSERT INTO renamed (old, new, at, shown) VALUES (?,?,?,?)", (old, copy_a, 1, old))
        db.execute("INSERT INTO renamed (old, new, at, shown) VALUES (?,?,?,?)", ("g:\\v\\mine.sldprt", "G:\\V\\Mine-2.SLDPRT", 1, ""))
        (self.dir / "data").mkdir(exist_ok=True)
        (self.dir / "data" / "rename_log.csv").write_text("Time,User,Action,Old,New,Result\n2026-01-01,s,rename,G:\\V\\Mine.SLDPRT,G:\\V\\Mine-2.SLDPRT,done\n",
                                                          encoding="utf-8")
        self.ref("G:\\CopyB\\CB-000.SLDASM", "Default", copy_a)    # pointed at the CopyA copy by the old fault
        db.execute("UPDATE files SET mtime=5 WHERE path=?", ("g:\\copyb\\cb-000.sldasm",))
        db.commit()
        self.assertEqual(wu._cleanup_global_detections(), 1)
        self.assertEqual([o for (o,) in db.execute("SELECT old FROM renamed")], ["g:\\v\\mine.sldprt"])   # own rename kept
        self.assertEqual(db.execute("SELECT mtime FROM files WHERE path=?", ("g:\\copyb\\cb-000.sldasm",)).fetchone()[0], 0)
        self.assertEqual(wu._cleanup_global_detections(), 0)                    # only once
