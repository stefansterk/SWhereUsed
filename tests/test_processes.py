"""Only one index process at a time, the whole process tree stopped, and the size of the index."""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import SWhereUsed as wu  # noqa: E402
from test_whereused import Base  # noqa: E402
from test_rename import RenameBase  # noqa: E402


class SingleIndexProcessTests(Base):
    def tearDown(self):
        if wu._index_lock_file:
            wu._index_lock_file.close()
            wu._index_lock_file = None
        super().tearDown()

    def test_a_second_index_process_cannot_start(self):
        self.assertTrue(wu._single_index_process())
        first = wu._index_lock_file
        self.assertFalse(wu._single_index_process())                 # a left-over or second one stops
        first.close()
        wu._index_lock_file = None
        self.assertTrue(wu._single_index_process())                  # the first one ended: free again


class SizeTests(Base):
    def test_size_of_the_index(self):
        self.ref("G:\\\\V\\\\A.SLDASM", "Default", "G:\\\\V\\\\B.SLDPRT")
        self.assertGreater(wu.status()["db_size"], 0)


class StuckJobTests(RenameBase):
    def test_stuck_job_is_stopped_as_a_tree_before_anything_is_put_back(self):
        order = []

        class Stuck:
            pid = 1234

            def wait(self, timeout=None):
                order.append("wait")
                if len(order) == 1:
                    raise wu.subprocess.TimeoutExpired("x", timeout)
                return -9
        self.patch("subprocess", mock.Mock(Popen=lambda args, **kw: Stuck(), TimeoutExpired=TimeoutError))
        wu.subprocess.TimeoutExpired = TimeoutError
        self.patch("_kill_tree", lambda proc: order.append("kill tree"))
        self.patch("_rollback", lambda journal: order.append("put back") or [])
        r = wu.rename_execute(self.part, "Bolt-M8", None, False)
        self.assertFalse(r["result"]["ok"])
        self.assertEqual(order[:3], ["wait", "kill tree", "wait"])
        self.assertEqual(order[-1], "put back")
        self.assertIn("stuck", r["result"]["error"])
