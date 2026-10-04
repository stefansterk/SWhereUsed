"""
Automated tests for SWhereUsed. Run:  python SWhereUsed.py --selftest   (or: python -m unittest discover tests)

The Document Manager and Everything are simulated: no SOLIDWORKS needed, no real CAD files touched.
Everything happens in temporary folders.
"""
import contextlib
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import SWhereUsed as wu  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._stack = contextlib.ExitStack()
        self.patch("DATA_DIR", self.dir / "data")
        self.patch("DATABASE", "")
        self.patch("_db", None)
        self.patch("INDEX_FOLDERS", [])
        self.patch("INDEX_SOURCE", "auto")
        self.patch("EXCLUDE", list(wu.EXCLUDE))
        self.patch("SW_KEY", "")
        self.patch("_probe_ext_refs", lambda assemblies: None)       # the real one starts a process
        self.patch("SW_KEY_SOURCE", None)
        wu._counts_cache.clear()
        wu._reach_cache.clear()
        wu._ev_cache.update(t=0.0, value=None)
        for cache in (wu._listing_cache, wu._fstatus_cache, wu._isdir_cache, wu._structure_cache, wu._unused_cache,
                      wu._count_cache):
            cache.clear()
        for cache in (wu._broken_cache, wu._stats_cache, wu._index_maps):
            cache.clear()
            cache["key"] = None
        wu._renamed_cache.update(t=0.0, map={})
        wu._vault_cache.clear()
        wu._copied_cache.update(t=0.0, map={})

    def tearDown(self):
        if wu._db is not None:
            wu._db.close()
        self._stack.close()
        self._tmp.cleanup()

    def patch(self, name, value):
        return self._stack.enter_context(mock.patch.object(wu, name, value))

    def ref(self, parent, pcfg, child, stored=None, ccfg="", qty=1, supp=0, virt=0):
        db = wu.conn()
        db.execute("INSERT INTO refs VALUES (?,?,?,?,?,?,?,?)",
                   (parent.lower(), pcfg, wu._fname(child).lower(), stored or child, ccfg, qty, supp, virt))
        db.execute("INSERT OR IGNORE INTO files VALUES (?,?,?,?,?,?,?,?,?)",
                   (parent.lower(), parent, 1.0, 1, time.time(), "ok", None, 1, f"desc of {wu._fname(parent)}"))
        db.commit()


# ---------------------------------------------------------------------------
class SettingsTests(Base):
    def test_template_is_created_and_read_back(self):
        wu.load_settings()
        ini = self.dir / "data" / "settings.ini"
        self.assertTrue(ini.exists())
        text = ini.read_text(encoding="utf-8")
        self.assertIn("[index]", text)
        self.assertIn("everything_query = ext:sldasm;slddrw", text)

    def test_values_lists_and_invalid_values(self):
        ini = self.dir / "data" / "settings.ini"
        ini.parent.mkdir(parents=True)
        ini.write_text("[index]\nfolders = D:\\Vault; \\\\srv\\cad, with comma\nworkers = lots\n"
                       "[solidworks]\ndescription_properties = Omschrijving, Description\n", encoding="utf-8")
        with mock.patch.object(wu, "WORKERS", 4), mock.patch.object(wu, "DESCRIPTION_PROPS", ["Description"]):
            wu.load_settings()
            self.assertEqual(wu.INDEX_FOLDERS, ["D:\\Vault", "\\\\srv\\cad, with comma"])   # ; separates, not ,
            self.assertEqual(wu.DESCRIPTION_PROPS, ["Omschrijving", "Description"])
            self.assertEqual(wu.WORKERS, 4)
            self.assertTrue(any("workers" in p for p in wu.SETTINGS_PROBLEMS))
        # Settings that were missing were added with their default, the user's lines kept
        text = ini.read_text(encoding="utf-8")
        self.assertIn("folders = D:\\Vault; \\\\srv\\cad, with comma", text)
        self.assertIn("interval_minutes = ", text)
        self.assertIn("[app]", text)

    def test_set_setting_keeps_comments(self):
        wu.load_settings()
        wu.set_setting("index", "source", "folders")
        wu.set_setting("everything", "url", "http://127.0.0.1:9090")
        text = (self.dir / "data" / "settings.ini").read_text(encoding="utf-8")
        self.assertIn("source = folders", text)
        self.assertIn("url = http://127.0.0.1:9090", text)
        self.assertIn("# Where to find assemblies and drawings:", text)
        self.assertEqual(text.count("source ="), 1)

    def test_key_is_stored_in_the_data_folder(self):
        wu.save_key('  "ABC-123"  ')
        self.assertEqual(wu.SW_KEY, "ABC-123")
        self.assertEqual((self.dir / "data" / "swdm_key.txt").read_text().strip(), "ABC-123")
        self.assertFalse((wu.HERE / "swdm_key.txt").exists() and wu.SW_KEY_SOURCE == str(wu.HERE / "swdm_key.txt"))


# ---------------------------------------------------------------------------
class MatchTests(Base):
    def test_kinds(self):
        with mock.patch.object(wu, "_root_reachable_cached", return_value=True):
            self.assertEqual(wu.match_kind("G:\\Vault\\Bolt.SLDPRT", "g:\\vault\\bolt.sldprt"), "exact")
            self.assertEqual(wu.match_kind("\\\\srv\\cad\\Vault\\Bolt.SLDPRT", "G:\\Vault\\Bolt.SLDPRT"), "remapped")
            self.assertEqual(wu.match_kind("X:\\Old\\Bolt.SLDPRT", "G:\\Vault\\Bolt.SLDPRT"), "path_missing")
            self.assertEqual(wu.match_kind("", "G:\\Vault\\Bolt.SLDPRT"), "path_missing")
            self.assertEqual(wu.match_kind("C:\\Users\\a\\AppData\\Local\\Temp\\swx1\\Bolt.SLDPRT", "G:\\V\\Bolt.SLDPRT"),
                             "path_missing")
            other = self.dir / "copy" / "Bolt.SLDPRT"
            other.parent.mkdir()
            other.write_bytes(b"x")
            self.assertEqual(wu.match_kind(str(other), "G:\\Vault\\Bolt.SLDPRT"), "other_copy")


# ---------------------------------------------------------------------------
class WhereUsedTests(Base):
    P, S, T, T2, D = ("G:\\V\\Bolt.SLDPRT", "G:\\V\\Sub.SLDASM", "G:\\V\\Top.SLDASM",
                      "G:\\V\\Old.SLDASM", "G:\\V\\Bolt.SLDDRW")

    def seed(self):
        self.ref(self.S, "Default", self.P, qty=2, ccfg="M8")
        self.ref(self.S, "Long", self.P, qty=4, ccfg="M10")
        self.ref(self.T, "A", self.S, qty=3, ccfg="Default")
        self.ref(self.T, "B", self.S, qty=1, ccfg="Default")
        self.ref(self.T, "C", self.S, qty=5, ccfg="Long")
        self.ref(self.T2, "Default", self.S, qty=7, ccfg="Default", supp=1)      # suppressed: not counted
        self.ref(self.D, "", self.P, qty=2, ccfg="M8")                            # drawing: 2 views
        self.ref(self.S, "Default", "C:\\x\\swx123\\IC~~\\Virtual^Sub.SLDPRT", virt=1)

    def run_wu(self, *a, **kw):
        with mock.patch.object(wu, "_root_reachable_cached", return_value=True):
            return wu.whereused(*a, **kw)

    def test_direct_and_top_level(self):
        self.seed()
        d = self.run_wu(self.P)
        self.assertEqual([x["kind"] for x in d["direct"]], ["assembly", "drawing"])
        sub = d["direct"][0]
        self.assertEqual(sub["description"], "desc of Sub.SLDASM")
        self.assertEqual(sorted((c["config"], c["qty"]) for c in sub["configs"]), [("Default", 2), ("Long", 4)])
        tops = {(t["path"], t["config"]): t["qty"] for t in d["top"]}
        # Top A: 3 x Sub<Default> x 2 bolts = 6; B: 1 x 2 = 2; C: 5 x Sub<Long> x 4 = 20
        self.assertEqual(tops, {(self.T, "A"): 6, (self.T, "B"): 2, (self.T, "C"): 20})
        self.assertEqual(d["child_configs"], ["M10", "M8"])
        self.assertEqual(d["top"][0]["example"], [self.S, self.T])

    def test_configuration_filter(self):
        self.seed()
        d = self.run_wu(self.P, config="M10")
        self.assertEqual([c["config"] for c in d["direct"][0]["configs"]], ["Long"])
        self.assertEqual({t["config"]: t["qty"] for t in d["top"]}, {"C": 20})
        self.assertEqual(len(d["direct"]), 1)          # the drawing shows M8 only

    def test_other_copies_hidden_unless_asked(self):
        other = self.dir / "elsewhere" / "Bolt.SLDPRT"
        other.parent.mkdir()
        other.write_bytes(b"x")
        self.ref(self.S, "Default", self.P)
        self.ref("G:\\W\\Foreign.SLDASM", "Default", str(other))
        self.assertEqual(len(self.run_wu(self.P)["direct"]), 1)
        d = self.run_wu(self.P, include_other=True)
        self.assertEqual(sorted(x["match"] for x in d["direct"]), ["exact", "other_copy"])

    def test_same_config_saved_under_two_paths_is_added_up(self):
        self.ref(self.S, "Default", self.P, qty=2, ccfg="M8")
        self.ref(self.S, "Default", "X:\\Gone\\Bolt.SLDPRT", qty=1, ccfg="M8")     # old path, same file
        self.ref(self.S, "Default", self.P, qty=3, ccfg="M10")
        d = self.run_wu(self.P)
        sub = d["direct"][0]
        self.assertEqual(sub["match"], "exact")                                   # best match wins
        self.assertEqual([(c["child_config"], c["qty"]) for c in sub["configs"]], [("M10", 3), ("M8", 3)])

    def test_counts_match_the_detail_page(self):
        """The situation from the field: the list said 'used in 40', the detail page showed nothing."""
        self.patch("_root_reachable_cached", lambda *a, **k: True)
        real = self.dir / "vault" / "Bolt.SLDPRT"
        copy = self.dir / "old" / "Bolt.SLDPRT"
        for f in (real, copy):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"x")
        for i in range(40):                                   # 40 assemblies use the real one
            self.ref(f"G:\\V\\Asm{i}.SLDASM", "Default", str(real))
        self.ref("G:\\V\\OldAsm.SLDASM", "Default", str(copy))   # one uses the copy
        counts = wu.usage_for_paths([str(real), str(copy)])
        self.assertEqual(counts[str(real).lower()], (40, 1))
        self.assertEqual(counts[str(copy).lower()], (1, 40))
        d = self.run_wu(str(copy))
        self.assertEqual(len(d["direct"]), 1)                  # same number as the list
        self.assertEqual(d["hidden_other"], 40)                # and the page can explain the rest
        self.assertEqual(d["other_files"], [str(real)])
        with mock.patch.object(wu, "everything_status", return_value=(False, "off")):
            found = {x["path"]: (x["used_in"], x["other_copies"]) for x in wu.search("bolt")["results"]}
        self.assertEqual(found, {str(real): (40, 1), str(copy): (1, 40)})   # both files listed, each its own count

    def test_same_file_through_another_path_is_not_a_copy(self):
        """A drive letter mapped deep into a share: two different paths, one file."""
        self.patch("_root_reachable_cached", lambda *a, **k: True)
        real = self.dir / "share" / "cad" / "Vault" / "Bolt.SLDPRT"
        real.parent.mkdir(parents=True)
        real.write_bytes(b"x")
        alias = self.dir / "Z" / "Bolt.SLDPRT"
        alias.parent.mkdir()
        os.link(real, alias)                                   # same file, other path (like Z:\ = \\srv\cad\Vault)
        self.ref("G:\\V\\Sub.SLDASM", "Default", str(alias))
        self.assertEqual(wu.match_kind(str(alias), str(real)), "remapped")
        self.assertEqual(len(self.run_wu(str(real))["direct"]), 1)
        self.assertEqual(wu.usage_for_paths([str(real)])[str(real).lower()], (1, 0))

    def test_cycles_do_not_hang(self):
        a, b = "G:\\V\\A.SLDASM", "G:\\V\\B.SLDASM"
        self.ref(a, "Default", self.P)
        self.ref(b, "Default", a)
        self.ref(a, "Default", b)                        # A in B in A
        d = self.run_wu(self.P)
        self.assertEqual(len(d["direct"]), 1)
        self.assertFalse(d["truncated"])

    def test_not_used(self):
        self.seed()
        d = self.run_wu("G:\\V\\Unused.SLDPRT")
        self.assertEqual(d["direct"], [])
        self.assertEqual(d["top"], [])

    def test_search_from_index(self):
        self.seed()
        with mock.patch.object(wu, "everything_status", return_value=(False, "off")):
            r = wu.search("bolt")
        self.assertEqual(r["via"], "index")
        names = {wu._fname(x["path"]).lower(): x["used_in"] for x in r["results"]}
        self.assertEqual(names.get("bolt.sldprt"), 2)
        self.assertIn("bolt.slddrw", names)
        self.assertNotIn("virtual^sub.sldprt", names)
        with mock.patch.object(wu, "everything_status", return_value=(False, "off")):
            self.assertEqual(wu.search("100%_sure")["results"], [])    # LIKE wildcards are escaped


# ---------------------------------------------------------------------------
class IndexTests(Base):
    def setUp(self):
        super().setUp()
        self.files = {}
        self.reads = []
        self.failing = set()
        self.patch("sw_problem", lambda: None)
        self.patch("index_source", lambda: ("everything", "test"))
        self.patch("_reachable", lambda root, timeout=10: True)
        self.patch("_com_init", lambda: None)
        self.patch("everything_list", lambda q: [dict(type="file", path=p, modified=m, size=s)
                                                 for p, (m, s) in self.files.items()])
        self.patch("read_document", self.fake_read)
        self.patch("_probe_part_references", lambda parts: None)

    def fake_read(self, path):
        self.reads.append(wu._fname(path))
        if path in self.failing:
            raise RuntimeError("could not open the file (code 6: saved with a newer SOLIDWORKS)")
        return [(path.lower(), "Default", "bolt.sldprt", "G:\\V\\Bolt.SLDPRT", "", 1, 0, 0)], 1, "A description"

    def test_incremental_update(self):
        a, b, c = "G:\\V\\A.SLDASM", "G:\\V\\B.SLDDRW", "G:\\V\\C.SLDASM"
        self.files = {a: (100, 10), b: (100, 10), c: (100, 10), "G:\\V\\~$A.SLDASM": (1, 1),
                      "G:\\V\\Part.SLDPRT": (1, 1)}
        self.failing = {c}
        wu.run_index()
        self.assertEqual(sorted(self.reads), ["A.SLDASM", "B.SLDDRW", "C.SLDASM", "Part.SLDPRT"])   # no lock files
        st = wu.status()
        self.assertEqual((st["assemblies"], st["drawings"], st["parts"], st["failed"]), (1, 1, 1, 1))
        self.assertIn("4 file(s) read", st["message"])

        self.reads.clear()
        wu._counts_cache.clear()
        wu.run_index()                                  # nothing changed, error not retried yet
        self.assertEqual(self.reads, [])

        self.files[a] = (200, 10)                       # changed
        del self.files[b]                               # deleted
        wu._meta_set("retry_errors", "1")               # the Update button retries unreadable files
        self.failing = set()
        wu.run_index()
        self.assertEqual(sorted(self.reads), ["A.SLDASM", "C.SLDASM"])
        rows = dict(wu.conn().execute("SELECT path, status FROM files").fetchall())
        self.assertEqual(rows, {a.lower(): "ok", c.lower(): "ok", "g:\\v\\part.sldprt": "ok"})
        self.assertEqual(wu.conn().execute("SELECT COUNT(*) FROM refs WHERE parent=?", (b.lower(),)).fetchone()[0], 0)

    def test_unreachable_drive_is_kept(self):
        a, n = "G:\\V\\A.SLDASM", "N:\\Net\\B.SLDASM"
        self.files = {a: (1, 1), n: (1, 1)}
        wu.run_index()
        self.reads.clear()
        self.patch("_reachable", lambda root, timeout=10: not root.upper().startswith("N:"))
        self.files[n] = (2, 2)                          # changed, but the drive is offline now
        wu.run_index()
        self.assertEqual(self.reads, [])
        self.assertIsNotNone(wu.conn().execute("SELECT 1 FROM files WHERE path=?", (n.lower(),)).fetchone())
        self.assertEqual(json.loads(wu._meta_get("unreachable"))["roots"], ["N:\\"])

    def test_crash_bookkeeping(self):
        db = wu.conn()
        db.execute("INSERT INTO inflight VALUES (?,?,?,?,?)", ("g:\\v\\bad.sldasm", "G:\\V\\Bad.SLDASM", 1, 1, time.time()))
        db.commit()
        crashed, single = wu._mark_inflight_crashed("exit code 3221226356")
        self.assertTrue(single)
        self.assertEqual(crashed, ["G:\\V\\Bad.SLDASM"])
        self.assertEqual(db.execute("SELECT status FROM files").fetchone()[0], "crash")
        p = wu.problems()
        self.assertEqual(p["summary"][0]["status"], "crash")


# ---------------------------------------------------------------------------
class FolderScanTests(Base):
    def test_scan(self):
        root = self.dir / "vault"
        for rel in ("a/Top.SLDASM", "a/b/Detail.slddrw", "a/Part.SLDPRT", "a/~$Top.SLDASM",
                    "a/$Recycle.Bin/Old.SLDASM", "c/Other.sldasm"):
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
        self.patch("EXCLUDE", ["$Recycle.Bin"])
        found, unreachable = wu.scan_folders([str(root), str(self.dir / "missing")])
        self.assertEqual(sorted(Path(f["path"]).name for f in found),
                         ["Detail.slddrw", "Other.sldasm", "Part.SLDPRT", "Top.SLDASM"])
        with mock.patch.object(wu, "INCLUDE_PARTS", "no"):
            found, _ = wu.scan_folders([str(root)])
        self.assertNotIn("Part.SLDPRT", [Path(f["path"]).name for f in found])
        self.assertEqual(unreachable, [str(self.dir / "missing")])

    def test_source_choice(self):
        with mock.patch.object(wu, "everything_status", return_value=(False, "Everything is not reachable")):
            self.assertIsNone(wu.index_source()[0])
            self.patch("INDEX_FOLDERS", ["D:\\Vault"])
            self.assertEqual(wu.index_source()[0], "folders")
        with mock.patch.object(wu, "everything_status", return_value=(True, "ok")):
            self.assertEqual(wu.index_source()[0], "everything")
            self.patch("INDEX_SOURCE", "folders")
            self.assertEqual(wu.index_source()[0], "folders")


class SchedulerTests(Base):
    def run_ticks(self, ticks, source_at_tick, interval=60, on_start="yes"):
        """Run the scheduler for a number of ticks; the file source appears at tick `source_at_tick`."""
        clock = {"t": 1000.0, "tick": 0}
        started = []

        def sleep(_s):
            clock["tick"] += 1
            clock["t"] += 15
            if clock["tick"] >= ticks:
                raise StopIteration
        with mock.patch.object(wu, "sw_problem", lambda: None), \
             mock.patch.object(wu, "index_source", lambda: ("everything", "ok") if clock["tick"] >= source_at_tick else (None, "off")), \
             mock.patch.object(wu, "update_index", lambda *a, **k: started.append(clock["tick"]) or True), \
             mock.patch.object(wu, "INTERVAL_MIN", interval), mock.patch.object(wu, "UPDATE_ON_START", on_start), \
             mock.patch.object(wu.time, "sleep", sleep), mock.patch.object(wu.time, "time", lambda: clock["t"]):
            with self.assertRaises(StopIteration):
                wu._scheduler()
        return started

    def test_starts_as_soon_as_everything_answers(self):
        # Everything appears after 3 ticks (45 s): the index starts right then, not an hour later
        self.assertEqual(self.run_ticks(10, source_at_tick=3), [3])

    def test_interval_after_first_run(self):
        # 60 min = 240 ticks of 15 s
        self.assertEqual(self.run_ticks(500, source_at_tick=0), [0, 240, 480])

    def test_no_update_on_start(self):
        self.assertEqual(self.run_ticks(300, source_at_tick=0, on_start="no"), [240])


class AutostartTests(Base):
    def test_on_and_off(self):
        appdata = self.dir / "Roaming Ünïcode"
        with mock.patch.dict(os.environ, {"APPDATA": str(appdata)}):
            self.assertFalse(wu.autostart_state()["on"])
            st = wu.set_autostart(True)
            self.assertTrue(st["on"])
            f = Path(st["file"])
            self.assertEqual(f.parent.name, "Startup")
            text = f.read_bytes().decode("utf-8")
            self.assertIn("chcp 65001", text)
            self.assertIn(f'""{wu.HERE / "Start SWhereUsed.bat"}" --no-browser"', text)
            self.assertIn("\r\n", text)
            self.assertFalse(wu.set_autostart(False)["on"])
            self.assertFalse(f.exists())

    def test_setup_endpoint(self):
        with mock.patch.dict(os.environ, {"APPDATA": str(self.dir)}):
            code, h = wu.apply_setup({"autostart": True})
            self.assertEqual(code, 200)
            self.assertTrue(h["autostart"]["on"])
            self.assertFalse(wu.apply_setup({"autostart": False})[1]["autostart"]["on"])


# ---------------------------------------------------------------------------
class HttpTests(Base):
    def setUp(self):
        super().setUp()
        self.server = wu.ThreadingHTTPServer(("127.0.0.1", 0), wu.Handler)
        self.port = self.server.server_address[1]
        self.patch("APP_PORT", self.port)
        self.patch("everything_status", lambda max_age=10: (False, "Everything is not reachable"))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": f"127.0.0.1:{self.port}", **(headers or {})}
        data = json.dumps(body).encode() if body is not None else None
        if data:
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, raw

    def test_page_and_health(self):
        code, raw = self.req("GET", "/whereused?path=x")
        self.assertEqual(code, 200)
        self.assertIn(b"<title>SWhereUsed</title>", raw)
        code, raw = self.req("GET", "/api/health")
        h = json.loads(raw)
        self.assertEqual(h["version"], wu.APP_VERSION)
        self.assertIn("ready", h)

    def test_port_state(self):
        self.assertEqual(wu._port_state(), "whereused")               # our own test server answers
        quiet = type("Quiet", (wu.BaseHTTPRequestHandler,), {"log_message": lambda *a: None})
        other = wu.ThreadingHTTPServer(("127.0.0.1", 0), quiet)   # another program: answers 501 to everything
        threading.Thread(target=other.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(wu, "APP_PORT", other.server_address[1]):
                self.assertEqual(wu._port_state(), "other")
        finally:
            other.shutdown()
            other.server_close()
        free = wu.ThreadingHTTPServer(("127.0.0.1", 0), wu.Handler)
        port = free.server_address[1]
        free.server_close()
        with mock.patch.object(wu, "APP_PORT", port):
            self.assertEqual(wu._port_state(), "free")

    def test_foreign_host_and_origin_refused(self):
        self.assertEqual(self.req("GET", "/api/health", headers={"Host": "evil.example:80"})[0], 403)
        self.assertEqual(self.req("POST", "/api/update", {}, {"Origin": "http://evil.example"})[0], 403)

    def test_setup_saves_settings(self):
        code, raw = self.req("POST", "/api/setup", {"source": "folders", "folders": ["D:\\Vault", " "],
                                                    "everything_url": "127.0.0.1:8081"},
                             {"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(code, 200)
        h = json.loads(raw)
        self.assertEqual(h["folders"], ["D:\\Vault"])
        self.assertEqual(h["source"], "folders")
        text = (self.dir / "data" / "settings.ini").read_text(encoding="utf-8")
        self.assertIn("folders = D:\\Vault", text)
        self.assertIn("url = http://127.0.0.1:8081", text)

    def test_whereused_endpoint(self):
        self.ref("G:\\V\\Sub.SLDASM", "Default", "G:\\V\\Bolt.SLDPRT", qty=2)
        code, raw = self.req("GET", "/api/whereused?path=" + "G%3A%5CV%5CBolt.SLDPRT")
        self.assertEqual(code, 200)
        d = json.loads(raw)
        self.assertEqual(d["direct"][0]["configs"][0]["qty"], 2)
        self.assertEqual(self.req("GET", "/api/whereused")[0], 400)


if __name__ == "__main__":
    unittest.main()
