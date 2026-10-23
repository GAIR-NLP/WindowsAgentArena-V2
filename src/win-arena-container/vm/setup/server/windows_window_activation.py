"""Bound Windows controller activation without treating queued input as success."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ACTIVATION_SECONDS = 2.5
WORKER_SECONDS = 3.5
SUCCESS = ("Window activated successfully", 200)


def failure(reason, phase, *, emitted=False, status=503, evidence=None):
    body = {"status": "error", "error": reason, "phase": phase,
            "effect": "unknown" if emitted else "not_emitted", "retry": False}
    if evidence:
        body["activation_evidence"] = evidence
    return body, status


def activate(data, api, *, timeout=ACTIVATION_SECONDS, clock=time.monotonic, sleep=time.sleep):
    """Keep upstream discovery semantics; confirm the exact selected window."""
    deadline = clock() + timeout
    name = data.get("window_name")
    if not name:
        return "window_name required", 400
    if data.get("by_class", False):
        return "Get window by class name is currently not supported on Windows.", 500
    matches = api.find_windows(name)
    if not matches:
        return "Window {:} not found (empty results)".format(name), 404
    # Upstream non-strict mode only discovers the window, without activating it.
    if not data.get("strict", False):
        return SUCCESS
    hwnd = matches[0]
    identity = api.identity(hwnd)
    if identity is None:
        return failure("Selected window disappeared", "identity", status=409)
    emitted = False

    def verify():
        if api.identity(hwnd) != identity:
            return "stale"
        remaining = deadline - clock()
        if remaining <= 0:
            return "timeout"
        ready = api.ready(hwnd, min(100, max(1, int(remaining * 1000))))
        if api.identity(hwnd) != identity:
            return "stale"
        if clock() >= deadline:
            return "timeout"
        return "ready" if ready else "pending"

    state = verify()
    if state == "ready":
        return SUCCESS
    if state == "stale":
        return failure("Selected window disappeared or was replaced", "identity", status=409)
    if state == "timeout":
        return failure("Window activation deadline expired", "before_input")
    # Emit each request once. Async enqueue and foreground return values do not
    # establish the requested state; the checks below decide completion.
    api.restore_async(hwnd)
    emitted = True
    if api.identity(hwnd) != identity:
        return failure("Selected window disappeared or was replaced", "identity", emitted=emitted, status=409)
    if clock() >= deadline:
        return failure("Window activation deadline expired", "after_restore", emitted=emitted)
    api.foreground(hwnd, deadline=deadline, expected_identity=identity)
    while True:
        state = verify()
        if state == "ready":
            return SUCCESS
        if state == "stale":
            return failure("Selected window disappeared or was replaced", "identity", emitted=emitted, status=409)
        if state == "timeout":
            return failure("Window did not become active and responsive before its deadline", "confirmation", emitted=emitted,
                           evidence=getattr(api, "evidence", None))
        sleep(min(0.05, max(0, deadline - clock())))


class NativeWindows:
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        import pygetwindow
        self.ctypes, self.wintypes, self.gw = ctypes, wintypes, pygetwindow
        self.evidence = {"foreground_attempts": []}
        kernel = self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentThreadId.restype = wintypes.DWORD
        user = self.user = ctypes.WinDLL("user32", use_last_error=True)
        user.IsWindow.argtypes = [wintypes.HWND]
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user.GetWindowThreadProcessId.restype = wintypes.DWORD
        user.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        user.SetForegroundWindow.argtypes = [wintypes.HWND]
        user.SetForegroundWindow.restype = wintypes.BOOL
        user.GetForegroundWindow.restype = wintypes.HWND
        user.IsIconic.argtypes = [wintypes.HWND]
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t,
                                            ctypes.c_ssize_t, wintypes.UINT, wintypes.UINT,
                                            ctypes.POINTER(ctypes.c_size_t)]
        user.SendMessageTimeoutW.restype = ctypes.c_ssize_t

    def find_windows(self, name):
        # Preserve the original case-insensitive substring matching and order.
        return [window._hWnd for window in self.gw.getWindowsWithTitle(name)]

    def identity(self, hwnd):
        if not self.user.IsWindow(hwnd):
            return None
        pid = self.wintypes.DWORD()
        tid = self.user.GetWindowThreadProcessId(hwnd, self.ctypes.byref(pid))
        return (pid.value, tid) if pid.value and tid else None

    def restore_async(self, hwnd):
        # SW_SHOW preserves the geometry of an already visible maximized window.
        # SW_RESTORE is needed only when the selected window is minimized.
        return self.user.ShowWindowAsync(hwnd, 9 if self.user.IsIconic(hwnd) else 5)

    def uia_element(self, hwnd):
        # Each activation owns a fresh worker. Initialize COM lazily on its MTA
        # thread; the supervisor's existing deadline bounds provider calls.
        sys.coinit_flags = 0
        import comtypes.client
        module = comtypes.client.GetModule("UIAutomationCore.dll")
        client = comtypes.client.CreateObject("{FF48DBA4-60EF-4201-AA87-54103EEF594E}",
                                              interface=module.IUIAutomation)
        return client.ElementFromHandle(hwnd)

    def foreground(self, hwnd, *, deadline=None, expected_identity=None):
        def request(method):
            self.ctypes.set_last_error(0)
            result = bool(self.user.SetForegroundWindow(hwnd))
            self.evidence["foreground_attempts"].append({
                "method": method, "returned": result, "last_error": self.ctypes.get_last_error(),
                "foreground_after": self.user.GetForegroundWindow(), "target": hwnd})
            return result

        foreground = self.user.GetForegroundWindow()
        original_target = expected_identity if expected_identity is not None else self.identity(hwnd)
        original_foreground = self.identity(foreground) if foreground else None
        if original_target is None or self.identity(hwnd) != original_target:
            self.evidence["handoff_skipped"] = "target_identity_changed"
            return False
        self.evidence["foreground_before"] = foreground
        if request("direct"):
            return True
        # A background helper can be denied foreground activation. UIA offers a
        # semantic focus request; only the final Win32 checks establish success.
        if not original_foreground:
            self.evidence["handoff_skipped"] = "window_identity_unavailable"
            return False
        if (self.identity(hwnd) != original_target or self.identity(foreground) != original_foreground
                or self.user.GetForegroundWindow() != foreground):
            self.evidence["handoff_skipped"] = "window_or_foreground_changed"
            return False
        focus = self.evidence["uia_focus"] = {"attempted": False, "completed": False}
        try:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Activation deadline expired before UIA initialization")
            element = self.uia_element(hwnd)
            # COM discovery can cross process boundaries. Recheck both window
            # identities and foreground ownership before the sole mutation.
            if (self.identity(hwnd) != original_target or self.identity(foreground) != original_foreground
                    or self.user.GetForegroundWindow() != foreground):
                focus["skipped"] = "window_or_foreground_changed"
                return False
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Activation deadline expired during UIA initialization")
            focus["attempted"] = True
            element.SetFocus()
            focus["completed"] = True
            return True
        except Exception as error:
            focus["error_type"] = type(error).__name__
            focus["hresult"] = getattr(error, "hresult", None)
            raise

    def ready(self, hwnd, timeout_ms):
        state = {"target": hwnd, "foreground": self.user.GetForegroundWindow(),
                 "minimized": bool(self.user.IsIconic(hwnd)),
                 "visible": bool(self.user.IsWindowVisible(hwnd)), "responsive": None}
        self.evidence["last_confirmation"] = state
        if state["foreground"] != hwnd or state["minimized"] or not state["visible"]:
            return False
        answer = self.ctypes.c_size_t()
        # SMTO_BLOCK | SMTO_ABORTIFHUNG; never use NOTIMEOUTIFNOTHUNG.
        responsive = self.user.SendMessageTimeoutW(hwnd, 0, 0, 0, 3, timeout_ms, self.ctypes.byref(answer))
        state["responsive"] = bool(responsive)
        state["foreground_after_probe"] = self.user.GetForegroundWindow()
        return bool(responsive and state["foreground_after_probe"] == hwnd
                    and not self.user.IsIconic(hwnd) and self.user.IsWindowVisible(hwnd))


def run_bounded(data, *, timeout=WORKER_SECONDS, command=None):
    """A blocked native call cannot retain the Flask request thread indefinitely."""
    command = command or [sys.executable, str(Path(__file__).resolve()), "--worker"]
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        started = time.monotonic()
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=output, stderr=errors)
        try:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            process.communicate(json.dumps(data).encode(), timeout=remaining)
        except subprocess.TimeoutExpired:
            # These signals target only the helper we just created, never the app.
            process.terminate()
            try:
                process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=0.25)
            return failure("Window activation worker exceeded its deadline", "worker", emitted=True)
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=0.25)
            raise
        finally:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
        if time.monotonic() - started >= timeout:
            return failure("Window activation worker exceeded its deadline", "worker", emitted=True)
        output.seek(0)
        raw = output.read(65537)
        if process.returncode or len(raw) > 65536:
            return failure("Window activation worker failed", "worker", emitted=True)
        try:
            result = json.loads(raw)
            body, status = result["body"], result["status"]
            if type(status) is not int or not 100 <= status <= 599 or not isinstance(body, (str, dict)):
                raise ValueError("Malformed worker result")
        except (ValueError, TypeError, KeyError):
            return failure("Window activation worker returned invalid evidence", "worker", emitted=True)
        return body, status


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if args.worker:
        api = None
        try:
            api = NativeWindows()
            body, status = activate(json.load(sys.stdin), api)
        except Exception as exc:
            body, status = failure("Native window activation failed: " + type(exc).__name__, "native", emitted=True,
                                   evidence=getattr(api, "evidence", None))
        print(json.dumps({"body": body, "status": status}))
    else:
        parser.error("Select --worker")


if __name__ == "__main__":
    main()
