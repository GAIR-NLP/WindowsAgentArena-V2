"""Cancellation seals its phase, cleans owned resources, and stops dispatch."""
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import waa_compare


class InterruptEpisodeTests(unittest.TestCase):
    def episode(self, folder, failure_phase=None, exception=None, cleanup_script=None, agent_result=None):
        marker = folder.parent / "cleaned"
        script = cleanup_script or f"from pathlib import Path; Path({str(marker)!r}).touch()"
        args = SimpleNamespace(guest_url="http://guest", bridge_url="http://bridge", upstream_host=None,
            cleanup_command=json.dumps([sys.executable, "-c", script]), reset_command=None,
            setup_timeout=10, setup_settle=0, timeout=10, evaluator_timeout=10, cleanup_timeout=5,
            model="model", reasoning="high", max_responses=1)
        env = Mock()
        env.controller.get_screenshot.return_value = b"image"
        env.evaluate.return_value = 0.0
        bridge = Mock()
        bridge.shell_runtime.return_value = {"version": "5.1", "edition": "Desktop", "executable": "fixture-powershell.exe", "dotnet": ".NET Framework"}
        bridge.call.side_effect = [{"instructions": ""}, None, {"tools": []}]
        if failure_phase == "driver_start":
            bridge.start.side_effect = exception
        if failure_phase == "evaluator":
            env.evaluate.side_effect = exception
        with patch.dict(os.environ, {"WAA_BRIDGE_TOKEN": "test", "OPENAI_BASE_URL": "https://model"}), \
                patch("bridge_client.BridgeClient", return_value=bridge), \
                patch.object(waa_compare, "make_env", return_value=env), \
                patch.object(waa_compare, "prepare_env", side_effect=exception if failure_phase == "setup" else None), \
                patch.object(waa_compare, "run_agent", return_value=agent_result or {"status": "done"}):
            result = waa_compare.run_episode(args, "hcu", {"command": ["driver"]}, "test",
                                            {"_selection_id": "task", "id": "task", "instruction": "task"}, folder)
        self.assertTrue(marker.exists())
        self.assertEqual(result["vm_cleanup_status"], "completed")
        self.assertEqual(json.loads((folder / "result.json").read_text())["status"], result["status"])
        return result, env

    def test_keyboard_interrupt_records_setup_and_driver_phases(self):
        for phase in ("setup", "driver_start"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temp:
                result, env = self.episode(Path(temp) / "episode", phase, KeyboardInterrupt("operator stop"))
                self.assertEqual(result["status"], "interrupted")
                self.assertEqual(result["interrupted_phase"], phase)
                self.assertIn("operator stop", result["error"])
                self.assertEqual(result["evaluator_status"], "not_run_interrupted")
                env.evaluate.assert_not_called()

    def test_other_base_exception_is_cancelled_and_still_cleans_up(self):
        with tempfile.TemporaryDirectory() as temp:
            result, _ = self.episode(Path(temp) / "episode", "setup", SystemExit("cancelled by owner"))
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["interrupted_phase"], "setup")

    def test_interrupted_agent_keeps_response_count_and_skips_evaluator(self):
        with tempfile.TemporaryDirectory() as temp:
            result, env = self.episode(Path(temp) / "episode", agent_result={
                "status": "interrupted", "error_kind": "interrupted", "interrupted_phase": "agent",
                "successful_model_responses": 2, "usage": [{"total_tokens": 50}]})
        self.assertEqual(result["successful_model_responses"], 2)
        self.assertEqual(result["usage"], [{"total_tokens": 50}])
        self.assertEqual(result["status_before_interruption"], "error")
        env.evaluate.assert_not_called()

    def test_reset_interruption_recovers_early_owner_binding_and_cleans(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "episode"
            marker = Path(temp) / "cleaned"
            cleanup = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
            args = SimpleNamespace(guest_url="unused", bridge_url="unused", upstream_host=None,
                                   cleanup_command=None, reset_command='["dummy"]', run_id="test",
                                   setup_timeout=1, reset_cleanup_timeout=1, cleanup_timeout=3)
            def interrupted_reset(*_):
                waa_compare.write_json(folder / "reset-episode.json", {"cleanup_command": cleanup, "episode_id": "owned"})
                raise KeyboardInterrupt("operator stop during reset")
            with patch.object(waa_compare, "run_reset_hook", side_effect=interrupted_reset):
                result = waa_compare.run_episode(args, "hcu", {}, "test", {"_selection_id": "task", "id": "task"}, folder)
            self.assertEqual(result["status"], "interrupted")
            self.assertEqual(result["interrupted_phase"], "reset")
            self.assertTrue(marker.exists())
            self.assertEqual(result["vm_cleanup_status"], "completed")

    def test_interrupt_during_evaluation_preserves_completed_agent_status(self):
        with tempfile.TemporaryDirectory() as temp:
            result, _ = self.episode(Path(temp) / "episode", "evaluator", KeyboardInterrupt())
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["status_before_interruption"], "done")
        self.assertEqual(result["interrupted_phase"], "evaluator")

    def test_stop_signal_during_cleanup_does_not_abandon_owner_command(self):
        for signum, status in [(signal.SIGINT, "interrupted"), (signal.SIGTERM, "cancelled")]:
            with self.subTest(signal=signum), tempfile.TemporaryDirectory() as temp:
                marker = Path(temp) / "cleaned"
                script = ("import os; from pathlib import Path; "
                          f"os.kill({os.getpid()},{int(signum)}); Path({str(marker)!r}).touch()")
                previous = signal.getsignal(signum)
                result, _ = self.episode(Path(temp) / "episode", cleanup_script=script)
                self.assertIs(signal.getsignal(signum), previous)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["interrupted_phase"], "vm_cleanup")


class DispatchStopTests(unittest.TestCase):
    def dispatch(self, root, episode_result):
        drivers = root / "drivers.json"
        drivers.write_text(json.dumps({name: {"version": "test", "command": ["driver"]} for name in ("hcu", "cua")}))
        selected = [("test", {"_selection_id": name, "id": name}) for name in ("first", "second")]
        argv = ["waa_compare.py", "--task-list", "unused", "--drivers", str(drivers),
                "--run-id", "test", "--output", str(root / "run"), "--baseline-id", "test", "--reset-command", '["dummy"]']
        with patch.object(sys, "argv", argv), patch.object(waa_compare, "read_selection", return_value=selected), \
                patch.dict(os.environ, {"WAA_AZURE_CLI_AUTH": "0", "OPENAI_BASE_URL": "https://model", "OPENAI_API_KEY": "test", "WAA_BRIDGE_TOKEN": "test"}), \
                patch.object(waa_compare, "run_episode", return_value=episode_result) as run, \
                patch("builtins.print"):
            code = waa_compare.main()
        return code, run.call_count

    def test_terminal_rate_limit_seals_result_before_stopping_next_episode(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = {"status": "error", "error_kind": "rate_limit", "task_id": "first", "arm": "hcu", "vm_cleanup_status": "completed"}
            code, count = self.dispatch(root, result)
            self.assertEqual((code, count), (75, 1))
            self.assertEqual(json.loads((root / "run/results.json").read_text()), [result])
            stopped = json.loads((root / "run/dispatch-stop.json").read_text())
            self.assertEqual(stopped["planned_episode_count"], 4)
            self.assertEqual(stopped["reason"], "rate_limit")

    def test_interruption_stops_dispatch_after_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            code, count = self.dispatch(Path(temp), {"status": "interrupted", "task_id": "first", "arm": "hcu", "vm_cleanup_status": "completed"})
        self.assertEqual((code, count), (130, 1))

    def test_recovered_rate_limit_does_not_stop_successful_episodes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            code, count = self.dispatch(root, {"status": "done", "rate_limit_events": [{"http_status": 429}], "task_id": "first", "arm": "hcu", "vm_cleanup_status": "completed"})
            self.assertEqual((code, count), (None, 4))
            self.assertFalse((root / "run/dispatch-stop.json").exists())


if __name__ == "__main__":
    unittest.main()
