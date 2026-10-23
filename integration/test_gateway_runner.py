"""Exercise gateway admission and stop decisions without a guest or model."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from gateway_config import load_gateway_config, DEFAULT_HEADERS
import waa_compare


class GatewayRunnerTests(unittest.TestCase):
    def config(self, root, **changes):
        value = dict(base_url="https://gateway.example/v1", configuration_id="test-pool-v1",
                     routing_revision="task-affinity-v1", authentication="none", **{})
        value.update(changes)
        path = root / "gateway.json"
        path.write_text(json.dumps(value))
        return path

    def test_config_accepts_explicit_custom_headers(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.config(Path(temp), headers={key: "X-Custom-" + key.replace("_", "-") for key in DEFAULT_HEADERS})
            self.assertEqual(load_gateway_config(path)["headers"]["endpoint"], "X-Custom-endpoint")

    def test_config_rejects_secret_fields_invalid_urls_and_ambiguous_headers(self):
        variants = [{"api_key": "private"}, {"authentication": "auto"}, {"base_url": "https://user:secret@example/v1"},
                    {"base_url": "https://example/v1?key=secret"}, {"base_url": "file:///v1"},
                    {"headers": dict.fromkeys(DEFAULT_HEADERS, "X-Same")}, {"headers": {}}]
        with tempfile.TemporaryDirectory() as temp:
            for change in variants:
                with self.subTest(change=change), self.assertRaises(ValueError):
                    load_gateway_config(self.config(Path(temp), **change))

    def test_routes_separate_runs_arms_tasks_and_reserved_characters(self):
        base = "https://gateway.example/v1"
        routes = {waa_compare.gateway_episode_route(base, *identity)[0] for identity in
                  [("run:a", "hcu", "task"), ("run", "a:hcu", "task"),
                   ("run:a", "cua", "task"), ("run:a", "hcu", "task/with space")]}
        self.assertEqual(len(routes), 4)
        self.assertTrue(all(url.startswith("https://gateway.example/t/") and url.endswith("/v1") for url in routes))

    def run_cli(self, root, result=None, extra=(), authentication="none", key=None):
        drivers = root / "drivers.json"
        drivers.write_text(json.dumps({arm: {"version": "test-v1", "candidate_id": "candidate-test",
                                            "command": ["driver"]} for arm in ("hcu", "cua")}))
        config = self.config(root, authentication=authentication)
        argv = ["waa_compare.py", "--task-list", "unused", "--drivers", str(drivers), "--run-id", "run",
                "--baseline-id", "baseline-v1", "--output", str(root / "run"), "--reset-command", '["owner"]',
                "--model", "model-selected-by-user", "--reasoning", "medium", "--gateway-config", str(config), *extra]
        env = {"WAA_BRIDGE_TOKEN": "private-bridge-token"}
        if key:
            env["OPENAI_API_KEY"] = key
        selected = [("test", {"id": "task", "_selection_id": "task"})]
        terminal = result or dict(status="done", arm="hcu", task_id="task", vm_cleanup_status="completed")
        with patch.object(sys, "argv", argv), patch.dict(os.environ, env, clear=True), \
                patch.object(waa_compare, "read_selection", return_value=selected), \
                patch.object(waa_compare, "run_episode", return_value=terminal) as run, \
                patch("builtins.print"), patch.object(sys, "stderr", io.StringIO()):
            code = waa_compare.main()
        return code, run.call_count

    def test_config_and_driver_binding_are_recorded_without_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(self.run_cli(root, authentication="api_key", key="private-model-key"), (None, 2))
            raw = (root / "run/manifest.json").read_text()
            manifest = json.loads(raw)
        self.assertNotIn("private-model-key", raw)
        self.assertNotIn("private-bridge-token", raw)
        self.assertEqual(manifest["model"], "model-selected-by-user")
        self.assertEqual(manifest["reasoning"], "medium")
        self.assertEqual(manifest["selected_task_ids"], ["task"])
        self.assertEqual(manifest["arms"]["hcu"]["candidate_id"], "candidate-test")
        self.assertEqual(manifest["max_successful_model_responses"], 100)

    def test_auth_requires_key_unless_explicit_none(self):
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(SystemExit):
            self.run_cli(Path(temp), authentication="api_key")
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(self.run_cli(Path(temp)), (None, 2))

    def test_terminal_errors_stop_before_next_episode_and_preserve_results(self):
        for reason, code in [("rate_limit", 75), ("routing", 78), ("interrupted", 130)]:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                result = dict(status="interrupted" if reason == "interrupted" else "error", error_kind=reason,
                              arm="hcu", task_id="task", vm_cleanup_status="completed")
                self.assertEqual(self.run_cli(root, result), (code, 1))
                self.assertEqual(json.loads((root / "run/results.json").read_text()), [result])
                self.assertEqual(json.loads((root / "run/dispatch-stop.json").read_text())["reason"], reason)

    def test_invalid_retry_budget_fails_before_dispatch(self):
        for extra in [("--rate-limit-base-delay", "0"), ("--timeout", "nan"), ("--rate-limit-max-delay", "inf")]:
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as temp, self.assertRaises(SystemExit):
                self.run_cli(Path(temp), extra=extra)


if __name__ == "__main__":
    unittest.main()
