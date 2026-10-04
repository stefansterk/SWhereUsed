"""Statistics from the index, and progress per file type while indexing."""
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import SWhereUsed as wu  # noqa: E402
from test_whereused import Base  # noqa: E402

V = "G:\\V\\"


class StatsTests(Base):
    def setUp(self):
        super().setUp()
        db = wu.conn()

        def file(p, mtime=None, desc="d", status="ok", size=100):
            db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?)",
                       (p.lower(), p, mtime or time.time(), size, time.time(), status, None, 1, desc))
        for p in ("Top.SLDASM", "Sub.SLDASM", "Old.SLDASM"):
            file(V + p)
        file(V + "Sub.SLDDRW")
        file(V + "Bolt.SLDPRT")
        file(V + "Nut.SLDPRT", desc=None)
        file(V + "Spare.SLDPRT", mtime=time.mktime((2012, 5, 1, 0, 0, 0, 0, 0, -1)))
        file("G:\\Other\\Bolt.SLDPRT")                      # same name, other folder
        file(V + "Broken.SLDASM", status="error")
        db.commit()
        self.ref(V + "Top.SLDASM", "A", V + "Sub.SLDASM", qty=2)
        self.ref(V + "Top.SLDASM", "B", V + "Sub.SLDASM", qty=1)
        self.ref(V + "Top.SLDASM", "B", V + "Bolt.SLDPRT", qty=4)
        self.ref(V + "Sub.SLDASM", "Default", V + "Bolt.SLDPRT", qty=8)
        self.ref(V + "Sub.SLDASM", "Default", V + "Nut.SLDPRT", qty=8)
        self.ref(V + "Sub.SLDDRW", "", V + "Sub.SLDASM")

    def test_numbers(self):
        s = wu.statistics()
        self.assertEqual({k: v["count"] for k, v in s["types"].items()}, {"asm": 3, "drw": 1, "prt": 4})
        self.assertEqual(s["unreadable"], 1)
        self.assertEqual(s["unused_parts"], 1)                       # Spare (Bolt is used, by name, also the copy)
        self.assertEqual([wu._fname(p) for p in s["top_level_sample"]], ["Old.SLDASM", "Top.SLDASM"])
        self.assertEqual(s["no_drawing"], {"asm": 2, "prt": 4})      # only Sub has a drawing
        self.assertEqual(s["no_description"], {"asm": 0, "prt": 1})
        self.assertEqual((s["duplicate_names"], s["duplicate_files"]), (1, 2))
        self.assertEqual(s["duplicates"][0]["name"], "Bolt.SLDPRT")        # as it is written, with extension
        self.assertEqual([(m["name"], m["used_in"]) for m in s["most_used"][:2]], [("bolt.sldprt", 2), ("nut.sldprt", 1)])
        # All levels: Top (config A) = 2x Sub, each with 8 Bolt and 8 Nut: 3 different (Sub, Bolt, Nut), 2 x (1 + 16) pieces
        self.assertEqual([(wu._fname(x["path"]), x["unique"], x["qty"], x["config"], x["direct"]) for x in s["largest"]],
                         [("Top.SLDASM", 3, 34, "A", 2), ("Sub.SLDASM", 2, 16, "Default", 2)])
        self.assertEqual(s["older"]["prt"], 1)                       # 2012: older than ten years
        self.assertEqual(sum(y["asm"] for y in s["years"]), 3)

    def test_kept_until_the_index_changes(self):
        wu.statistics()
        self.assertTrue(wu.statistics().get("cached"))
        time.sleep(0.01)
        self.ref(V + "New.SLDASM", "Default", V + "Spare.SLDPRT")
        s = wu.statistics()
        self.assertFalse(s.get("cached"))
        self.assertEqual(s["unused_parts"], 0)


class ProgressByTypeTests(Base):
    def test_by_type(self):
        files = {V + "A.SLDASM": (1, 1), V + "B.SLDASM": (1, 1), V + "C.SLDDRW": (1, 1), V + "D.SLDPRT": (1, 1)}
        self.patch("sw_problem", lambda: None)
        self.patch("index_source", lambda: ("everything", "test"))
        self.patch("_reachable", lambda root, timeout=10: True)
        self.patch("_com_init", lambda: None)
        self.patch("_probe_part_references", lambda parts: None)
        self.patch("everything_list", lambda q: [dict(type="file", path=p, modified=m, size=s) for p, (m, s) in files.items()])
        self.patch("read_document", lambda p: ([], 1, None) if not p.endswith("B.SLDASM") else (_ for _ in ()).throw(RuntimeError("x")))
        wu.run_index()
        self.assertEqual(wu._state["by_type"], {"asm": [2, 2, 1], "drw": [1, 1, 0], "prt": [1, 1, 0]})


class LargestTests(Base):
    def test_shared_subassembly_is_counted_once_and_loops_stop(self):
        a, b, c = V + "A.SLDASM", V + "B.SLDASM", V + "C.SLDASM"
        for p in (a, b, c):
            wu.conn().execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?)", (p.lower(), p, 1, 1, 1, "ok", None, 1, None))
        self.ref(a, "Default", b, qty=2)
        self.ref(a, "Default", c, qty=1)
        self.ref(b, "Default", V + "X.SLDPRT", qty=3)
        self.ref(c, "Default", V + "X.SLDPRT", qty=5)       # X in both: one different part
        self.ref(c, "Default", a, qty=1)                    # a loop (damaged files): must stop
        top = {wu._fname(x["path"]): (x["unique"], x["qty"]) for x in wu.statistics()["largest"]}
        self.assertEqual(top["A.SLDASM"][0], 3)            # B, C, X
        self.assertEqual(top["B.SLDASM"], (1, 3))


class FunFactTests(Base):
    def setUp(self):
        super().setUp()
        db = wu.conn()
        at = lambda y, mo, d, h: time.mktime((y, mo, d, h, 30, 0, 0, 0, -1))   # noqa: E731

        def file(name, when, desc=None, size=100, configs=1, folder=V):
            p = folder + name
            db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?)",
                       (p.lower(), p, when, size, time.time(), "ok", None, configs, desc))
            return p
        self.top = file("Top.SLDASM", at(2024, 3, 5, 10), "Frame welded", configs=7)      # a Tuesday
        self.sub = file("Sub.SLDASM", at(2024, 3, 5, 10), "Frame bolted")
        self.old = file("Bracket.SLDPRT", at(2009, 6, 1, 23), "Bracket steel", size=5_000_000)
        file("Unused.SLDPRT", at(2001, 1, 1, 9), "Bracket")                                 # older, but not used
        file("Top.SLDDRW", at(2024, 3, 5, 11), configs=4)
        file("P.SLDPRT", at(2025, 8, 9, 14), folder="G:\\" + "Very long folder name\\" * 11)
        file("Future.SLDPRT", time.time() + 400 * 86400)                               # a pc with a wrong clock
        db.commit()
        self.ref(self.top, "Default", self.sub)
        self.ref(self.sub, "Default", self.old, qty=2)
        self.ref(V + "Other.SLDASM", "Default", self.old)

    def test_facts(self):
        f = wu.statistics()["fun"]
        self.assertEqual(wu._fname(f["oldest_used"]["path"]), "Bracket.SLDPRT")            # 2009, used in 2 files
        self.assertEqual(f["oldest_used"]["used_in"], 2)
        self.assertEqual(wu._fname(f["newest"]["path"]), "P.SLDPRT")
        self.assertEqual(f["busiest_day"], {"date": "2024-03-05", "count": 3})
        self.assertEqual(f["weekday"]["day"], "Tuesday")
        self.assertEqual(f["hour"]["hour"], 10)
        self.assertGreaterEqual(f["hour"]["night"], 1)                                     # 23:30 in 2009
        self.assertEqual((wu._fname(f["biggest"]["path"]), f["biggest"]["size"]), ("Bracket.SLDPRT", 5_000_000))
        self.assertEqual(wu._fname(f["longest_path"]["path"]), "P.SLDPRT")
        self.assertEqual(f["longest_path"]["near_limit"], 1)
        self.assertEqual((wu._fname(f["most_configs"]["path"]), f["most_configs"]["count"]), ("Top.SLDASM", 7))
        self.assertEqual((wu._fname(f["most_sheets"]["path"]), f["most_sheets"]["count"]), ("Top.SLDDRW", 4))
        self.assertEqual((wu._fname(f["deepest"]["path"]), f["deepest"]["levels"]), ("Top.SLDASM", 2))
        self.assertEqual([w["word"] for w in f["words"][:2]], ["bracket", "frame"])


class VersionTests(Base):
    def test_release_names(self):
        cases = {2500: "2004", 3800: "2008", 5000: "2012", 6000: "2013", 15000: "2022", 17000: "2024",
                 19000: "2026", 20000: "2027", 15123: "2022", 1950: "2001 Plus", 0: None, None: None, -3: None}
        for number, name in cases.items():
            self.assertEqual(wu.sw_release(number), name, number)

    def test_the_whole_table(self):
        """The list from the Document Manager API help, 2000 to 2026."""
        table = {1500: "2000", 1750: "2001", 1950: "2001 Plus", 2200: "2003", 2500: "2004", 2800: "2005",
                 3100: "2006", 3400: "2007", 3800: "2008", 4100: "2009", 4400: "2010", 4700: "2011", 5000: "2012"}
        table.update({6000 + 1000 * i: str(2013 + i) for i in range(14)})          # 2013 = 6000 ... 2026 = 19000
        self.assertEqual(table[19000], "2026")
        for number, release in table.items():
            self.assertEqual(wu.sw_release(number), release, number)

    def test_counts_per_release_and_pending(self):
        db = wu.conn()
        for i, (name, v) in enumerate((("A.SLDASM", 17000), ("B.SLDPRT", 17000), ("C.SLDPRT", 11000),
                                       ("D.SLDDRW", 19000), ("E.SLDPRT", 0), ("F.SLDPRT", None))):
            p = V + name
            db.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?)", (p.lower(), p, 1, 1, 1, "ok", None, 1, None))
            if v is not None:
                db.execute("INSERT INTO versions VALUES (?,?)", (p.lower(), v))
        db.commit()
        self.ref(V + "A.SLDASM", "Default", V + "C.SLDPRT")
        s = wu.statistics()
        self.assertEqual([(x["release"], x["asm"], x["drw"], x["prt"]) for x in s["versions"]],
                         [("2026", 0, 1, 0), ("2024", 1, 0, 1), ("2018", 0, 0, 1)])
        self.assertEqual((s["versions_pending"], s["versions_unknown"]), (1, 1))
        self.assertEqual((wu._fname(s["fun"]["oldest_release_used"]["path"]), s["fun"]["oldest_release_used"]["release"]),
                         ("C.SLDPRT", "2018"))

    def test_existing_files_are_read_once_for_their_version(self):
        files = {V + "A.SLDASM": (1, 1)}
        reads = []
        self.patch("sw_problem", lambda: None)
        self.patch("index_source", lambda: ("everything", "test"))
        self.patch("_reachable", lambda root, timeout=10: True)
        self.patch("_com_init", lambda: None)
        self.patch("_probe_part_references", lambda parts: None)
        self.patch("everything_list", lambda q: [dict(type="file", path=p, modified=m, size=s) for p, (m, s) in files.items()])
        self.patch("read_document", lambda p: reads.append(p) or ([], 1, None, 17000))
        wu.run_index()
        wu.conn().execute("DELETE FROM versions")                  # as in an index made by an older SWhereUsed
        wu.conn().commit()
        wu.run_index()
        wu.run_index()
        self.assertEqual(len(reads), 2)                              # first run, then once for the version, then not again
        self.assertEqual(wu.conn().execute("SELECT version FROM versions").fetchone()[0], 17000)


class RunHistoryTests(Base):
    def seed(self, n, start, status="ok"):
        db = wu.conn()
        for i in range(n):
            p = f"{V}F{start:.0f}-{i}.{['SLDASM', 'SLDDRW', 'SLDPRT'][i % 3]}"
            db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?)",
                       (p.lower(), p, 1, 1, start + 1 + i, status if i == 0 else "ok", None, 1, None))
        db.commit()

    def test_full_then_update(self):
        t0 = 1_700_000_000
        self.seed(30, t0)
        wu._record_run(t0, t0 + 3600, "30 file(s) read", ok_before=0)
        t1 = t0 + 86400
        self.seed(3, t1)                                                    # a small update the next day
        wu._record_run(t1, t1 + 12, "3 file(s) read", ok_before=30)
        h = wu.run_history()
        self.assertEqual([(r["files"], r["full"], r["seconds"]) for r in h], [(3, False, 12), (30, True, 3600)])
        self.assertEqual(h[1]["by_type"], {"asm": 10, "drw": 10, "prt": 10})
        st = wu.status()
        self.assertEqual((st["last_run"]["files"], st["last_full"]["files"]), (3, 30))

    def test_most_of_the_index_read_again_counts_as_full(self):
        self.seed(10, 1_700_000_000)
        t = 1_700_100_000
        self.seed(10, t)                                                   # 10 new files, all read: 50%? no:
        wu.conn().execute("UPDATE files SET indexed_at=? WHERE path LIKE ?", (t + 5, "%f1700000000%"))
        wu.conn().commit()                                                 # ...everything read again
        wu._record_run(t, t + 100, "20 file(s) read", ok_before=10)
        self.assertTrue(wu.run_history()[0]["full"])

    def test_errors_counted_and_history_kept_short(self):
        for k in range(55):
            wu._record_run(1_600_000_000 + k, 1_600_000_010 + k, "nothing", ok_before=5)
        self.assertEqual(wu.conn().execute("SELECT COUNT(*) FROM runs").fetchone()[0], 50)
        self.seed(4, 1_800_000_000, status="error")
        wu._record_run(1_800_000_000, 1_800_000_060, "done", ok_before=5)
        self.assertEqual(wu.run_history(1)[0]["errors"], 1)


class StatisticsPageTests(Base):
    """The page never waits for a full calculation: last numbers right away, new ones in the background."""

    def setUp(self):
        super().setUp()
        self.ref(V + "Top.SLDASM", "Default", V + "Bolt.SLDPRT")
        self.started = []
        real_thread = wu.threading.Thread
        self.patch("threading", mock.Mock(Thread=lambda target=None, **kw: mock.Mock(
            start=lambda: self.started.append(target) if target is wu._warm_statistics else target and target()),
            Lock=real_thread and wu.threading.Lock))

    def test_first_time_pending_then_fresh(self):
        r = wu.statistics_page()
        self.assertTrue(r["pending"])
        self.assertEqual(len(self.started), 1)                       # worked out in the background
        wu.statistics()                                              # (what that background job does)
        r = wu.statistics_page()
        self.assertTrue(r["fresh"])
        self.assertEqual(r["references"], 1)

    def test_while_indexing_the_last_numbers_come_at_once(self):
        wu.statistics()
        time.sleep(0.01)
        self.ref(V + "Other.SLDASM", "Default", V + "Bolt.SLDPRT")   # the index changes...
        with mock.patch.dict(wu._state, {"running": True}):
            r = wu.statistics_page()
        self.assertFalse(r["fresh"])
        self.assertEqual(r["references"], 1)                         # ...the page gets the last numbers right away
        self.assertEqual(self.started, [])                           # and no new calculation within 5 minutes
        with mock.patch.dict(wu._state, {"running": False}):
            wu.statistics_page()
        self.assertEqual(len(self.started), 1)                       # not indexing: refreshed straight away

    def test_numbers_survive_a_restart(self):
        wu.statistics()
        wu._stats_cache.update(key=None, value=None)                 # as after starting the app again
        r = wu.statistics_page()
        self.assertFalse(r.get("pending"))
        self.assertEqual(r["references"], 1)
