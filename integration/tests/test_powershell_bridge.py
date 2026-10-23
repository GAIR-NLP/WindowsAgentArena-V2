"""Command lifetime tests use real subprocesses; Windows also exercises job ownership."""
import os
import signal
import subprocess
import sys
import unittest

from guest_mcp_bridge import Command


class CommandTests(unittest.TestCase):
    def test_successful_launch_does_not_wait_for_or_kill_child_with_inherited_output(self):
        code = "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import threading; threading.Event().wait()']); print(p.pid,flush=True)"
        command = Command([sys.executable, "-c", code])
        child = None
        try:
            result = command.wait(5)
            self.assertFalse(result["timed_out"])
            self.assertEqual(result["returncode"], 0)
            child = int(result["output"].strip())
            command.dispose()
            # A successful command may intentionally leave its GUI process alive.
            if os.name == "nt":
                self.assertIn(str(child).encode(), subprocess.check_output(["tasklist", "/FI", "PID eq %s" % child, "/FO", "CSV"]))
            else:
                os.kill(child, 0)
        finally:
            if child is not None:
                os.kill(child, signal.SIGTERM)
            command.dispose()

    def test_timeout_reports_unknown_effect_and_terminates_command(self):
        command = Command([sys.executable, "-c", "import threading; print('started',flush=True); threading.Event().wait()"])
        try:
            result = command.wait(0.5)
            self.assertTrue(result["timed_out"])
            self.assertTrue(result["cancelled"])
            self.assertEqual(result["effect"], "unknown")
            self.assertIsNotNone(command.process.poll())
        finally:
            command.cancel()
            command.dispose()

    def test_large_output_is_bounded_and_explicitly_truncated(self):
        command = Command([sys.executable, "-c", "import sys; sys.stdout.write('x'*1100000); sys.stderr.write('diagnostic')"])
        try:
            result = command.wait(5)
            self.assertEqual(len(result["output"]), 1024 * 1024)
            self.assertTrue(result["output_truncated"])
            self.assertEqual(result["error"], "diagnostic")
            self.assertFalse(result["error_truncated"])
        finally:
            command.dispose()


if __name__ == "__main__":
    unittest.main()
