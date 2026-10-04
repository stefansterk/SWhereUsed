"""Diagnostics, settings in the app, update check, broken references, custom properties, drawings per assembly,
thumbnails and moving several files at once."""
import io
import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import SWhereUsed as wu  # noqa: E402
from test_whereused import Base  # noqa: E402
from test_rename import RenameBase  # noqa: E402

V = "G:\\V\\"


class DiagnosticsAndSettingsTests(Base):
    def test_diagnostics_never_contain_secrets(self):
        self.patch("SW_KEY", "SECRET-KEY-123")
        self.patch("EVERYTHING_PASS", "hunter2")
        self.patch("everything_status", lambda max_age=10: (False, "off"))
        text = wu.diagnostics()
        self.assertIn(f"SWhereUsed {wu.APP_VERSION}", text)
        self.assertIn("License key: present", text)
        self.assertNotIn("SECRET-KEY-123", text)
        self.assertNotIn("hunter2", text)
        self.assertIn("[index] workers =", text)

    def test_change_a_setting(self):
        wu.load_settings()
        with mock.patch.object(wu, "WORKERS", 4), mock.patch.object(wu, "EXCLUDE", ["a"]):
            wu.change_setting("index", "workers", "6")
            self.assertEqual(wu.WORKERS, 6)
            wu.change_setting("index", "exclude", "\\\\Old\\\\; \\\\Archive\\\\")
            self.assertEqual(wu.EXCLUDE, ["\\\\Old\\\\", "\\\\Archive\\\\"])
            for sec, key, bad in (("index", "workers", "20"), ("index", "workers", "many"), ("app", "port", "80"),
                                  ("index", "source", "sometimes"), ("nope", "x", "1")):
                with self.assertRaises(ValueError, msg=(key, bad)):
                    wu.change_setting(sec, key, bad)
            self.assertEqual(wu.WORKERS, 6)
        text = (self.dir / "data" / "settings.ini").read_text(encoding="utf-8")
        self.assertIn("workers = 6", text)
        items = {(x["section"], x["key"]): x for x in wu.settings_list()}
        self.assertTrue(items[("everything", "password")]["secret"])
        self.assertEqual(items[("everything", "password")]["value"], "")
        self.assertTrue(items[("app", "port")]["restart"])


class UpdateTests(Base):
    def release(self, tag):
        body = json.dumps({"tag_name": tag, "html_url": "https://github.com/me/whereused/releases/tag/" + tag}).encode()
        return mock.MagicMock(__enter__=lambda s: io.BytesIO(body), __exit__=lambda *a: False)

    def test_newer_older_and_off(self):
        with mock.patch.object(wu, "UPDATE_REPO", "me/whereused"), mock.patch.object(wu, "APP_VERSION", "1.15.0"):
            with mock.patch.object(wu.urllib.request, "urlopen", lambda *a, **k: self.release("v1.16.2")):
                self.assertEqual(wu.check_for_update(force=True)["latest"], "1.16.2")
                self.assertEqual(wu._meta_update()["latest"], "1.16.2")
            with mock.patch.object(wu.urllib.request, "urlopen", lambda *a, **k: self.release("v1.9.9")):
                self.assertIsNone(wu.check_for_update(force=True))          # 1.9.9 is older than 1.15.0
            with mock.patch.object(wu.urllib.request, "urlopen", mock.Mock(side_effect=OSError("offline"))):
                self.assertIsNone(wu.check_for_update(force=True))
        self.assertIsNone(wu.check_for_update(force=True))                  # no repository set: nothing


class BrokenReferenceTests(Base):
    def test_only_what_solidworks_will_not_find(self):
        self.patch("_root_reachable_cached", lambda *a, **k: True)
        self.patch("_existing_files", lambda paths: {V.lower() + "bolt.sldprt"})
        db = wu.conn()
        db.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?)", ((V + "Nut.SLDPRT").lower(), V + "Nut.SLDPRT", 1, 1, 1, "ok", None, 1, None))
        db.commit()
        self.ref(V + "A.SLDASM", "Default", V + "Bolt.SLDPRT")              # exists
        self.ref(V + "A.SLDASM", "Default", "X:\\Old\\Nut.SLDPRT")          # gone, but found by name (Nut is indexed)
        self.ref(V + "A.SLDASM", "Default", "X:\\Old\\Gone.SLDPRT")         # gone, nowhere
        self.ref(V + "B.SLDASM", "Default", "X:\\Old\\Gone.SLDPRT")
        self.ref(V + "B.SLDASM", "Default", "C:\\x\\swx1\\IC~~\\Virtual^B.SLDPRT", virt=1)
        b = wu.broken_references()
        self.assertEqual((b["files"], b["parents"]), (1, 2))
        self.assertEqual(b["items"][0]["name"], "Gone.SLDPRT")
        self.assertEqual(sorted(wu._fname(p) for p in b["items"][0]["parents"]), ["A.SLDASM", "B.SLDASM"])
        self.assertEqual(wu.statistics()["broken"]["files"], 1)


class PropsAndDrawingsTests(Base):
    def setUp(self):
        super().setUp()
        self.files = {V + "Top.SLDASM": (1, 1), V + "Bolt.SLDPRT": (1, 1), V + "Top.SLDDRW": (1, 1)}
        self.reads = []
        self.patch("sw_problem", lambda: None)
        self.patch("index_source", lambda: ("everything", "test"))
        self.patch("_reachable", lambda root, timeout=10: True)
        self.patch("_com_init", lambda: None)
        self.patch("_probe_part_references", lambda parts: None)
        self.patch("everything_list", lambda q: [dict(type="file", path=p, modified=m, size=s) for p, (m, s) in self.files.items()])

        def read(p):
            self.reads.append(wu._fname(p))
            if p.endswith(".SLDASM"):
                return [(p.lower(), "Default", "bolt.sldprt", V + "Bolt.SLDPRT", "", 4, 0, 0)], 1, "Top", 17000, {"PartNo": "A-100"}
            if p.endswith(".SLDPRT"):
                return [], 1, "Bolt", 17000, {"PartNo": "B-200", "Material": "Steel"}
            return [(p.lower(), "", "top.sldasm", V + "Top.SLDASM", "", 1, 0, 0)], 1, None, 17000
        self.patch("read_document", read)
        self.patch("_existing_files", lambda paths: {p.lower() for p in paths})

    def test_properties_in_the_structure_and_read_again_when_the_list_changes(self):
        with mock.patch.object(wu, "EXTRA_PROPS", ["PartNo", "Material"]):
            wu.run_index()
            flat = {f["name"]: f for f in wu.structure(V + "Top.SLDASM")["flat"]}
            self.assertEqual(flat["Bolt.SLDPRT"]["props"], {"PartNo": "B-200", "Material": "Steel"})
            self.assertEqual(wu.structure(V + "Top.SLDASM")["prop_names"], ["PartNo", "Material"])
            self.reads.clear()
            wu.run_index()
            self.assertEqual(self.reads, [])                                  # nothing changed: nothing read
        with mock.patch.object(wu, "EXTRA_PROPS", ["PartNo"]):
            wu.run_index()
            self.assertEqual(sorted(self.reads), ["Bolt.SLDPRT", "Top.SLDASM"])   # once more, not the drawing

    def test_drawing_per_file(self):
        self.files[V + "Nut.SLDPRT"] = (1, 1)
        wu.run_index()
        self.ref(V + "Top.SLDASM", "Default", V + "Nut.SLDPRT")
        flat = {f["name"]: f for f in wu.structure(V + "Top.SLDASM")["flat"]}
        self.assertEqual((flat["Bolt.SLDPRT"]["has_drawing"], flat["Nut.SLDPRT"]["has_drawing"]), (False, False))
        self.files[V + "Bolt.SLDDRW"] = (1, 1)
        wu.run_index()
        flat = {f["name"]: f for f in wu.structure(V + "Top.SLDASM")["flat"]}
        self.assertTrue(flat["Bolt.SLDPRT"]["has_drawing"])


class ThumbnailTests(Base):
    def setUp(self):
        super().setUp()
        self.patch("sw_problem", lambda: None)
        self.part = self.dir / "Bolt.SLDPRT"
        self.part.write_bytes(b"x")
        self.asked = []

        def helper(path, target, none, timeout=20):
            self.asked.append(path)
            if "Plain" in path:
                none.write_bytes(b"")
            else:
                target.write_bytes(wu.PNG_SIG + b"picture")
        self.patch("_ask_thumb_helper", helper)

    def test_made_once_per_file_version(self):
        self.assertTrue(wu.thumbnail(str(self.part)).startswith(wu.PNG_SIG))
        wu.thumbnail(str(self.part))
        self.assertEqual(len(self.asked), 1)                                  # the second time from disk
        os.utime(self.part, (time.time() + 60, time.time() + 60))             # saved again
        wu.thumbnail(str(self.part))
        self.assertEqual(len(self.asked), 2)
        self.assertEqual(len(list((self.dir / "data" / "thumbs").glob("*.png"))), 1)   # the old picture is gone

    def test_no_picture_is_remembered_too(self):
        plain = self.dir / "Plain.SLDPRT"
        plain.write_bytes(b"x")
        self.assertIsNone(wu.thumbnail(str(plain)))
        self.assertIsNone(wu.thumbnail(str(plain)))
        self.assertEqual(len(self.asked), 1)

    def test_off_or_not_a_solidworks_file(self):
        with mock.patch.object(wu, "THUMBNAILS", "no"):
            self.assertIsNone(wu.thumbnail(str(self.part)))
        other = self.dir / "notes.txt"
        other.write_text("x")
        self.assertIsNone(wu.thumbnail(str(other)))
        self.assertEqual(self.asked, [])


class MoveTests(RenameBase):
    def test_move_several_files_to_another_folder(self):
        target = str(Path(self.part).parent / "new")
        r = wu.rename_batch([{"path": self.part, "name": "Bolt", "folder": target},
                             {"path": self.sub, "name": "Sub", "folder": target}], True, False)
        self.assertTrue(r["result"]["ok"], r["result"])
        moved_part, moved_sub = str(Path(target) / "Bolt.SLDPRT"), str(Path(target) / "Sub.SLDASM")
        self.assertTrue(Path(moved_part).exists() and Path(moved_sub).exists())
        self.assertTrue(Path(target, "Bolt.SLDDRW").exists())                 # the drawing went along
        self.assertEqual(self.refs(moved_sub)[0], moved_part)
        self.assertEqual(self.refs(self.top), [moved_sub])

    def test_unchanged_folder_and_name_is_skipped(self):
        r = wu.rename_batch([{"path": self.part, "name": "Bolt", "folder": str(Path(self.part).parent)}], True, True)
        self.assertEqual(r["counts"]["renamed"], 0)
