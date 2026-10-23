"""Authenticated host-to-guest transport for one persistent stdio MCP session."""

import argparse
import base64
from collections import deque
import ctypes
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import uuid


class BridgeFailure(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class WindowsJob:
    """Own a command tree without terminating successful GUI launches on close."""
    def __init__(self):
        from ctypes import wintypes
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                            wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            getattr(self.api, name).argtypes = arguments
            getattr(self.api, name).restype = result
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def attach(self, process):
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def terminate(self):
        if not self.api.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_int64) for name in ("user", "kernel", "period_user", "period_kernel")] + [
                (name, ctypes.c_uint32) for name in ("faults", "total", "active", "terminated")]
        deadline = time.monotonic() + 5
        while True:
            accounting = Accounting()
            if not self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(accounting),
                                                       ctypes.sizeof(accounting), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if accounting.active == 0:
                return
            if time.monotonic() >= deadline:
                raise BridgeFailure("PowerShell process tree remains active after termination", 503)
            time.sleep(0.02)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class Command:
    def __init__(self, command, *, env=None):
        self.lock = threading.Lock()
        self.cancelled = False
        self.finished = threading.Event()
        self.stdout = tempfile.TemporaryFile()
        self.stderr = tempfile.TemporaryFile()
        self.job = None
        # The supervisor cannot launch the command until it belongs to its job.
        supervisor = "import sys,subprocess; gate=sys.stdin.buffer.read(1); sys.exit(subprocess.call(sys.argv[1:],stdin=subprocess.DEVNULL) if gate==b'1' else 125)"
        try:
            self.job = WindowsJob() if os.name == "nt" else None
            self.process = subprocess.Popen([sys.executable, "-c", supervisor, *command],
                stdin=subprocess.PIPE, stdout=self.stdout, stderr=self.stderr,
                start_new_session=os.name != "nt", env=env)
            if self.job:
                self.job.attach(self.process)
            self.process.stdin.write(b"1")
            self.process.stdin.close()
        except Exception:
            if hasattr(self, "process"):
                self.process.stdin.close()
                self.process.wait(timeout=5)
            self.dispose()
            raise

    def cancel(self, force=False):
        with self.lock:
            if self.process.poll() is not None and not self.cancelled and not force:
                return
            self.cancelled = True
            if self.job:
                self.job.terminate()
            else:
                import signal
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            self.process.wait(timeout=5)

    def wait(self, timeout):
        try:
            return self._wait(timeout)
        finally:
            self.finished.set()

    def _wait(self, timeout):
        timed_out = False
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self.cancel(force=True)
        result = {"returncode": self.process.returncode, "timed_out": timed_out,
                  "cancelled": self.cancelled,
                  "effect": "unknown" if timed_out or self.cancelled else "not_verified"}
        for name, stream in (("output", self.stdout), ("error", self.stderr)):
            stream.seek(0)
            data = stream.read(1024 * 1024 + 1)
            result[name] = data[:1024 * 1024].decode("utf-8", errors="replace")
            result[name + "_truncated"] = len(data) > 1024 * 1024
        return result

    def dispose(self):
        if self.job:
            self.job.close()
        self.stdout.close()
        self.stderr.close()


def interactive_session():
    if os.name != "nt":
        return None
    session = ctypes.c_ulong()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        raise RuntimeError("Cannot determine Windows process session")
    if session.value == 0:
        raise RuntimeError("Bridge must run in the logged-in desktop session, not Session 0")
    return session.value


class Session:
    def __init__(self, config):
        shell = config.get("powershell", {})
        if not isinstance(shell, dict) or set(shell) - {"executable", "expected_version"}:
            raise ValueError("powershell must contain only executable and expected_version")
        self.powershell_executable = shell.get("executable")
        self.powershell_expected_version = shell.get("expected_version")
        if self.powershell_executable is not None and (not isinstance(self.powershell_executable, str)
                or not os.path.isabs(self.powershell_executable)):
            raise ValueError("PowerShell executable must be an absolute path")
        if self.powershell_expected_version is not None and (not isinstance(self.powershell_expected_version, str)
                or not self.powershell_expected_version):
            raise ValueError("PowerShell expected_version must be a non-empty string")
        self.powershell_runtime = None
        self.process = subprocess.Popen(
            config["command"], cwd=config.get("cwd"),
            env={**os.environ, **config.get("env", {})}, shell=False,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
        )
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.close_lock = threading.Lock()
        self.command_lock = threading.Lock()
        self.command = None
        self.incoming = queue.Queue()
        self.stderr = deque(maxlen=50)
        self.next_id = 0
        self.failed = False
        self.shutdown_error = None
        self.readers = [threading.Thread(target=self._read, daemon=True),
                        threading.Thread(target=self._read_stderr, daemon=True)]
        for reader in self.readers:
            reader.start()

    def _send(self, message, timeout=5):
        completed = threading.Event()
        errors = []

        def write():
            try:
                with self.write_lock:
                    self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                    self.process.stdin.flush()
            except Exception as exc:
                errors.append(exc)
            finally:
                completed.set()

        # Pipe writes can block when a server stops reading. The caller must
        # retain control so cleanup can terminate that server and unblock IO.
        threading.Thread(target=write, daemon=True).start()
        if not completed.wait(timeout):
            raise BridgeFailure("MCP send timeout; action outcome unknown; never retry automatically", 504)
        if errors:
            raise BridgeFailure("MCP send failed; action outcome unknown: " + str(errors[0]), 502)

    def _read_stderr(self):
        for line in self.process.stderr:
            self.stderr.append(line[-4096:].rstrip())

    def _read(self):
        try:
            for line in self.process.stdout:
                message = json.loads(line)
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise ValueError("Invalid JSON-RPC stdout message")
                # This client advertises no sampling/elicitation capabilities.
                if "method" in message and "id" in message:
                    if message["method"] == "ping":
                        self._send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
                    else:
                        self._send({"jsonrpc": "2.0", "id": message["id"], "error": {
                            "code": -32601, "message": "Client capability not supported"}})
                else:
                    self.incoming.put(message)
        except Exception as exc:
            self.incoming.put(BridgeFailure("MCP stdout failed: " + str(exc), 502))
        finally:
            self.incoming.put(BridgeFailure("MCP subprocess exited", 502))

    def call(self, method, params, timeout):
        if not isinstance(method, str) or not method:
            raise BridgeFailure("method must be a non-empty string")
        if params is not None and not isinstance(params, dict):
            raise BridgeFailure("params must be an object")
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 1800:
            raise BridgeFailure("timeout must be between 0 and 1800 seconds")
        if not self.lock.acquire(blocking=False):
            raise BridgeFailure("Concurrent calls on one desktop session are rejected", 409)
        try:
            if self.failed:
                raise BridgeFailure("Session failed; stop it before starting a fresh task", 409)
            request = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                request["params"] = params
            notification = method.startswith("notifications/")
            if not notification:
                self.next_id += 1
                request["id"] = self.next_id
            deadline = time.monotonic() + timeout
            self._send(request, timeout=timeout)
            if notification:
                return {"response": None, "notifications": []}
            notifications = []
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                message = self.incoming.get(timeout=remaining)
                if isinstance(message, Exception):
                    raise message
                if "method" in message:
                    notifications.append(message)
                elif message.get("id") == request["id"]:
                    return {"response": message, "notifications": notifications}
                else:
                    raise BridgeFailure("Unexpected MCP response ID", 502)
        except queue.Empty:
            self.failed = True
            self.close()
            raise BridgeFailure("MCP timeout; action outcome unknown; never retry automatically", 504)
        except (OSError, BridgeFailure) as exc:
            if isinstance(exc, BridgeFailure) and exc.status == 409:
                raise
            self.failed = True
            self.close()
            if isinstance(exc, BridgeFailure):
                raise
            raise BridgeFailure("MCP transport failed; action outcome unknown: " + str(exc), 502)
        finally:
            self.lock.release()

    def close(self):
        with self.close_lock:
            self.failed = True
            with self.command_lock:
                if self.command is not None:
                    try:
                        self.command.cancel()
                        if not self.command.finished.wait(timeout=5):
                            raise BridgeFailure("PowerShell command reader did not finish", 503)
                        self.command.dispose()
                        self.command = None
                    except Exception as exc:
                        self.shutdown_error = "PowerShell cleanup failed: " + str(exc)
                        raise BridgeFailure(self.shutdown_error, 503)
            # Prefer EOF, but a backpressured writer cannot release this lock
            # until its reader exits. Terminate the parent before waiting.
            acquired = self.write_lock.acquire(blocking=False)
            if not acquired:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.shutdown_error = "MCP process did not stop during blocked write"
                        raise BridgeFailure(self.shutdown_error, 503)
                acquired = self.write_lock.acquire(timeout=5)
                if not acquired:
                    self.shutdown_error = "MCP input pipe remains blocked; inspect child processes before continuing"
                    raise BridgeFailure(self.shutdown_error, 503)
            try:
                if not self.process.stdin.closed:
                    try:
                        self.process.stdin.close()
                    except BrokenPipeError:
                        pass
            finally:
                self.write_lock.release()
            if self.process.poll() is None:
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.shutdown_error = "MCP process did not stop; inspect guest before continuing"
                        raise BridgeFailure(self.shutdown_error, 503)
            for reader in self.readers:
                reader.join(timeout=2)
            if any(reader.is_alive() for reader in self.readers):
                self.shutdown_error = "MCP output pipes remain open; inspect child processes before continuing"
                raise BridgeFailure(self.shutdown_error, 503)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()

    def shell_runtime(self, timeout=15):
        if os.name != "nt":
            raise BridgeFailure("PowerShell execution requires a Windows guest")
        if not self.lock.acquire(blocking=False):
            raise BridgeFailure("Concurrent calls on one desktop session are rejected", 409)
        try:
            if self.failed:
                raise BridgeFailure("Session failed; stop it before starting a fresh task", 409)
            if self.powershell_runtime is None:
                script = "$PSVersionTable | Out-Null; @{version=$PSVersionTable.PSVersion.ToString(); edition=$PSVersionTable.PSEdition; executable=(Get-Process -Id $PID).Path; dotnet=[System.Runtime.InteropServices.RuntimeInformation]::FrameworkDescription} | ConvertTo-Json -Compress"
                result = self._run_powershell(script, timeout)
                if result["returncode"] != 0 or result["timed_out"] or result["cancelled"]:
                    raise BridgeFailure("PowerShell runtime probe failed: " + json.dumps(result), 503)
                runtime = json.loads(result["output"])
                if (not isinstance(runtime, dict) or any(not isinstance(runtime.get(key), str) or not runtime[key]
                        for key in ("version", "edition", "executable", "dotnet"))):
                    raise BridgeFailure("PowerShell runtime probe returned incomplete metadata", 503)
                selected = self._shell_executable()
                if os.path.normcase(runtime["executable"]) != os.path.normcase(selected):
                    raise BridgeFailure("PowerShell executable identity differs from selected path", 503)
                if self.powershell_expected_version and runtime["version"] != self.powershell_expected_version:
                    raise BridgeFailure("PowerShell version differs from expected_version", 503)
                self.powershell_runtime = runtime
            return dict(self.powershell_runtime)
        finally:
            self.lock.release()

    def _shell_executable(self):
        return self.powershell_executable or os.path.join(os.environ["SystemRoot"], "System32", "WindowsPowerShell", "v1.0", "powershell.exe")

    def powershell(self, script, timeout):
        if not isinstance(script, str):
            raise BridgeFailure("script must be a string")
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
            raise BridgeFailure("PowerShell timeout must be between 0 and 120 seconds")
        if os.name != "nt":
            raise BridgeFailure("PowerShell execution requires a Windows guest")
        started = time.monotonic()
        runtime = self.shell_runtime(timeout=min(15, timeout))
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise BridgeFailure("PowerShell runtime verification exhausted the command timeout; task script was not started", 408)
        if not self.lock.acquire(blocking=False):
            raise BridgeFailure("Concurrent calls on one desktop session are rejected", 409)
        try:
            return dict(self._run_powershell(script, remaining), runtime=runtime)
        finally:
            self.lock.release()

    def _run_powershell(self, script, timeout):
        # Caller owns the session lock; probe and task commands share cleanup.
        command = None
        completed = False
        try:
            with self.command_lock:
                if self.failed:
                    raise BridgeFailure("Session failed; stop it before starting a fresh task", 409)
                encoded = base64.b64encode(("[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new();\n" + script).encode("utf-16-le")).decode("ascii")
                executable = self._shell_executable()
                # Encoded, redirected noninteractive shells default stderr to
                # CLIXML unless OutputFormat is explicit. Startup NO_COLOR also
                # covers parser errors, before any script prefix can execute.
                shell_env = dict(os.environ)
                shell_env["NO_COLOR"] = "1"
                command = self.command = Command([executable, "-NoProfile", "-NonInteractive",
                    "-OutputFormat", "Text", "-EncodedCommand", encoded], env=shell_env)
            result = command.wait(timeout)
            completed = True
            return result
        except Exception as exc:
            if command is not None:
                self.failed = True
                self.shutdown_error = "PowerShell execution/cleanup failed: " + str(exc)
            raise
        finally:
            with self.command_lock:
                if completed:
                    command.dispose()
                    self.command = None


class Bridge:
    def __init__(self, config, token):
        if not token:
            raise ValueError("Bridge token must be non-empty")
        self.token = token
        self.arms = config["arms"]
        for arm in self.arms.values():
            command = arm.get("command")
            if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
                raise ValueError("Each arm needs a non-empty command array")
            if not os.path.isabs(command[0]):
                raise ValueError("Executable paths must be absolute")
        self.sessions = {}
        self.lock = threading.Lock()
        self.desktop_session = interactive_session()

    def dispatch(self, path, body):
        if path == "/health":
            with self.lock:
                sessions = list(self.sessions.items())
            return {"status": "ok", "desktop_session": self.desktop_session,
                    "arms": list(self.arms), "active_sessions": len(sessions),
                    "active_commands": [key for key, session in sessions
                                        if (command := session.command) is not None and command.process.poll() is None],
                    "failed_sessions": {key: session.shutdown_error for key, session in sessions
                                        if session.failed}}
        if path == "/session/start":
            arm = body.get("arm")
            if arm not in self.arms:
                raise BridgeFailure("Unknown configured arm")
            config = self.arms[arm]
            if body.get("command", config["command"]) != config["command"]:
                raise BridgeFailure("command does not exactly match the configured arm", 403)
            with self.lock:
                if self.sessions:
                    raise BridgeFailure("Desktop already owns a session; stop it before switching arms", 409)
                session_id = str(uuid.uuid4())
                self.sessions[session_id] = Session(config)
            return {"session_id": session_id, "arm": arm, "desktop_session": self.desktop_session}
        if path not in ("/session/call", "/session/stop", "/session/powershell", "/session/runtime"):
            raise BridgeFailure("Unknown route", 404)
        session_id = body.get("session_id")
        with self.lock:
            session = self.sessions.get(session_id)
        if session is None:
            raise BridgeFailure("Unknown session", 404)
        if path == "/session/runtime":
            return session.shell_runtime()
        if path == "/session/call":
            return session.call(body.get("method"), body.get("params"), body.get("timeout", 120))
        if path == "/session/powershell":
            return session.powershell(body.get("script"), body.get("timeout", 120))
        with self.lock:
            session.close()
            self.sessions.pop(session_id, None)
        return {"stopped": True}


def make_server(bridge, host, port):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                if not hmac.compare_digest(self.headers.get("X-Bridge-Token", ""), bridge.token):
                    raise BridgeFailure("Unauthorized", 401)
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or size > 64 * 1024 * 1024:
                    raise BridgeFailure("Invalid request size", 413)
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise BridgeFailure("Request must be an object")
                response, status = bridge.dispatch(self.path, body), 200
            except BridgeFailure as exc:
                response, status = {"error": str(exc)}, exc.status
            except (ValueError, KeyError, TypeError) as exc:
                response, status = {"error": str(exc)}, 400
            except OSError as exc:
                response, status = {"error": str(exc)}, 502
            payload = json.dumps(response, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            pass  # Request payloads and authentication headers never enter logs.

    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5051)
    args = parser.parse_args()
    bridge = Bridge(json.loads(Path(args.config).read_text(encoding="utf-8-sig")),
                    Path(args.token_file).read_text(encoding="utf-8-sig").strip())
    server = make_server(bridge, args.host, args.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        for session in bridge.sessions.values():
            session.close()


if __name__ == "__main__":
    main()
