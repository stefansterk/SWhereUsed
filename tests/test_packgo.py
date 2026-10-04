"""Pack & Go: copies under new names that refer to each other; the originals stay exactly as they were."""
import json
from unittest import mock
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import SWhereUsed as wu  # noqa: E402
from test_rename import RenameBase, FakeDoc, FakeProps  # noqa: E402


class PackGoTests(RenameBase):
    def setUp(self):
        super().setUp()
        self.vault = Path(self.part).parent
        self.target = self.dir / "copy" / "P26050"
        self.before = {f: Path(f).read_text(encoding="utf-8") for f in (self.part, self.drw, self.sub, self.top)}

    def items(self, prefix="NEW-", keep=()):
        return [{"path": p, "name": prefix + Path(p).stem, "copy": Path(p).name not in keep}
                for p in (self.top, self.sub, self.part)]

    def originals_untouched(self):
        for f, text in self.before.items():
            self.assertEqual(Path(f).read_text(encoding="utf-8"), text, f)

    def test_copy_everything_with_a_prefix(self):
        r = wu.packgo(self.top, self.items(), str(self.target), True, True, dry_run=False)
        self.assertTrue(r["result"]["ok"], r["result"])
        self.assertEqual({k: r["counts"][k] for k in ("copied", "drawings", "kept", "updated", "overwritten")},
                         {"copied": 3, "drawings": 1, "kept": 0, "updated": 3, "overwritten": 0})
        t = self.target
        self.assertEqual(self.refs(str(t / "NEW-Top.SLDASM")), [str(t / "NEW-Sub.SLDASM")])
        self.assertEqual(self.refs(str(t / "NEW-Sub.SLDASM"))[0], str(t / "NEW-Bolt.SLDPRT"))
        self.assertTrue(self.refs(str(t / "NEW-Sub.SLDASM"))[1].endswith("Nut.SLDPRT"))       # not copied: original
        self.assertEqual(self.refs(str(t / "NEW-Bolt.SLDDRW")), [str(t / "NEW-Bolt.SLDPRT")])  # the drawing came along
        self.originals_untouched()

    def test_keep_a_part_and_refer_to_the_original(self):
        r = wu.packgo(self.top, self.items(keep=("Bolt.SLDPRT",)), str(self.target), True, True, dry_run=False)
        self.assertTrue(r["result"]["ok"], r["result"])
        self.assertEqual(self.refs(str(self.target / "NEW-Sub.SLDASM"))[0], self.part)          # the original Bolt
        self.assertFalse((self.target / "NEW-Bolt.SLDDRW").exists())                            # not copied: no drawing
        self.assertEqual(r["counts"]["kept"], 1)

    def test_folder_structure_is_kept(self):
        sub_folder = self.vault / "parts"
        sub_folder.mkdir()
        washer = sub_folder / "Washer.SLDPRT"
        washer.write_text("part", encoding="utf-8")
        items = self.items() + [{"path": str(washer), "name": "NEW-Washer", "copy": True}]
        plan = wu.packgo_plan(self.top, items, str(self.target), keep_structure=True)
        news = {Path(c["new"]).name: c["new"] for c in plan["copies"]}
        self.assertEqual(news["NEW-Washer.SLDPRT"], str(self.target / "parts" / "NEW-Washer.SLDPRT"))
        self.assertEqual(news["NEW-Top.SLDASM"], str(self.target / "NEW-Top.SLDASM"))
        flat = wu.packgo_plan(self.top, items, str(self.target), keep_structure=False)
        self.assertIn(str(self.target / "NEW-Washer.SLDPRT"), [c["new"] for c in flat["copies"]])

    def test_failure_removes_the_copies_and_new_folders(self):
        FakeDoc.fail_save_on = "NEW-Sub.SLDASM"
        r = wu.packgo(self.top, self.items(), str(self.target), True, True, dry_run=False)
        self.assertFalse(r["result"]["ok"])
        self.assertTrue(r["result"]["rolled_back"], r["result"])
        self.assertFalse((self.dir / "copy").exists() and any((self.dir / "copy").rglob("*")))
        self.originals_untouched()

    def test_stops_before_doing_anything(self):
        self.target.mkdir(parents=True)
        (self.target / "NEW-Sub.SLDASM").write_text("someone else's file")
        p = wu.packgo(self.top, self.items(), str(self.target), dry_run=False)
        self.assertFalse(p["executed"])
        self.assertTrue(any("already exists" in b for b in p["blockers"]))
        self.assertEqual((self.target / "NEW-Sub.SLDASM").read_text(), "someone else's file")
        bad = wu.packgo_plan(self.top, [{"path": self.sub, "name": "A/B", "copy": True}], str(self.target))
        self.assertTrue(any("not a valid file name" in b for b in bad["blockers"]))
        same = wu.packgo_plan(self.top, [{"path": self.sub, "name": "X", "copy": True}, {"path": self.part, "name": "X.SLDASM", "copy": True}],
                              str(self.target))
        self.assertTrue(any("copy of two files" in b for b in same["blockers"]) or same["copies"])
        rel = wu.packgo_plan(self.top, self.items(), "D:Projects")
        self.assertIn("Did you mean D:\\Projects?", rel["blockers"][0])
        self.originals_untouched()

    def test_toolbox_is_recognised(self):
        self.assertTrue(wu.is_toolbox("C:\\\\SOLIDWORKS Data\\\\browser\\\\ansi\\\\bolt.sldprt"))
        self.assertTrue(wu.is_toolbox("\\\\\\\\srv\\\\cad\\\\Toolbox\\\\iso\\\\nut.sldprt"))
        self.assertFalse(wu.is_toolbox(self.part))


class PackGoDeepTests(PackGoTests):
    """A whole assembly, more levels: as reported, only the top went well."""

    def test_reference_from_a_deeper_level_is_left_to_its_own_file(self):
        # As if the external references of Top also listed Bolt (it is inside Sub, not directly in Top)
        Path(self.top).write_text(json.dumps([self.sub, self.part]), encoding="utf-8")
        self.before[self.top] = Path(self.top).read_text(encoding="utf-8")
        r = wu.packgo(self.top, self.items(), str(self.target), True, True, dry_run=False)
        self.assertTrue(r["result"]["ok"], r["result"])
        top = self.refs(str(self.target / "NEW-Top.SLDASM"))
        self.assertEqual(top[0], str(self.target / "NEW-Sub.SLDASM"))
        self.assertEqual(top[1], self.part)                              # not touched in Top: Sub's copy has it
        self.assertEqual(self.refs(str(self.target / "NEW-Sub.SLDASM"))[0], str(self.target / "NEW-Bolt.SLDPRT"))
        self.originals_untouched()

    def test_results_are_kept_until_the_job_ends(self):
        wu._COM_KEEP.clear()
        r = wu.packgo(self.top, self.items(), str(self.target), True, True, dry_run=False)
        self.assertTrue(r["result"]["ok"], r["result"])
        self.assertGreaterEqual(len(wu._COM_KEEP), 6)                  # documents and reference lists of every copy


class PackGoIndexTests(PackGoTests):
    """The copies are right in SOLIDWORKS, but their component lists still name the originals until SOLIDWORKS
    saves them. SWhereUsed must show the copies using the copies, and the originals using the originals."""

    def test_index_after_pack_and_go(self):
        r = wu.packgo(self.top, self.items(), str(self.target), True, True, dry_run=False)
        self.assertTrue(r["result"]["ok"], r["result"])
        t = self.target
        copy_of = {str(t / "NEW-Top.SLDASM"): self.top, str(t / "NEW-Sub.SLDASM"): self.sub,
                   str(t / "NEW-Bolt.SLDDRW"): self.drw}
        before = {f: json.loads(text) for f, text in self.before.items() if not f.endswith(".SLDPRT")}

        def read(p):                       # a copy's component list: still the one of its original
            refs = before[copy_of[p]] if p in copy_of else (json.loads(Path(p).read_text()) if p.endswith(("ASM", "DRW")) else [])
            return [(p.lower(), "Default", Path(x).name.lower(), x, "", 1, 0, 0) for x in refs], 1, None, 17000, {}
        files = [self.top, self.sub, self.part, self.drw] + [str(t / n) for n in ("NEW-Top.SLDASM", "NEW-Sub.SLDASM", "NEW-Bolt.SLDPRT", "NEW-Bolt.SLDDRW")]
        self.patch("read_document", read)
        self.patch("sw_problem", lambda: None)
        self.patch("index_source", lambda: ("everything", "test"))
        self.patch("_reachable", lambda root, timeout=10: True)
        self.patch("_com_init", lambda: None)
        self.patch("_probe_part_references", lambda parts: None)
        self.patch("everything_list", lambda q: [dict(type="file", path=p, modified=2, size=2) for p in files])
        wu.run_index()
        names = lambda p: sorted(Path(x["path"]).name for x in wu.whereused(p)["direct"])   # noqa: E731
        self.assertEqual(names(str(t / "NEW-Bolt.SLDPRT")), ["NEW-Bolt.SLDDRW", "NEW-Sub.SLDASM"])
        self.assertEqual(names(self.part), ["Bolt.SLDDRW", "Sub.SLDASM"])          # the originals: untouched
        self.assertEqual(names(str(t / "NEW-Sub.SLDASM")), ["NEW-Top.SLDASM"])
        tree = wu.structure(str(t / "NEW-Top.SLDASM"))["tree"]
        self.assertEqual(tree[0]["name"], "NEW-Sub.SLDASM")
        self.assertEqual(tree[0]["children"][0]["name"], "NEW-Bolt.SLDPRT")


class SameNameTests(PackGoTests):
    """As reported: Supplier\\9238051\\x.sldprt and Supplier\\9238101\\x.sldprt (same name, other files), each in its
    own subassembly, and the references still holding an older location of the vault."""

    def test_each_copy_points_to_its_own_files(self):
        v = Path(self.part).parent
        made = {}
        for prod in ("9238051", "9238101"):
            d = v / "Supplier" / prod
            d.mkdir(parents=True)
            clip = d / "_support_unit.sldprt"
            clip.write_text("part " + prod, encoding="utf-8")
            sub = d / f"{prod}.sldasm"
            old_location = f"D:\\Old share\\Work\\Supplier\\{prod}\\_support_unit.sldprt"
            sub.write_text(json.dumps([old_location]), encoding="utf-8")
            self.ref(str(sub), "Default", str(clip))
            self.ref(self.top, "Default", str(sub))
            made[prod] = (sub, clip)
        Path(self.top).write_text(json.dumps([self.sub] + [str(made[p][0]) for p in made]), encoding="utf-8")
        items = self.items() + [{"path": str(p), "name": "test_" + p.stem, "copy": True} for prod in made for p in made[prod]]
        r = wu.packgo(self.top, items, str(self.target), True, True, dry_run=False)
        self.assertTrue(r["result"]["ok"], r["result"])
        for prod in made:
            copy_sub = self.target / "Supplier" / prod / f"test_{prod}.sldasm"
            self.assertEqual(self.refs(str(copy_sub)), [str(self.target / "Supplier" / prod / "test__support_unit.sldprt")])

    def test_pick_copy(self):
        a = {"old": "D:\\\\New\\\\Supplier\\\\A\\\\x.sldprt", "new": "a"}
        b = {"old": "D:\\\\New\\\\Supplier\\\\B\\\\x.sldprt", "new": "b"}
        self.assertEqual(wu._pick_copy("Z:\\\\Old\\\\Supplier\\\\B\\\\x.sldprt", [a, b])["new"], "b")
        self.assertIsNone(wu._pick_copy("Z:\\\\Elsewhere\\\\x.sldprt", [a, b]))        # not certain: no guess
        self.assertEqual(wu._pick_copy("Z:\\\\Anything\\\\x.sldprt", [a])["new"], "a")  # only one with that name


class SameNameOwnFolderTests(PackGoTests):
    """As in the second report: 9238101.sldasm holds an old path to 9238051\\x.sldprt, which no longer exists;
    SOLIDWORKS then takes x.sldprt from the assembly's own folder (9238101). The copy must do the same."""

    def test_own_folder_first_when_the_saved_path_is_gone(self):
        v = Path(self.part).parent
        subs = {}
        for prod in ("9238051", "9238101"):
            d = v / "Supplier" / prod
            d.mkdir(parents=True)
            (d / "_support_unit.sldprt").write_text("part " + prod, encoding="utf-8")
            subs[prod] = d / f"{prod}.sldasm"
        old = "D:\\\\Old Cloud\\\\Work\\\\Supplier\\\\9238051\\\\_support_unit.sldprt"   # both point there
        for prod, sub in subs.items():
            sub.write_text(json.dumps([old]), encoding="utf-8")
            self.ref(str(sub), "Default", old)
            self.ref(self.top, "Default", str(sub))
        Path(self.top).write_text(json.dumps([self.sub] + [str(s) for s in subs.values()]), encoding="utf-8")
        items = self.items() + [{"path": str(p), "name": "test_" + p.stem, "copy": True}
                                for prod in subs for p in (subs[prod], subs[prod].parent / "_support_unit.sldprt")]
        r = wu.packgo(self.top, items, str(self.target), True, True, dry_run=False)
        self.assertTrue(r["result"]["ok"], r["result"])
        for prod in subs:
            copy_sub = self.target / "Supplier" / prod / f"test_{prod}.sldasm"
            self.assertEqual(self.refs(str(copy_sub)), [str(self.target / "Supplier" / prod / "test__support_unit.sldprt")], prod)


class PackGoChoicesTests(PackGoTests):
    def same_name_pair(self):
        v = Path(self.part).parent
        out = []
        for prod in ("9238051", "9238101"):
            d = v / "Supplier" / prod
            d.mkdir(parents=True)
            f = d / "_activator.sldprt"
            f.write_text("part " + prod, encoding="utf-8")
            out.append(f)
        return out

    def test_conflicts_red_in_one_folder_orange_with_folders(self):
        a, b = self.same_name_pair()
        items = self.items() + [{"path": str(p), "name": "test__activator", "copy": True} for p in (a, b)]
        flat = wu.packgo_plan(self.top, items, str(self.target), keep_structure=False)
        self.assertEqual({flat["conflicts"][str(p).lower()]["level"] for p in (a, b)}, {"error"})
        self.assertTrue(flat["blockers"])
        kept = wu.packgo_plan(self.top, items, str(self.target), keep_structure=True)
        self.assertEqual({kept["conflicts"][str(p).lower()]["level"] for p in (a, b)}, {"warn"})
        self.assertEqual(kept["blockers"], [])                          # allowed, but marked
        unique = [dict(i) for i in items]
        unique[-1]["name"] = "test__activator_9238101"                  # what "Make names unique" does
        ok = wu.packgo_plan(self.top, unique, str(self.target), keep_structure=False)
        self.assertEqual((ok["blockers"], ok["conflicts"]), ([], {}))

    def test_existing_file_is_marked_red(self):
        self.target.mkdir(parents=True)
        (self.target / "NEW-Sub.SLDASM").write_text("x")
        p = wu.packgo_plan(self.top, self.items(), str(self.target))
        self.assertEqual(p["conflicts"][self.sub.lower()]["level"], "error")

    def test_a_folder_of_its_own_and_its_drawing_goes_along(self):
        own = self.dir / "library" / "bolts"
        items = self.items()
        items[2]["folder"] = str(own)                                   # Bolt to its own folder
        r = wu.packgo(self.top, items, str(self.target), True, True, dry_run=False)
        self.assertTrue(r["result"]["ok"], r["result"])
        self.assertTrue((own / "NEW-Bolt.SLDPRT").exists() and (own / "NEW-Bolt.SLDDRW").exists())
        self.assertEqual(self.refs(str(self.target / "NEW-Sub.SLDASM"))[0], str(own / "NEW-Bolt.SLDPRT"))
        bad = wu.packgo_plan(self.top, [dict(items[0]), dict(items[1], folder="relative\\\\path")], str(self.target))
        self.assertTrue(any("not a full path" in b for b in bad["blockers"]))

    def test_without_the_assembly_itself(self):
        items = self.items()
        items[0]["copy"] = False                                        # Top stays out
        r = wu.packgo(self.top, items, str(self.target), True, True, dry_run=False)
        self.assertTrue(r["result"]["ok"], r["result"])
        self.assertFalse((self.target / "NEW-Top.SLDASM").exists())
        self.assertEqual(self.refs(str(self.target / "NEW-Sub.SLDASM"))[0], str(self.target / "NEW-Bolt.SLDPRT"))
        self.originals_untouched()
        nothing = wu.packgo_plan(self.top, [dict(i, copy=False) for i in items], str(self.target))
        self.assertIn("nothing is chosen to be copied", nothing["blockers"])


class OverwriteTests(PackGoTests):
    def existing(self):
        self.target.mkdir(parents=True)
        f = self.target / "NEW-Bolt.SLDPRT"
        f.write_text("the file that was there", encoding="utf-8")
        return f

    def test_off_by_default(self):
        f = self.existing()
        r = wu.packgo(self.top, self.items(), str(self.target), dry_run=False)
        self.assertFalse(r["executed"])
        self.assertEqual(f.read_text(encoding="utf-8"), "the file that was there")
        self.assertEqual(r["conflicts"][self.part.lower()]["level"], "error")

    def test_overwrite_with_a_backup(self):
        f = self.existing()
        plan = wu.packgo_plan(self.top, self.items(), str(self.target), overwrite=True)
        self.assertEqual((plan["blockers"], plan["counts"]["overwritten"]), ([], 1))
        self.assertEqual(plan["conflicts"][self.part.lower()]["level"], "warn")
        r = wu.packgo(self.top, self.items(), str(self.target), dry_run=False, overwrite=True)
        self.assertTrue(r["result"]["ok"], r["result"])
        self.assertEqual(f.read_text(encoding="utf-8"), "part")                 # the copy of Bolt
        backups = [b for b in (self.dir / "data" / "backups").rglob("*NEW-Bolt.SLDPRT")]
        self.assertEqual([b.read_text(encoding="utf-8") for b in backups], ["the file that was there"])

    def test_failure_puts_the_overwritten_file_back(self):
        f = self.existing()
        FakeDoc.fail_save_on = "NEW-Top.SLDASM"
        r = wu.packgo(self.top, self.items(), str(self.target), dry_run=False, overwrite=True)
        self.assertFalse(r["result"]["ok"])
        self.assertTrue(r["result"]["rolled_back"], r["result"])
        self.assertEqual(f.read_text(encoding="utf-8"), "the file that was there")
        self.assertEqual(sorted(x.name for x in self.target.iterdir()), ["NEW-Bolt.SLDPRT"])
        self.originals_untouched()

    def test_never_an_open_file_or_an_original_being_copied(self):
        f = self.existing()
        self.patch("access_info", lambda p: {"exists": True, "in_use": True, "locked_by": "jan / PC-12"})
        self.patch("blocking_reason", lambda acc: "open in SOLIDWORKS by jan / PC-12")
        p = wu.packgo_plan(self.top, self.items(), str(self.target), overwrite=True)
        self.assertTrue(any("open in SOLIDWORKS" in b for b in p["blockers"]), p["blockers"])
        same = [{"path": x, "name": Path(x).stem, "copy": True} for x in (self.top, self.sub, self.part)]
        p = wu.packgo_plan(self.top, same, str(Path(self.part).parent), keep_structure=False, overwrite=True)
        self.assertTrue(any("itself one of the files being copied" in b for b in p["blockers"]), p["blockers"])
        self.originals_untouched()


class PropertyRuleTests(PackGoTests):
    def setUp(self):
        super().setUp()
        self.patch("_best", lambda obj, base: obj)          # no Document Manager here to cast COM interfaces

    def seed(self, path, file_props, config_props):
        Path(path + ".props.json").write_text(json.dumps({"file": file_props, "configs": {"Default": dict(config_props), "Long": dict(config_props)}}))

    def run_rules(self, rules, scope="both"):
        import shutil as sh
        real = sh.copy2

        def copy2(a, b):                                   # the fake keeps properties in a file next to it
            real(a, b)
            if Path(str(a) + ".props.json").exists():
                real(str(a) + ".props.json", str(b) + ".props.json")
        with mock.patch.object(wu.shutil, "copy2", copy2):
            return wu.packgo(self.top, self.items(), str(self.target), True, True, dry_run=False,
                             props_rules=rules, props_scope=scope)

    def props(self, name):
        return json.loads((self.target / (name + ".props.json")).read_text())

    def test_clear_delete_set_replace(self):
        self.seed(self.part, {"Revision": "C", "Approved": "jan", "Project": "P25001 frame", "PartNo": "B-1"},
                  {"Revision": "C", "Project": "P25001"})
        r = self.run_rules([{"action": "clear", "name": "revision"},          # any case
                            {"action": "delete", "name": "Approved"},
                            {"action": "replace", "name": "Project", "find": "P25001", "value": "P26050"},
                            {"action": "set", "name": "Copied from", "value": "{oldname} on {date}"},
                            {"action": "set", "name": "PartNo", "value": "{name}"}])
        self.assertTrue(r["result"]["ok"], r["result"])
        p = self.props("NEW-Bolt.SLDPRT")
        self.assertEqual(p["file"]["Revision"], "")
        self.assertNotIn("Approved", p["file"])
        self.assertEqual(p["file"]["Project"], "P26050 frame")
        self.assertTrue(p["file"]["Copied from"].startswith("Bolt on 20"))
        self.assertEqual(p["file"]["PartNo"], "NEW-Bolt")
        self.assertEqual(p["configs"]["Long"]["Project"], "P26050")             # configurations too
        self.assertEqual(json.loads(Path(self.part + ".props.json").read_text())["file"]["Approved"], "jan")   # original untouched

    def test_scope_file_only(self):
        self.seed(self.part, {"Revision": "C"}, {"Revision": "C"})
        r = self.run_rules([{"action": "clear", "name": "Revision"}], scope="file")
        self.assertTrue(r["result"]["ok"], r["result"])
        p = self.props("NEW-Bolt.SLDPRT")
        self.assertEqual((p["file"]["Revision"], p["configs"]["Default"]["Revision"]), ("", "C"))

    def test_a_failed_check_removes_the_copies(self):
        self.seed(self.part, {"Revision": "C"}, {})
        with mock.patch.object(FakeProps, "SetCustomProperty", lambda self_, n, v: False):   # silently ignored
            r = self.run_rules([{"action": "clear", "name": "Revision"}])
        self.assertFalse(r["result"]["ok"])
        self.assertIn("Revision", r["result"]["error"])
        self.assertTrue(r["result"]["rolled_back"])
        self.assertFalse((self.target / "NEW-Bolt.SLDPRT").exists())

    def test_incomplete_rules_stop_before_anything(self):
        p = wu.packgo_plan(self.top, self.items(), str(self.target), props_rules=[{"action": "replace", "name": "Project"},
                                                                                {"action": "", "name": "X"}])
        self.assertEqual(len(p["blockers"]), 2)
        empty = wu.packgo_plan(self.top, self.items(), str(self.target), props_rules=[{"action": "clear", "name": ""}])
        self.assertEqual(empty["blockers"], [])                            # an empty row is ignored


class PropsOverviewTests(PackGoTests):
    def setUp(self):
        super().setUp()
        self.patch("_best", lambda obj, base: obj)

    def test_one_file_file_and_configurations(self):
        Path(self.part + ".props.json").write_text(json.dumps({"file": {"Revision": "C", "PartNo": "B-1"},
                                                               "configs": {"Default": {"Revision": "C"}, "Long": {"Length": "200"}}}))
        got = wu._props_of_file(self.part)
        self.assertEqual(got["file"], {"Revision": "C", "PartNo": "B-1"})
        self.assertEqual(got["configs"]["Long"], {"Length": "200"})

    def test_overview_over_the_files(self):
        data = {self.part: {"file": {"Revision": "C", "PartNo": "B-1"}, "configs": {"Default": {"Revision": "C"}}},
                self.sub: {"file": {"revision": "A", "Project": "P25001"}, "configs": {}},
                self.top: {"error": "could not open"}}
        self.patch("read_props_isolated", lambda paths, timeout=300: {p: data[p] for p in paths})
        o = wu.props_overview([self.part, self.sub, self.top])
        rev = next(x for x in o["properties"] if x["name"].lower() == "revision")
        self.assertEqual((rev["files"], rev["file"], rev["configs"], sorted(rev["values"])), (2, 2, 1, ["A", "C"]))
        self.assertEqual(o["properties"][0]["name"].lower(), "revision")                 # most files first
        self.assertEqual([f["path"] for f in o["failed"]], [self.top])


class PropsFilesTests(PackGoTests):
    def test_all_files_and_their_drawings_once(self):
        asked = []
        self.patch("read_props_isolated", lambda paths, timeout=300: asked.extend(paths) or {p: {"file": {}, "configs": {}} for p in paths})
        got = wu.props_files([self.top, self.sub, self.part])
        self.assertEqual(sorted(Path(p).name for p in got["files"]), ["Bolt.SLDDRW", "Bolt.SLDPRT", "Sub.SLDASM", "Top.SLDASM"])
        self.assertEqual(len(asked), 4)                                    # one read, the drawing included


class RuleForOneKindTests(PropertyRuleTests):
    def test_only_drawings(self):
        self.seed(self.part, {"Checked by": "jan", "Project": "P1"}, {})
        Path(self.drw + ".props.json").write_text(json.dumps({"file": {"Checked by": "jan", "Project": "P1"}, "configs": {}}))
        r = self.run_rules([{"action": "clear", "name": "Checked by", "only": "drw"},
                            {"action": "set", "name": "Project", "value": "P2", "only": "nonsense"}])   # unknown: all files
        self.assertTrue(r["result"]["ok"], r["result"])
        drw, prt = self.props("NEW-Bolt.SLDDRW"), self.props("NEW-Bolt.SLDPRT")
        self.assertEqual((drw["file"]["Checked by"], prt["file"]["Checked by"]), ("", "jan"))
        self.assertEqual((drw["file"]["Project"], prt["file"]["Project"]), ("P2", "P2"))


class RuleForSeveralKindsTests(PropertyRuleTests):
    def test_parts_and_assemblies_not_drawings(self):
        self.seed(self.part, {"Supplier": "Supplier"}, {})
        Path(self.drw + ".props.json").write_text(json.dumps({"file": {"Supplier": "Supplier"}, "configs": {}}))
        r = self.run_rules([{"action": "clear", "name": "Supplier", "only": ["prt", "asm"]}])
        self.assertTrue(r["result"]["ok"], r["result"])
        self.assertEqual((self.props("NEW-Bolt.SLDPRT")["file"]["Supplier"], self.props("NEW-Bolt.SLDDRW")["file"]["Supplier"]), ("", "Supplier"))

    def test_kinds_are_cleaned(self):
        ok, _bad = wu._clean_prop_rules([{"action": "clear", "name": "A", "only": ["drw", "prt", "asm"]},     # all three: all
                                         {"action": "clear", "name": "B", "only": "drw"},                     # saved before
                                         {"action": "clear", "name": "C", "only": ["xyz", "asm"]}])
        self.assertEqual([r["only"] for r in ok], [[], ["drw"], ["asm"]])


class KeptFileTests(PackGoTests):
    """As reported: the assembly names its parts by an OLD path that no longer exists; SOLIDWORKS finds them in the
    assembly's own folder. A part set to Keep must then be referred to by its REAL path in the copy, which is in
    another folder: otherwise the copy cannot find it (SWhereUsed said 'not found', 'not indexed')."""

    def test_kept_part_gets_its_real_path(self):
        old = "X:\\\\Old cloud\\\\Work\\\\Bolt.SLDPRT"                  # gone
        Path(self.sub).write_text(json.dumps([old]), encoding="utf-8")
        self.before[self.sub] = Path(self.sub).read_text(encoding="utf-8")
        db = wu.conn()
        db.execute("UPDATE refs SET child_stored_path=? WHERE parent=?", (old, self.sub.lower()))
        db.commit()
        items = [{"path": self.top, "name": "NEW-Top", "copy": True}, {"path": self.sub, "name": "NEW-Sub", "copy": True},
                 {"path": self.part, "name": "Bolt", "copy": False}]                    # Bolt: Keep
        r = wu.packgo(self.top, items, str(self.target), True, True, dry_run=False)
        self.assertTrue(r["result"]["ok"], r["result"])
        self.assertEqual(self.refs(str(self.target / "NEW-Sub.SLDASM")), [self.part])  # the real, existing path
        self.originals_untouched()

    def test_the_index_knows_it_too(self):
        old = "X:\\\\Old cloud\\\\Work\\\\Bolt.SLDPRT"
        Path(self.sub).write_text(json.dumps([old]), encoding="utf-8")
        self.before[self.sub] = Path(self.sub).read_text(encoding="utf-8")
        items = [{"path": self.top, "name": "NEW-Top", "copy": True}, {"path": self.sub, "name": "NEW-Sub", "copy": True},
                 {"path": self.part, "name": "Bolt", "copy": False}]
        wu.packgo(self.top, items, str(self.target), True, True, dry_run=False)
        rows = wu._apply_copies_to_rows([(str(self.target / "NEW-Sub.SLDASM").lower(), "Default", "bolt.sldprt", old, "", 1, 0, 0)])
        self.assertEqual(rows[0][3], self.part)              # the copy's old component list: read as the real file


class SameNameKeptTests(PackGoTests):
    """As reported: two parts with the same name in different folders; one copied (with a prefix), the other set to
    Keep. An assembly that uses the KEPT one must keep pointing to it, not to the copy of the other one."""

    def test_kept_same_name_file_is_not_swapped_for_the_copy(self):
        other = Path(self.part).parent / "Storage"
        other.mkdir()
        twin = other / "Bolt.SLDPRT"                                  # same name, another folder
        twin.write_text("part (storage)", encoding="utf-8")
        Path(self.sub).write_text(json.dumps([str(twin)]), encoding="utf-8")   # Sub uses the Storage one
        self.before[self.sub] = Path(self.sub).read_text(encoding="utf-8")
        db = wu.conn()
        db.execute("UPDATE refs SET child_stored_path=? WHERE parent=? AND child_name='bolt.sldprt'", (str(twin), self.sub.lower()))
        db.commit()
        items = [{"path": self.top, "name": "P26-Top", "copy": True}, {"path": self.sub, "name": "P26-Sub", "copy": True},
                 {"path": self.part, "name": "P26-Bolt", "copy": True},              # the vault one: copied, prefixed
                 {"path": str(twin), "name": "P26-Bolt", "copy": False}]             # the Storage one: Keep
        r = wu.packgo(self.top, items, str(self.target), False, True, dry_run=False)
        self.assertTrue(r["result"]["ok"], r["result"])
        self.assertEqual(self.refs(str(self.target / "P26-Sub.SLDASM"))[0], str(twin))      # still the kept file


class JobProgressTests(PackGoTests):
    def test_progress_from_the_journal(self):
        self.assertEqual(wu.job_progress(), {"running": False})
        journal = self.dir / "j.json"
        journal.write_text(json.dumps({"created": ["a", "b", "c", "d"], "updated_n": 1}), encoding="utf-8")
        job = {"kind": "packgo", "journal": str(journal), "copies": [{}] * 4, "update": ["x", "y"], "props_rules": [{"a": 1}]}
        with mock.patch.dict(wu._job_now, {"job": job, "started": 0.0}):
            p = wu.job_progress()
        self.assertEqual((p["done"], p["total"], p["phase"]), (5, 10, "Pointing the copies to each other"))   # 4 + 1 of 4 + 2 + 4
