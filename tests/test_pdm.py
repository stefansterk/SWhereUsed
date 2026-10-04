"""SOLIDWORKS PDM: files in a vault view are renamed and moved through PDM, never outside it."""
import sys
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import SWhereUsed as wu  # noqa: E402
from test_rename import RenameBase  # noqa: E402

PDM_INI = "[.ShellClassInfo]\r\nIconResource=C:\\Program Files\\SOLIDWORKS PDM\\EdmRes.dll,0\r\n[ConisioVault]\r\nVaultName=ACME\r\n"


class PdmTests(RenameBase):
    def make_vault(self):
        (Path(self.part).parent / "desktop.ini").write_bytes("\ufeff".encode("utf-16-le") + PDM_INI.encode("utf-16-le"))

    def test_recognised_from_any_subfolder(self):
        self.make_vault()
        sub = Path(self.part).parent / "Projects" / "P26050"
        sub.mkdir(parents=True)
        self.assertEqual(wu.pdm_vault(str(sub / "X.SLDPRT")), str(Path(self.part).parent))
        self.assertEqual(wu.pdm_vault(self.part), str(Path(self.part).parent))

    def test_an_ordinary_desktop_ini_is_not_a_vault(self):
        (Path(self.part).parent / "desktop.ini").write_text("[.ShellClassInfo]\r\nIconResource=C:\\Windows\\system32\\imageres.dll,-3\r\n")
        self.assertIsNone(wu.pdm_vault(self.part))
        self.assertIsNone(wu.rename_plan(self.part, "Bolt-M8")["blockers"] or None)

    def test_renaming_in_a_vault_is_stopped(self):
        self.make_vault()
        plan = wu.rename_plan(self.part, "Bolt-M8")
        self.assertTrue(any("SOLIDWORKS PDM vault" in b for b in plan["blockers"]), plan["blockers"])
        r = wu.rename_execute(self.part, "Bolt-M8", None, False)
        self.assertFalse(r["executed"])
        self.assertTrue(Path(self.part).exists())
        status = wu.rename_readiness([self.part])["files"][self.part.lower()]
        self.assertFalse(status["ok"])
        self.assertIn("PDM", status["reason"])

    def test_a_file_outside_used_by_an_assembly_inside_is_stopped_too(self):
        vault = self.dir / "pdm"
        vault.mkdir()
        (vault / "desktop.ini").write_bytes(PDM_INI.encode("utf-16"))
        inside = vault / "Big.SLDASM"
        inside.write_text("[]", encoding="utf-8")
        self.ref(str(inside), "Default", self.part)
        plan = wu.rename_plan(self.part, "Bolt-M8")
        self.assertTrue(any("Big.SLDASM" in b and "PDM" in b for b in plan["blockers"]), plan["blockers"])

    def test_pack_and_go_out_of_a_vault_is_fine_into_a_vault_gets_a_note(self):
        self.make_vault()
        items = [{"path": p, "name": "NEW-" + Path(p).stem, "copy": True} for p in (self.top, self.sub, self.part)]
        out = wu.packgo_plan(self.top, items, str(self.dir / "outside"))
        self.assertEqual(out["blockers"], [])                           # copying out of a vault only reads
        into = wu.packgo_plan(self.top, items, str(Path(self.part).parent / "copies"))
        self.assertEqual(into["blockers"], [])
        self.assertTrue(any("check them in with PDM" in w for w in into["warnings"]))


class PdmRegistryTests(RenameBase):
    def tearDown(self):
        wu._pdm_reg_cache.update(t=0.0, roots={})
        super().tearDown()

    def test_vault_views_from_the_registry(self):
        self.patch("pdm_registry_vaults", lambda: {"D:\\\\PDM\\\\ACME": "ACME"})
        self.assertEqual(wu.pdm_vault("D:\\\\PDM\\\\ACME\\\\Projects\\\\P26050\\\\Bolt.SLDPRT"), "D:\\\\PDM\\\\ACME")
        self.assertEqual(wu.pdm_vault("d:\\\\pdm\\\\acme\\\\x.sldasm"), "D:\\\\PDM\\\\ACME")          # any case
        self.assertIsNone(wu.pdm_vault("D:\\\\PDM\\\\ACME2\\\\x.SLDPRT"))                           # whole folder names
        self.assertIsNone(wu.pdm_vault(self.part))

    def test_reading_the_registry(self):
        import types
        tree = {(1, r"SOFTWARE\SolidWorks\Applications\PDMWorks Enterprise\Databases"): {"ACME": {"ShellRoot": "C:\\ACME", "DbName": "ACME", "DbServer": "oldserver", "ServerLoc": "oldserver"}},
                (2, r"SOFTWARE\SolidWorks\Applications\PDMWorks Enterprise\Databases"): {"Mine": {"Location": "D:\\MyVault\\"}}}

        class Key:
            def __init__(self, hive, path, subs=None, value=None):
                self.hive, self.path, self.subs, self.value = hive, path, subs, value
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def OpenKey(parent, sub):
            if isinstance(parent, Key):
                if sub not in parent.subs:
                    raise OSError
                return Key(parent.hive, sub, value=parent.subs[sub])
            if (parent, sub) not in tree:
                raise OSError
            return Key(parent, sub, subs=tree[(parent, sub)])
        fake = types.SimpleNamespace(HKEY_LOCAL_MACHINE=1, HKEY_CURRENT_USER=2, OpenKey=OpenKey,
                                     QueryInfoKey=lambda k: (len(k.subs), 0, 0),
                                     EnumKey=lambda k, i: list(k.subs)[i],
                                     QueryValueEx=lambda k, n: (k.value[n], 1) if n in k.value else (_ for _ in ()).throw(OSError()))
        wu._pdm_reg_cache.update(t=0.0, roots={})
        with mock.patch.dict(sys.modules, {"winreg": fake}):
            self.assertEqual(wu.pdm_registry_vaults(), {"C:\\ACME": "ACME", "D:\\MyVault": "Mine"})
