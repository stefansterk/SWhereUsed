"""Search with sorting, pages and totals over ALL matches (not just the first page). Everything is simulated."""
import re
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import SWhereUsed as wu  # noqa: E402
from test_whereused import Base  # noqa: E402


def make_vault():
    files = []
    for i in range(1239):
        ext = [".SLDPRT", ".SLDASM", ".SLDDRW"][i % 3]
        files.append({"path": f"D:\\Vault\\F{i % 7}\\HINGE-{i:04d}{ext}", "modified": 1_600_000_000 + (i * 7919) % 100000})
    files.append({"path": "D:\\Vault\\F1\\~$HINGE-0001.SLDASM", "modified": 1})          # lock file
    files.append({"path": "D:\\$Recycle.Bin\\HINGE-9999.SLDPRT", "modified": 1})       # skipped path
    return files


class FakeEverything:
    """Understands what SWhereUsed sends: ext:a;b, words, !"path" and !~$, sort and ascending, offset and count."""

    def __init__(self, files):
        self.files = files
        self.calls = 0

    def __call__(self, query, count, offset=0, timeout=10, sort=None, ascending=True):
        self.calls += 1
        exts = tuple("." + e for e in re.search(r"ext:(\S+)", query).group(1).lower().split(";"))
        rest = re.sub(r"ext:\S+", "", query)
        whole = re.search(r'wfn:"([^"]+)"', rest)
        rest = re.sub(r'wfn:"[^"]+"', "", rest)
        excl = [x.lower() for x in re.findall(r'!"([^"]+)"', rest)] + (["~$"] if "!~$" in rest else [])
        words = [w.lower() for w in re.sub(r'!"[^"]+"|!~\$', "", rest).split()]
        hits = [f for f in self.files if f["path"].lower().endswith(exts) and all(w in f["path"].lower() for w in words)
                and not any(x in f["path"].lower() for x in excl)
                and (not whole or wu._fname(f["path"]).lower() == whole.group(1).lower())]
        key = {"name": lambda f: wu._fname(f["path"]).lower(), "path": lambda f: f["path"].lower(),
               "date_modified": lambda f: f["modified"]}.get(sort or "name")
        hits.sort(key=key, reverse=not ascending)
        return {"total": len(hits), "results": [dict(f, type="file") for f in hits[offset:offset + count]]}


class SearchTests(Base):
    def setUp(self):
        super().setUp()
        self.files = make_vault()
        self.ev = FakeEverything(self.files)
        self.patch("everything_search", self.ev)
        self.patch("everything_status", lambda max_age=10: (True, "ok"))
        self.patch("EXCLUDE", ["\\$Recycle.Bin\\"])
        self.patch("_root_reachable_cached", lambda *a, **k: True)

    def test_totals_and_counts(self):
        r = wu.search("hinge", limit=200)
        self.assertEqual(r["total"], 1239)                                  # not 200, not 500: all of them
        self.assertEqual(r["counts"], {"": 1239, "part": 413, "asm": 413, "drw": 413})
        self.assertEqual(len(r["results"]), 200)

    def test_sorting_is_over_all_results_and_pages_follow(self):
        newest = max((f for f in self.files if "HINGE-" in f["path"] and "~$" not in f["path"] and "Recycle" not in f["path"]),
                     key=lambda f: f["modified"])
        p1 = wu.search("hinge", limit=200, sort="modified", desc=True)
        self.assertEqual(p1["results"][0]["path"], newest["path"])          # the newest of all 1239
        p2 = wu.search("hinge", limit=200, offset=200, sort="modified", desc=True)
        self.assertGreaterEqual(p1["results"][-1]["modified"], p2["results"][0]["modified"])
        last = wu.search("hinge", limit=200, offset=1200, sort="modified", desc=True)
        self.assertEqual(len(last["results"]), 39)
        seen = {x["path"] for o in range(0, 1239, 200)
                for x in wu.search("hinge", limit=200, offset=o, sort="name")["results"]}
        self.assertEqual(len(seen), 1239)                                   # every file once over the pages

    def test_kind_filter_and_extra_words(self):
        r = wu.search("hinge", limit=50, kind="asm", filt="F3")
        self.assertTrue(all(x["path"].endswith(".SLDASM") and "\\F3\\" in x["path"] for x in r["results"]))
        self.assertEqual(r["total"], sum(1 for f in self.files if f["path"].endswith(".SLDASM") and "\\F3\\" in f["path"]
                                         and "~$" not in f["path"]))

    def test_sort_by_used_and_only_unused(self):
        parts = [f["path"] for f in self.files if f["path"].endswith(".SLDPRT") and "Recycle" not in f["path"]]
        busy = parts[500 // 3]                                               # used in 3 assemblies
        for i in range(3):
            self.ref(f"D:\\Vault\\A{i}.SLDASM", "Default", busy)
        self.ref("D:\\Vault\\A9.SLDASM", "Default", parts[10])
        r = wu.search("hinge", limit=10, sort="used", desc=True, kind="part")
        self.assertEqual([(wu._fname(x["path"]), x["used_in"]) for x in r["results"][:2]],
                         [(wu._fname(busy), 3), (wu._fname(parts[10]), 1)])
        db = wu.conn()                                                   # the parts are in the index too
        for p in parts:
            db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?)", (p.lower(), p, 1, 1, 1, "ok", None, 1, None))
        db.commit()
        u = wu.search("hinge", limit=10, kind="part", unused=True)
        self.assertEqual(u["total"], 413 - 2)
        self.assertTrue(u["unused"])
        # The reported problem: more matches than the old limit must still work
        with mock.patch.object(wu, "SEARCH_ALL_MAX", 50):
            big = wu.search(".sldprt", limit=10, kind="part", unused=True)
        self.assertEqual((big["total"], big["unused"]), (411, True))
        self.assertEqual(wu.statistics()["unused_parts"], 411)            # the statistic says the same number

    def test_too_many_for_used_sorting(self):
        with mock.patch.object(wu, "SEARCH_ALL_MAX", 1000):
            r = wu.search("hinge", limit=50, sort="used", desc=True)
        self.assertIn("up to 1,000 results", r["note"])
        self.assertEqual((r["sort"], r["total"]), ("name", 1239))

    def test_exact_file_name(self):
        self.files.append({"path": "D:\\Vault\\X\\HINGE-0004.SLDASM", "modified": 5})      # same name, other folder
        loose = wu.search("HINGE-000", limit=50)
        exact = wu.search("HINGE-0004.SLDASM", limit=50, exact=True)
        self.assertGreater(loose["total"], 2)                              # HINGE-0001 ... HINGE-0009 and more
        self.assertEqual(sorted(x["path"] for x in exact["results"]),
                         ["D:\\Vault\\F4\\HINGE-0004.SLDASM", "D:\\Vault\\X\\HINGE-0004.SLDASM"])
        self.assertTrue(exact["exact"])
        with mock.patch.object(wu, "everything_status", lambda max_age=10: (False, "off")):
            self.ref("D:\\Vault\\A.SLDASM", "Default", "D:\\Vault\\P\\Bolt.SLDPRT")
            self.ref("D:\\Vault\\B.SLDASM", "Default", "D:\\Vault\\P\\Bolt-M8.SLDPRT")
            r = wu.search("bolt.sldprt", limit=50, exact=True)
        self.assertEqual([wu._fname(x["path"]) for x in r["results"]], ["Bolt.SLDPRT"])

    def test_without_everything_the_index_pages_too(self):
        self.patch("everything_status", lambda max_age=10: (False, "off"))
        for i in range(30):
            self.ref(f"D:\\Vault\\Asm{i:02d}.SLDASM", "Default", f"D:\\Vault\\P\\Part{i:02d}.SLDPRT")
        r = wu.search("vault", limit=10, offset=10, sort="name", kind="part")
        self.assertEqual(r["via"], "index")
        self.assertEqual(r["total"], 30)
        self.assertEqual([wu._fname(x["path"]) for x in r["results"]], [f"Part{i:02d}.SLDPRT" for i in range(10, 20)])
        self.assertEqual(r["counts"], {"": 60, "part": 30, "asm": 30, "drw": 0})
