"""Exercise the real HTTP bridge and persistent subprocess transport."""

import base64
import json
import os
from pathlib import Path
import sys
import signal
import tempfile
import threading
import unittest
from unittest.mock import patch

from bridge_client import BridgeClient, BridgeError
from guest_mcp_bridge import Bridge, make_server


FAKE_MCP = r'''
import base64, json, subprocess, sys, time
count = 0
exit_log = None
for line in sys.stdin:
    request = json.loads(line)
    if "method" not in request or "id" not in request:
        continue
    count += 1
    method = request["method"]
    if method == "stall":
        with open(request["params"]["log"], "a") as f:
            f.write("mutation\n")
        time.sleep(60)
    if method == "exit_log":
        exit_log = request["params"]["log"]
    if method == "childpipes":
        child = subprocess.Popen([sys.executable, "-c", "import threading; threading.Event().wait()"], stdin=subprocess.DEVNULL)
        result = {"pid":child.pid}
    elif method == "tools/call":
        print(json.dumps({"jsonrpc":"2.0", "method":"notifications/progress", "params":{"progress":1}}), flush=True)
        print(json.dumps({"jsonrpc":"2.0", "method":"ping", "id":"server-ping"}), flush=True)
        ping = json.loads(sys.stdin.readline())
        assert ping == {"jsonrpc":"2.0", "id":"server-ping", "result":{}}
        result = {"content":[{"type":"image", "mimeType":"image/png", "data":base64.b64encode(b"pixel" * 300000).decode()}, {"type":"text", "text":str(count)}]}
    else:
        result = {"count":count, "method":method}
    response = {"jsonrpc":"2.0", "id":request["id"], "result":result}
    if method == "bad":
        response = {"jsonrpc":"2.0", "id":request["id"], "error":{"code":-32602,"message":"invalid arguments"}}
    print(json.dumps(response), flush=True)
    if method == "stop_reading":
        time.sleep(60)
if exit_log:
    with open(exit_log, "w") as f:
        f.write("clean EOF exit")
'''


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.command = [sys.executable, "-u", "-c", FAKE_MCP]
        self.bridge = Bridge({"arms": {"cua": {"command": self.command}}}, "test-secret")
        self.server = make_server(self.bridge, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%s" % self.server.server_port
        self.client = BridgeClient(self.url, "test-secret")

    def tearDown(self):
        for session in self.bridge.sessions.values():
            session.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_persistent_state_notifications_server_ping_and_full_image(self):
        session = self.client.start("cua", self.command)
        self.assertEqual(self.client.call(session, "initialize", {})["count"], 1)
        self.assertIsNone(self.client.call(session, "notifications/initialized"))
        self.assertEqual(self.client.call(session, "tools/list")["count"], 2)
        result = self.client.call(session, "tools/call", {"name": "screenshot"})
        self.assertEqual(base64.b64decode(result["content"][0]["data"]), b"pixel" * 300000)
        self.assertEqual(result["content"][1]["text"], "3")
        self.assertEqual(self.client.notifications[0]["method"], "notifications/progress")
        self.assertEqual(self.client.stop(session), {"stopped": True})

    def test_auth_command_allowlist_and_one_desktop_owner(self):
        with self.assertRaisesRegex(BridgeError, "401"):
            BridgeClient(self.url, "wrong").start("cua")
        with self.assertRaisesRegex(BridgeError, "403"):
            self.client.start("cua", [sys.executable, "unapproved.py"])
        with self.assertRaisesRegex(BridgeError, "Unknown configured arm"):
            self.client.start("unconfigured")
        session = self.client.start("cua")
        with self.assertRaisesRegex(BridgeError, "409"):
            self.client.start("cua")
        self.client.stop(session)
        second = self.client.start("cua")
        self.assertNotEqual(session, second)

    def test_mcp_error_does_not_corrupt_session(self):
        session = self.client.start("cua")
        with self.assertRaisesRegex(BridgeError, "invalid arguments") as caught:
            self.client.call(session, "bad")
        self.assertEqual(caught.exception.response["error"]["code"], -32602)
        self.assertEqual(self.client.call(session, "tools/list")["count"], 2)

    def test_timeout_poisons_session_without_replaying_mutation(self):
        session = self.client.start("cua")
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "emissions"
            with self.assertRaisesRegex(BridgeError, "504"):
                self.client.call(session, "stall", {"log": str(log)}, timeout=0.2)
            self.assertEqual(log.read_text(), "mutation\n")
            with self.assertRaisesRegex(BridgeError, "Session failed"):
                self.client.call(session, "tools/list")
        self.client.stop(session)

    def test_stop_delivers_eof_before_termination(self):
        session = self.client.start("cua")
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "exit"
            self.client.call(session, "exit_log", {"log": str(log)})
            self.client.stop(session)
            self.assertEqual(log.read_text(), "clean EOF exit")

    def test_timeout_covers_backpressured_stdin_without_replay(self):
        session = self.client.start("cua")
        self.client.call(session, "stop_reading")
        with self.assertRaisesRegex(BridgeError, "504.*send timeout"):
            self.client.call(session, "tools/call", {"text": "x" * (2 * 1024 * 1024)}, timeout=0.1)
        self.assertIsNotNone(self.bridge.sessions[session].process.poll())
        with self.assertRaisesRegex(BridgeError, "Session failed"):
            self.client.call(session, "tools/list")
        self.client.stop(session)
        self.assertEqual(self.client.health()["active_sessions"], 0)

    def test_stop_interrupts_backpressured_writer(self):
        session_id = self.client.start("cua")
        self.client.call(session_id, "stop_reading")
        session = self.bridge.sessions[session_id]
        writing = threading.Event()
        errors = []
        original_write = session.process.stdin.write

        def write(data):
            writing.set()
            return original_write(data)

        def call():
            try:
                session.call("tools/call", {"text": "x" * (2 * 1024 * 1024)}, 30)
            except Exception as exc:
                errors.append(exc)

        with patch.object(session.process.stdin, "write", side_effect=write):
            worker = threading.Thread(target=call, daemon=True)
            worker.start()
            try:
                self.assertTrue(writing.wait(5))
                self.client.stop(session_id)
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                self.assertTrue(errors)
                self.assertEqual(self.client.health()["active_sessions"], 0)
            finally:
                if session.process.poll() is None:
                    session.process.terminate()
                worker.join(timeout=5)

    def test_child_held_pipes_block_new_session_until_cleanup(self):
        session = self.client.start("cua")
        child = self.client.call(session, "childpipes")["pid"]
        try:
            with self.assertRaisesRegex(BridgeError, "output pipes remain open"):
                self.client.stop(session)
            self.assertIsNotNone(self.bridge.sessions[session].process.poll())
            self.assertIn("output pipes remain open", self.client.health()["failed_sessions"][session])
            with self.assertRaisesRegex(BridgeError, "409"):
                self.client.start("cua")
            with self.assertRaisesRegex(BridgeError, "Session failed"):
                self.client.call(session, "tools/list")
        finally:
            os.kill(child, signal.SIGTERM)
            self.client.stop(session)


if __name__ == "__main__":
    unittest.main()
