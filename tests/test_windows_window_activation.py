"""Contract and process-boundary tests for the Windows activation adapter."""
import ctypes
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import ast
import importlib.util
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "src/win-arena-container/vm/setup/server"
SPEC = importlib.util.spec_from_file_location("windows_window_activation", SERVER / "windows_window_activation.py")
activation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(activation)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, value):
        self.now += value


class FakeWindows:
    def __init__(self, clock):
        self.clock = clock
        self.matches = [10, 20]
        self.current_identity = (100, 200)
        self.is_ready = False
        self.complete_at = None
        self.events = []
        self.on_restore = None
        self.on_ready = None

    def find_windows(self, title):
        self.events.append(("find", title))
        return self.matches

    def identity(self, hwnd):
        return self.current_identity

    def ready(self, hwnd, timeout_ms):
        self.events.append(("ready", hwnd, timeout_ms))
        if self.on_ready:
            self.on_ready()
        return self.is_ready or self.complete_at is not None and self.clock() >= self.complete_at

    def restore_async(self, hwnd):
        self.events.append(("restore", hwnd))
        if self.on_restore:
            self.on_restore()
        return True

    def foreground(self, hwnd, *, deadline=None, expected_identity=None):
        self.events.append(("foreground", hwnd))
        return True


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.api = FakeWindows(self.clock)
        self.request = {"window_name": "MiXeD partial title", "strict": True}

    def run_activation(self, data=None, timeout=0.2):
        return activation.activate(self.request if data is None else data, self.api,
                                   timeout=timeout, clock=self.clock, sleep=self.clock.sleep)

    def mutations(self):
        return [e for e in self.api.events if e[0] in {"restore", "foreground"}]

    def test_original_discovery_parameters_and_first_match_are_preserved(self):
        self.api.complete_at = 0.05
        self.assertEqual(self.run_activation(), activation.SUCCESS)
        self.assertEqual(self.api.events[0], ("find", self.request["window_name"]))
        self.assertEqual(self.mutations(), [("restore", 10), ("foreground", 10)])

    def test_non_strict_only_discovers_without_input_or_readiness_requirement(self):
        self.assertEqual(self.run_activation({"window_name": "part", "strict": False}), activation.SUCCESS)
        self.assertEqual(self.api.events, [("find", "part")])

    def test_original_missing_and_class_errors(self):
        self.assertEqual(self.run_activation({}), ("window_name required", 400))
        self.assertEqual(self.run_activation({"window_name": "x", "by_class": True})[1], 500)
        self.assertEqual(self.api.events, [])
        self.api.matches = []
        self.assertEqual(self.run_activation(), ("Window MiXeD partial title not found (empty results)", 404))

    def test_already_active_and_responsive_needs_no_input(self):
        self.api.is_ready = True
        self.assertEqual(self.run_activation(), activation.SUCCESS)
        self.assertEqual(self.mutations(), [])

    def test_successful_enqueue_is_not_confirmation(self):
        body, status = self.run_activation()
        self.assertEqual(status, 503)
        self.assertEqual(body["effect"], "unknown")
        self.assertFalse(body["retry"])
        self.assertEqual(self.mutations(), [("restore", 10), ("foreground", 10)])
        self.assertAlmostEqual(self.clock(), 0.2)

    def test_late_confirmation_is_rejected(self):
        def late_ready():
            self.clock.now = 0.3
        self.api.on_ready = late_ready
        self.api.is_ready = True
        body, status = self.run_activation()
        self.assertEqual(status, 503)
        self.assertEqual(body["effect"], "not_emitted")
        self.assertEqual(self.mutations(), [])

    def test_replaced_window_stops_before_second_input(self):
        self.api.on_restore = lambda: setattr(self.api, "current_identity", (101, 201))
        body, status = self.run_activation()
        self.assertEqual(status, 409)
        self.assertEqual(body["effect"], "unknown")
        self.assertEqual(self.mutations(), [("restore", 10)])

    def test_identity_is_checked_after_native_readiness(self):
        self.api.on_ready = lambda: setattr(self.api, "current_identity", None)
        self.api.is_ready = True
        body, status = self.run_activation()
        self.assertEqual(status, 409)
        self.assertEqual(body["effect"], "not_emitted")
        self.assertEqual(self.mutations(), [])

    def test_discovery_can_exhaust_budget_without_emitting(self):
        original = self.api.find_windows
        def slow_discovery(name):
            self.clock.now = 2
            return original(name)
        self.api.find_windows = slow_discovery
        body, status = self.run_activation()
        self.assertEqual(status, 503)
        self.assertEqual(body["effect"], "not_emitted")
        self.assertEqual(self.mutations(), [])


class WorkerTests(unittest.TestCase):
    def test_late_worker_success_is_not_accepted(self):
        process = Mock(returncode=0)
        process.stdin.closed = True
        def create(*args, **kwargs):
            kwargs["stdout"].write(b'{"body":"Window activated successfully","status":200}')
            return process
        with patch.object(activation.subprocess, "Popen", side_effect=create), \
             patch.object(activation.time, "monotonic", side_effect=[0, 0, 4]):
            body, status = activation.run_bounded({}, timeout=3.5)
        self.assertEqual(status, 503)
        self.assertEqual(body["effect"], "unknown")

    def test_expired_spawn_budget_does_not_send_payload(self):
        process = Mock(returncode=None)
        process.stdin.closed = True
        with patch.object(activation.subprocess, "Popen", return_value=process), \
             patch.object(activation.time, "monotonic", side_effect=[0, 4]):
            self.assertEqual(activation.run_bounded({}, timeout=3.5)[1], 503)
        process.communicate.assert_not_called()
        process.terminate.assert_called_once()

    def test_worker_completion_preserves_body_and_status(self):
        code = "import json,sys; d=json.load(sys.stdin); print(json.dumps({'body': d, 'status': 409}))"
        body, status = activation.run_bounded({"window_name": "example"}, command=[sys.executable, "-c", code])
        self.assertEqual((body, status), ({"window_name": "example"}, 409))

    def test_blocked_helper_is_reaped_without_retry(self):
        # Event.wait blocks deterministically; only the supervisor uses a timer.
        code = "import threading; threading.Event().wait()"
        processes = []
        real_popen = subprocess.Popen
        def create(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            processes.append(child)
            return child
        with patch.object(activation.subprocess, "Popen", side_effect=create):
            body, status = activation.run_bounded({}, timeout=0.1, command=[sys.executable, "-c", code])
        self.assertEqual(status, 503)
        self.assertEqual(body["effect"], "unknown")
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())

    def test_corrupt_or_oversized_worker_evidence_fails_closed(self):
        for code in ["print('not json')", "print('x' * 65537)", "print('{\"body\":\"ok\",\"status\":true}')"]:
            with self.subTest(code=code):
                self.assertEqual(activation.run_bounded({}, command=[sys.executable, "-c", code])[1], 503)

    def test_interruption_reaps_only_the_created_helper(self):
        child = subprocess.Popen([sys.executable, "-c", "import threading; threading.Event().wait()"], stdin=subprocess.PIPE)
        with patch.object(activation.subprocess, "Popen", return_value=child), patch.object(child, "communicate", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                activation.run_bounded({})
        self.assertIsNotNone(child.poll())
        self.assertTrue(child.stdin.closed)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        from flask import Flask, request
        self.app = Flask(__name__)
        self.platform = Mock()
        self.platform.system.return_value = "Windows"
        self.subprocess = Mock()
        # Compile the actual route without starting the desktop server or
        # importing its unrelated platform and screenshot dependencies.
        tree = ast.parse((SERVER / "main.py").read_text(encoding="utf-8"))
        route = next(node for node in tree.body
                     if isinstance(node, ast.FunctionDef) and node.name == "activate_window")
        namespace = {"app": self.app, "request": request,
                     "platform": self.platform, "subprocess": self.subprocess}
        exec(compile(ast.Module(body=[route], type_ignores=[]), "main.py", "exec"), namespace)

    def post(self, data):
        with self.app.test_client() as client, \
             patch.dict(sys.modules, {"windows_window_activation": activation}):
            return client.post("/setup/activate_window", json=data)

    def test_windows_route_returns_bounded_worker_evidence(self):
        body = {"status": "error", "phase": "confirmation", "effect": "unknown", "retry": False}
        with patch.object(activation, "run_bounded", return_value=(body, 503)) as runner:
            response = self.post({"window_name": "Editor", "strict": True, "ignored": 1})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), body)
        runner.assert_called_once_with({"window_name": "Editor", "strict": True, "by_class": False})

    def test_missing_name_fails_without_starting_worker(self):
        with patch.object(activation, "run_bounded") as runner:
            response = self.post({})
        self.assertEqual(response.status_code, 400)
        runner.assert_not_called()

    def test_discovery_defaults_are_preserved(self):
        with patch.object(activation, "run_bounded", return_value=activation.SUCCESS) as runner:
            response = self.post({"window_name": "Editor"})
        self.assertEqual(response.status_code, 200)
        runner.assert_called_once_with({"window_name": "Editor", "strict": False, "by_class": False})

    def test_linux_keeps_its_existing_activation_path(self):
        self.platform.system.return_value = "Linux"
        with patch.object(activation, "run_bounded") as runner:
            response = self.post({"window_name": "Editor", "strict": True, "by_class": True})
        self.assertEqual(response.status_code, 200)
        self.subprocess.run.assert_called_once_with(["wmctrl", "-xFa", "Editor"])
        runner.assert_not_called()


class NativeConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.api = activation.NativeWindows.__new__(activation.NativeWindows)
        self.api.ctypes = ctypes
        self.api.evidence = {}
        self.api.user = Mock()
        self.api.user.GetForegroundWindow.return_value = 10
        self.api.user.IsIconic.return_value = False
        self.api.user.IsWindowVisible.return_value = True
        self.api.user.SendMessageTimeoutW.return_value = 1

    def test_all_native_postconditions_are_required(self):
        self.assertTrue(self.api.ready(10, 80))
        for method, value in [("GetForegroundWindow", 11), ("IsIconic", True),
                              ("IsWindowVisible", False), ("SendMessageTimeoutW", 0)]:
            with self.subTest(method=method):
                old = getattr(self.api.user, method).return_value
                getattr(self.api.user, method).return_value = value
                self.assertFalse(self.api.ready(10, 80))
                getattr(self.api.user, method).return_value = old

    def test_foreground_loss_during_response_probe_is_not_success(self):
        self.api.user.GetForegroundWindow.side_effect = [10, 11]
        self.assertFalse(self.api.ready(10, 80))

    def test_visible_window_keeps_geometry_while_minimized_window_restores(self):
        self.api.restore_async(10)
        self.api.user.ShowWindowAsync.assert_called_once_with(10, 5)
        self.api.user.ShowWindowAsync.reset_mock()
        self.api.user.IsIconic.return_value = True
        self.api.restore_async(10)
        self.api.user.ShowWindowAsync.assert_called_once_with(10, 9)


class ForegroundFocusTests(unittest.TestCase):
    def setUp(self):
        self.api = activation.NativeWindows.__new__(activation.NativeWindows)
        self.api.ctypes = Mock()
        self.api.ctypes.get_last_error.return_value = 0
        self.api.evidence = {"foreground_attempts": []}
        self.api.user = Mock()
        self.foreground = 20
        self.api.user.GetForegroundWindow.side_effect = lambda: self.foreground
        self.api.identity = lambda hwnd: {10: (100, 3), 20: (200, 2)}.get(hwnd)
        self.api.user.SetForegroundWindow.return_value = False
        self.element = Mock()
        self.api.uia_element = Mock(return_value=self.element)

    def test_denied_direct_request_uses_one_semantic_focus(self):
        self.assertTrue(self.api.foreground(10))
        self.api.user.SetForegroundWindow.assert_called_once_with(10)
        self.element.SetFocus.assert_called_once_with()
        self.assertEqual(self.api.evidence['uia_focus'], {'attempted': True, 'completed': True})
        # Completion is a request receipt; the outer ready checks still decide.
        self.assertEqual(self.foreground, 20)

    def test_direct_success_does_not_initialize_uia(self):
        self.api.user.SetForegroundWindow.return_value = True
        self.assertTrue(self.api.foreground(10))
        self.api.uia_element.assert_not_called()

    def test_target_replaced_during_direct_request_is_not_rebound(self):
        old = self.api.identity
        def direct(hwnd):
            self.api.identity = lambda handle: (999, 9) if handle == 10 else old(handle)
            return False
        self.api.user.SetForegroundWindow.side_effect = direct
        self.assertFalse(self.api.foreground(10, expected_identity=(100, 3)))
        self.api.uia_element.assert_not_called()

    def test_foreground_changes_during_direct_request_is_not_followed(self):
        def direct(hwnd):
            self.foreground = 99
            return False
        self.api.user.SetForegroundWindow.side_effect = direct
        self.assertFalse(self.api.foreground(10))
        self.api.uia_element.assert_not_called()

    def test_foreground_competition_during_com_discovery_stops_before_focus(self):
        def discover(hwnd):
            self.foreground = 99
            return self.element
        self.api.uia_element.side_effect = discover
        self.assertFalse(self.api.foreground(10))
        self.element.SetFocus.assert_not_called()

    def test_target_replacement_during_com_discovery_stops_before_focus(self):
        old = self.api.identity
        def discover(hwnd):
            self.api.identity = lambda handle: (999, 9) if handle == 10 else old(handle)
            return self.element
        self.api.uia_element.side_effect = discover
        self.assertFalse(self.api.foreground(10))
        self.element.SetFocus.assert_not_called()

    def test_foreground_identity_replacement_is_not_followed(self):
        old = self.api.identity
        def discover(hwnd):
            self.api.identity = lambda handle: (999, 9) if handle == 20 else old(handle)
            return self.element
        self.api.uia_element.side_effect = discover
        self.assertFalse(self.api.foreground(10))
        self.element.SetFocus.assert_not_called()

    def test_expired_budget_after_com_resolution_never_emits_focus(self):
        with patch.object(activation.time, 'monotonic', side_effect=[0, 2]):
            with self.assertRaises(TimeoutError):
                self.api.foreground(10, deadline=1)
        self.element.SetFocus.assert_not_called()

    def test_uia_error_stops_without_replaying_native_or_semantic_mutation(self):
        self.element.SetFocus.side_effect = OSError('provider failure')
        with self.assertRaises(OSError):
            self.api.foreground(10)
        self.element.SetFocus.assert_called_once()
        self.api.user.SetForegroundWindow.assert_called_once()
        self.assertEqual(self.api.evidence['uia_focus']['error_type'], 'OSError')

    def test_missing_target_identity_does_not_initialize_uia(self):
        self.api.identity = lambda hwnd: None
        self.assertFalse(self.api.foreground(10))
        self.api.uia_element.assert_not_called()


if __name__ == "__main__":
    unittest.main()
