"""Upkeep: preview pictures kept within a size, the index tidied up when idle, slow actions noted."""
import os
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import SWhereUsed as wu  # noqa: E402
from test_whereused import Base  # noqa: E402


class UpkeepTests(Base):
    def test_pictures_not_looked_at_go_first(self):
        d = wu._thumb_dir()
        d.mkdir(parents=True, exist_ok=True)
        now = time.time()
        for i in range(10):                                   # 10 pictures of 200 kB; i = 9 looked at most recently
            f = d / f"p{i}.png"
            f.write_bytes(b"x" * 200_000)
            os.utime(f, (now - 1000 + i, now - 1000 + i))
        before, after = wu.prune_thumbnails(limit_mb=1)       # 2 MB > 1 MB: down to 0.8 MB
        self.assertGreater(before, after)
        self.assertLessEqual(after, 0.8)
        left = sorted(p.name for p in d.iterdir())
        self.assertEqual(left, ["p6.png", "p7.png", "p8.png", "p9.png"])
        self.assertEqual(wu.prune_thumbnails(limit_mb=1)[0], wu.prune_thumbnails(limit_mb=1)[1])   # within: nothing

    def test_index_tidied_up_only_when_idle(self):
        self.ref("G:\\\\V\\\\A.SLDASM", "Default", "G:\\\\V\\\\B.SLDPRT")
        wu._last_request["t"] = time.time()                       # someone is using the app
        self.assertIsNone(wu.maintain_index())
        wu._last_request["t"] = time.time() - 3600
        with mock.patch.dict(wu._state, {"running": True}):
            self.assertIsNone(wu.maintain_index())            # an index run is busy
        done = wu.maintain_index()
        self.assertIsNotNone(done)
        self.assertIn("after_mb", done)
        self.assertIsNone(wu.maintain_index())                # not again within a week
        self.assertIsNotNone(wu.maintain_index(force=True))
        self.assertEqual(wu.whereused("G:\\\\V\\\\B.SLDPRT")["direct"][0]["path"], "G:\\\\V\\\\A.SLDASM")   # still all there

    def test_slow_actions_are_noted(self):
        wu._log_slow("GET", "/api/structure?path=D%3A%5CVault%5CBig.SLDASM", 4.2)
        wu._log_slow("GET", "/api/structure?path=D%3A%5CVault%5CBigger.SLDASM", 7.5)
        wu._log_slow("POST", "/api/packgo", 1.3)
        lines = wu.slow_summary()
        self.assertTrue(any("/api/structure: 2x slow, longest 7.5 s" in ln for ln in lines), lines)
        self.assertTrue(any("Big.SLDASM" in ln for ln in lines))
        self.patch("everything_status", lambda max_age=10: (False, "off"))
        self.assertIn("Actions that took longer than a second", wu.diagnostics())
