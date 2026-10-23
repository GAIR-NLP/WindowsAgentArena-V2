"""A failed evidence disk must not bypass an owned VM cleanup command."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import waa_compare


class CleanupEvidenceTests(unittest.TestCase):
    def test_reset_binding_write_failure_preserves_timeout_and_owned_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "episode"
            marker = Path(temp) / "cleaned"
            cleanup = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
            args = SimpleNamespace(guest_url="unused", bridge_url="unused", upstream_host=None,
                                   cleanup_command=None, reset_command='["dummy"]', run_id="test",
                                   setup_timeout=1, reset_cleanup_timeout=1, cleanup_timeout=3)
            original_write = waa_compare.write_json
            def failed_reset(*_):
                original_write(folder / "reset-episode.json", {"cleanup_command": cleanup, "episode_id": "owned"})
                raise TimeoutError("original reset timeout")
            def write(path, value):
                if path.name == "episode.json":
                    raise OSError("episode evidence disk full")
                original_write(path, value)
            with patch.object(waa_compare, "run_reset_hook", side_effect=failed_reset), \
                    patch.object(waa_compare, "write_json", side_effect=write):
                result = waa_compare.run_episode(args, "hcu", {}, "test", {"_selection_id": "task", "id": "task"}, folder)
            self.assertEqual(result["error"], "original reset timeout")
            self.assertTrue(any("episode evidence disk full" in note for note in result["error_notes"]))
            self.assertTrue(marker.exists())
            self.assertEqual(result["vm_cleanup_status"], "completed")

    def test_failed_initial_result_write_still_runs_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            marker = folder / "owner-cleaned"
            command = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
            original = waa_compare.write_json
            calls = []
            def write(path, value):
                calls.append(path)
                if len(calls) == 1:
                    raise OSError("disk full during seal")
                original(path, value)
            with patch.object(waa_compare, "write_json", side_effect=write):
                with self.assertRaisesRegex(RuntimeError, "VM cleanup status: completed"):
                    waa_compare.cleanup_episode(folder, {}, command, 5)
            self.assertTrue(marker.exists())
            result = json.loads((folder / "result.json").read_text())
            self.assertEqual(result["vm_cleanup_status"], "completed")
            self.assertIn("disk full", result["evidence_write_errors"][0])

    def test_failed_log_and_result_writes_do_not_skip_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            marker = folder / "owner-cleaned"
            command = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
            result = {}
            with patch.object(waa_compare, "write_json", side_effect=OSError("disk full")), \
                    patch.object(Path, "open", side_effect=OSError("log disk full")):
                with self.assertRaisesRegex(RuntimeError, "VM cleanup status: completed"):
                    waa_compare.cleanup_episode(folder, result, command, 5)
            self.assertTrue(marker.exists())
            self.assertEqual(len(result["evidence_write_errors"]), 3)

    def test_cleanup_failure_remains_visible_when_final_write_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            result = {}
            with patch.object(waa_compare, "write_json", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(RuntimeError, "VM cleanup status: error"):
                    waa_compare.cleanup_episode(Path(temp), result, [sys.executable, "-c", "raise SystemExit(7)"], 5)
            self.assertIn("CalledProcessError", result["vm_cleanup_error"])


if __name__ == "__main__":
    unittest.main()
