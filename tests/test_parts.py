"""Parts in the index: parts that refer to other parts (derived, mirrored, inserted), read safely."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import SWhereUsed as wu  # noqa: E402
from test_whereused import Base  # noqa: E402
from test_rename import RenameBase, FakeDoc  # noqa: E402


class PartReadingTests(Base):
    """The Document Manager side, simulated: different interface versions answer differently."""

    def setUp(self):
        super().setUp()
        self.opt = SimpleNamespace(ExternalReferences=("G:\\V\\Base.SLDPRT", "G:\\V\\Base.SLDPRT", "G:\\V\\Other.SLDPRT"),
                                   ReferencedConfigurations=("Long", "Long", ""))
        app = SimpleNamespace(GetExternalReferenceOptionObject2=lambda: self.opt, GetSearchOptionObject=lambda: "search")
        self.patch("_sw_app", lambda: app)
        self.patch("_best", lambda obj, base: obj)

    def test_newest_method_missing_older_one_returns_count_and_object(self):
        def old_style(opt):
            return (3, opt)                               # comtypes: [in, out] parameter comes back with the count
        doc = SimpleNamespace(GetExternalFeatureReferences2=mock.Mock(side_effect=OSError("E_NOTIMPL")),
                              GetExternalFeatureReferences=old_style)
        refs, method = wu._part_references(doc)
        self.assertEqual(method, "GetExternalFeatureReferences")
        self.assertEqual(refs, [("G:\\V\\Base.SLDPRT", "Long"), ("G:\\V\\Base.SLDPRT", "Long"), ("G:\\V\\Other.SLDPRT", "")])
        self.assertEqual(self.opt.SearchOption, "search")

    def test_read_part_rows_and_description(self):
        doc = SimpleNamespace(GetExternalFeatureReferences3=lambda opt: 3, CloseDoc=lambda: None,
                              ConfigurationManager=SimpleNamespace(GetActiveConfigurationName=lambda: "A",
                                                                   GetConfigurationByName=lambda n: "cfg"))
        self.patch("_open_sw_retry", lambda p: doc)
        self.patch("_description", lambda *objs: "Mirrored bracket")
        rows, n, desc = wu.read_part("G:\\V\\Mirror.SLDPRT", with_refs=True)[:3]
        self.assertEqual(desc, "Mirrored bracket")
        self.assertEqual(sorted((r[2], r[4]) for r in rows), [("base.sldprt", "Long"), ("other.sldprt", "")])  # once each
        self.assertTrue(all(r[0] == "g:\\v\\mirror.sldprt" and r[1] == "" for r in rows))

    def test_description_only_when_references_are_switched_off(self):
        doc = SimpleNamespace(GetExternalFeatureReferences3=mock.Mock(), CloseDoc=lambda: None, ConfigurationManager=None)
        self.patch("_open_sw_retry", lambda p: doc)
        self.patch("_description", lambda *objs: "Plate")
        with mock.patch.dict(wu.PART_REFS, {"enabled": False, "why": "crashed"}):
            rows, _n, desc = wu.read_part("G:\\V\\Plate.SLDPRT")[:3]
        self.assertEqual((rows, desc), ([], "Plate"))
        doc.GetExternalFeatureReferences3.assert_not_called()


class ProbeTests(Base):
    def test_crash_switches_part_references_off_and_is_remembered(self):
        runs = []
        self.patch("find_swdm_dll", lambda: __file__)
        with mock.patch.object(wu.subprocess, "run", lambda *a, **k: runs.append(a) or SimpleNamespace(returncode=3221225477)), \
             mock.patch.dict(wu.PART_REFS, {"enabled": True, "why": None}):
            wu._probe_part_references(["G:\\V\\A.SLDPRT", "G:\\V\\B.SLDPRT"])
            self.assertFalse(wu.PART_REFS["enabled"])
            self.assertIn("crashed", wu.PART_REFS["why"])
            wu.PART_REFS.update(enabled=True)
            wu._probe_part_references(["G:\\V\\A.SLDPRT"])      # same DLL: not tried again
            self.assertFalse(wu.PART_REFS["enabled"])
        self.assertEqual(len(runs), 1)

    def test_success(self):
        self.patch("find_swdm_dll", lambda: __file__)
        with mock.patch.object(wu.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0)), \
             mock.patch.dict(wu.PART_REFS, {"enabled": True, "why": None}):
            wu._probe_part_references(["G:\\V\\A.SLDPRT"])
            self.assertTrue(wu.PART_REFS["enabled"])

    def test_everything_query_gets_parts(self):
        with mock.patch.object(wu, "EVERYTHING_QUERY", 'ext:sldasm;slddrw "D:\\Vault\\"'):
            self.assertTrue(wu._everything_query().startswith('ext:sldasm;slddrw;sldprt "D:\\Vault\\"'))
            with mock.patch.object(wu, "INCLUDE_PARTS", "no"):
                self.assertTrue(wu._everything_query().startswith('ext:sldasm;slddrw "D:\\Vault\\"'))


class DerivedPartWhereUsedTests(Base):
    def test_derived_part_is_listed_but_not_climbed(self):
        base, mirror, asm, top = "G:\\V\\Base.SLDPRT", "G:\\V\\Mirror.SLDPRT", "G:\\V\\Asm.SLDASM", "G:\\V\\Top.SLDASM"
        self.ref(mirror, "", base, ccfg="Long")               # the mirrored part refers to its base part
        self.ref(asm, "Default", base, qty=2)
        self.ref(asm, "Default", mirror, qty=2)
        self.ref(top, "Default", asm, qty=3, ccfg="Default")
        with mock.patch.object(wu, "_root_reachable_cached", lambda *a, **k: True):
            d = wu.whereused(base)
        self.assertEqual([(wu._fname(x["path"]), x["kind"]) for x in d["direct"]],
                         [("Asm.SLDASM", "assembly"), ("Mirror.SLDPRT", "part")])
        self.assertEqual([(wu._fname(t["path"]), t["qty"]) for t in d["top"]], [("Top.SLDASM", 6)])   # not 6 + 6


class DerivedPartRenameTests(RenameBase):
    def setUp(self):
        super().setUp()
        self.mirror = str(Path(self.part).with_name("Mirror.SLDPRT"))
        Path(self.mirror).write_text(json.dumps([self.part]), encoding="utf-8")
        self.ref(self.mirror, "", self.part)

    def test_derived_part_is_updated(self):
        r = wu.rename_execute(self.part, "Bolt-M8", None, False)
        self.assertTrue(r["result"]["ok"], r["result"])
        self.assertEqual(self.refs(self.mirror), [str(Path(self.part).with_name("Bolt-M8.SLDPRT"))])

    def test_reference_that_cannot_be_replaced_puts_everything_back(self):
        before = {f.name: f.read_text(encoding="utf-8") for f in Path(self.part).parent.iterdir() if f.is_file()}
        real_refs = FakeDoc.GetAllExternalReferences4
        with mock.patch.object(FakeDoc, "GetAllExternalReferences4",
                               lambda self_, opt: ((), (), ()) if self_.path.endswith("Mirror.SLDPRT") else real_refs(self_, opt)):
            r = wu.rename_execute(self.part, "Bolt-M8", None, False)
        self.assertFalse(r["result"]["ok"])
        self.assertIn("could not be updated", r["result"]["error"])
        self.assertTrue(r["result"]["rolled_back"])
        after = {f.name: f.read_text(encoding="utf-8") for f in Path(self.part).parent.iterdir() if f.is_file()}
        self.assertEqual(after, before)
