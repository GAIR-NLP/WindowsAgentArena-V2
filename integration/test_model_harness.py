import base64
import copy
import contextlib
import datetime
import email.utils
import http.server
import io
import json
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

from model_agent import AzureCliTokenProvider, PhaseDeadline, ResponsesClient, result_content, run_agent
import waa_compare


TOOLS = [{"name": "GUI", "inputSchema": {"type": "object"}, "description": "Desktop"}]


@contextlib.contextmanager
def http_stub(replies, *, gateway_task_id=None, api_key="private-stub-token"):
    requests = []
    request_metadata = []
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            request_metadata.append({"path": self.path, "authorization": self.headers.get("Authorization")})
            status, headers, body = replies[min(len(requests) - 1, len(replies) - 1)]
            payload = json.dumps(body).encode()
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        path = "/v1" if gateway_task_id is None else "/t/" + urllib.parse.quote(gateway_task_id, safe="") + "/v1"
        client = ResponsesClient(f"http://127.0.0.1:{server.server_port}" + path, api_key,
                                 gateway_task_id=gateway_task_id)
        client._test_request_metadata = request_metadata
        yield client, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def response(name="GUI", args=None, status="completed"):
    return {"status": status, "usage": {"input_tokens": 10, "output_tokens": 5}, "output": [
        {"type": "function_call", "call_id": "call1", "name": name,
         "arguments": json.dumps(args or {})}]}


class HarnessTests(unittest.TestCase):
    def test_redirect_is_rejected_before_forwarding_model_context(self):
        with http_stub([(200, {}, response())]) as (destination, forwarded):
            with http_stub([(307, {"Location": destination.url}, {})]) as (client, requests):
                state = run_agent("private task", TOOLS, lambda *_: self.fail("No input after redirect"), client, lambda _: None)
        self.assertEqual(state["error_kind"], "routing")
        self.assertEqual(len(requests), 1)
        self.assertEqual(forwarded, [])

    def test_gateway_headers_are_preserved_outside_model_context(self):
        headers = {"X-Gateway-Endpoint": "endpoint-one", "X-Gateway-Routing": "task-affinity-v1",
                   "X-Gateway-Request-Id": "gateway-request-one"}
        second = dict(headers, **{"X-Gateway-Request-Id": "gateway-request-two"})
        events, calls = [], []
        with http_stub([(200, headers, response()), (200, second, response("finish", {"status": "done"}))],
                       gateway_task_id="waa:run:hcu:task", api_key=None) as (client, requests):
            client.model, client.reasoning = "gpt-5.4", "high"
            result = run_agent("Task", TOOLS, lambda *args: calls.append(args) or {"content": []}, client, events.append)
        self.assertEqual(result["status"], "done")
        self.assertEqual(len(calls), 1)
        self.assertEqual([e["request_id"] for e in result["model_routing_events"]],
                         ["gateway-request-one", "gateway-request-two"])
        self.assertEqual([e["routing"]["endpoint"] for e in events if e["kind"] == "model_response"],
                         ["endpoint-one", "endpoint-one"])
        self.assertEqual(client._test_request_metadata[0],
                         {"path": "/t/waa%3Arun%3Ahcu%3Atask/v1/responses", "authorization": None})
        self.assertEqual(requests[0]["model"], "gpt-5.4")
        self.assertEqual(requests[0]["reasoning"], {"effort": "high"})
        for metadata in ("endpoint-one", "gateway-request-one", "task-affinity-v1", "waa:run:hcu:task"):
            self.assertNotIn(metadata, json.dumps(requests))

    def test_gateway_missing_header_or_wrong_protocol_fails_before_tools(self):
        headers = {"X-Gateway-Endpoint": "endpoint-one", "X-Gateway-Routing": "task-affinity-v1",
                   "X-Gateway-Request-Id": "gateway-request-one"}
        variants = [{k: v for k, v in headers.items() if k != missing} for missing in headers]
        variants.append(dict(headers, **{"X-Gateway-Routing": "round-robin"}))
        for variant in variants:
            events = []
            with http_stub([(200, variant, response())], gateway_task_id="task", api_key=None) as (client, requests):
                result = run_agent("Task", TOOLS, lambda *_: self.fail("Invalid route must not emit input"), client, events.append)
            self.assertEqual(result["error_kind"], "routing")
            self.assertEqual(result["successful_model_responses"], 0)
            self.assertEqual(len(requests), 1)
            self.assertEqual(events[-1]["kind"], "model_routing_error")
            self.assertEqual(events[-1]["routing"], result["error_routing"])

    def test_gateway_endpoint_change_rejects_new_output_and_preserves_evidence(self):
        headers = {"X-Gateway-Endpoint": "endpoint-one", "X-Gateway-Routing": "task-affinity-v1",
                   "X-Gateway-Request-Id": "gateway-request-one"}
        changed = dict(headers, **{"X-Gateway-Endpoint": "endpoint-two", "X-Gateway-Request-Id": "gateway-request-two"})
        calls, events = [], []
        with http_stub([(200, headers, response()), (200, changed, response())], gateway_task_id="task", api_key=None) as (client, requests):
            result = run_agent("Task", TOOLS, lambda *args: calls.append(args) or {"content": []}, client, events.append)
        self.assertEqual(len(calls), 1)
        self.assertEqual((result["model_requests"], result["successful_model_responses"]), (2, 1))
        self.assertEqual(result["error_kind"], "routing")
        self.assertEqual(result["error_routing"]["expected_endpoint"], "endpoint-one")
        self.assertEqual(result["error_routing"]["endpoint"], "endpoint-two")
        self.assertEqual(events[-1]["routing"]["request_id"], "gateway-request-two")

    def test_gateway_route_must_match_explicit_task_before_http(self):
        for base in ("http://example.test/v1", "http://example.test/t/other/v1",
                     "http://example.test/t/task/v1?redirect=1", "http://example.test/t/task/v1#fragment"):
            with self.assertRaises(ValueError):
                ResponsesClient(base, gateway_task_id="task")

    def test_gateway_429_fallback_accepts_affinity_headers_on_success(self):
        headers = {"X-Gateway-Endpoint": "endpoint-one", "X-Gateway-Routing": "task-affinity-v1",
                   "X-Gateway-Request-Id": "gateway-request"}
        with http_stub([(429, {}, {"error": {"code": "rate_limit_exceeded"}}),
                        (200, headers, response("finish", {"status": "done"}))], gateway_task_id="task", api_key=None) as (client, requests):
            result = run_agent("Task", TOOLS, lambda *_: None, client, lambda _: None, sleep=lambda _: None)
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["model_requests"], 2)
        self.assertEqual(len(result["model_routing_events"]), 1)

    def test_legacy_mode_still_requires_credentials(self):
        result = run_agent("Task", TOOLS, lambda *_: None, ResponsesClient("http://unused/v1"), lambda _: None)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["model_requests"], 0)
        self.assertIn("credentials", result["error"])

    def test_real_http_429_retry_after_does_not_replay_tools(self):
        limited = (429, {"Retry-After": "0.05", "x-request-id": "request-test"},
                   {"error": {"code": "rate_limit_exceeded", "message": "Token rate limit exceeded"}})
        replies = [limited, (200, {}, response()), limited,
                   (200, {}, response("finish", {"status": "done"}))]
        now, waits, calls, events = [0.0], [], [], []
        def wait(delay):
            waits.append(delay)
            now[0] += delay
        with http_stub(replies) as (client, requests):
            result = run_agent("Task", TOOLS, lambda *args: calls.append(args) or {"content": []},
                               client, events.append, max_responses=2, clock=lambda: now[0], sleep=wait,
                               rate_limit_base_delay=0.001, rate_limit_max_delay=0.01)
        self.assertEqual(result["status"], "done")
        self.assertEqual((result["model_requests"], result["successful_model_responses"], len(calls)), (4, 2, 1))
        self.assertEqual(waits, [0.05, 0.05])
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(requests[2], requests[3])
        self.assertNotEqual(requests[1]["input"], requests[2]["input"])
        rejected = [e for e in events if e["kind"] == "model_rate_limit"]
        self.assertEqual([e["model_request"] for e in rejected], [1, 3])
        self.assertEqual(rejected[0]["request_id"], "request-test")
        self.assertEqual(rejected[0]["http_status"], 429)

    def test_real_http_429_exhaustion_is_bounded_and_structured(self):
        now, waits = [0.0], []
        def wait(delay):
            waits.append(delay)
            now[0] += delay
        with http_stub([(429, {}, {"error": {"message": "Too many requests"}})]) as (client, requests):
            result = run_agent("Task", TOOLS, lambda *_: self.fail("No tools expected"), client, lambda _: None,
                               clock=lambda: now[0], sleep=wait, rate_limit_max_retries=3)
        self.assertEqual(len(requests), 4)
        self.assertEqual(waits, [10.0, 20.0, 40.0])
        self.assertEqual(result["model_requests"], 4)
        self.assertEqual(result["successful_model_responses"], 0)
        self.assertEqual((result["error_kind"], result["rate_limit_reason"]), ("rate_limit", "retry_exhausted"))

    def test_real_http_retry_after_cannot_outlive_task_deadline(self):
        with http_stub([(429, {"Retry-After": "20"}, {"error": {"code": "rate_limit_exceeded"}})]) as (client, requests):
            result = run_agent("Task", TOOLS, lambda *_: self.fail("No tools expected"), client, lambda _: None,
                               timeout=10, sleep=lambda _: self.fail("Cannot sleep past deadline"))
        self.assertEqual(len(requests), 1)
        self.assertEqual((result["status"], result["error_kind"], result["rate_limit_reason"]),
                         ("timeout", "rate_limit", "deadline"))

    def test_real_http_retry_after_date_and_invalid_value(self):
        for header in (email.utils.formatdate(time.time() + 60, usegmt=True), "invalid"):
            now, waits = [0.0], []
            def wait(delay):
                waits.append(delay)
                now[0] += delay
            with http_stub([(429, {"Retry-After": header}, {"error": {"message": "Rate limit"}}),
                            (200, {}, response("finish", {"status": "done"}))]) as (client, requests):
                result = run_agent("Task", TOOLS, lambda *_: None, client, lambda _: None,
                                   clock=lambda: now[0], sleep=wait)
            self.assertEqual(result["status"], "done")
            self.assertEqual(len(requests), 2)
            if header == "invalid":
                self.assertEqual(waits, [10.0])
            else:
                self.assertGreater(waits[0], 55)
                self.assertLessEqual(waits[0], 60)

    def test_real_http_quota_exhaustion_stops_and_redacts_evidence(self):
        body = {"error": {"code": "insufficient_quota", "type": "quota",
                          "message": "Exceeded your current quota private-stub-token Bearer other-secret api_key=another-secret"},
                "unrelated": "private unrelated response data"}
        with http_stub([(429, {"apim-request-id": "azure-request"}, body)]) as (client, requests):
            result = run_agent("Task", TOOLS, lambda *_: None, client, lambda _: None,
                               sleep=lambda _: self.fail("Quota errors are terminal"))
        self.assertEqual(len(requests), 1)
        self.assertEqual(result["rate_limit_reason"], "quota_exhausted")
        evidence = json.dumps(result["rate_limit_events"])
        for secret in ("private-stub-token", "other-secret", "another-secret", "private unrelated response data"):
            self.assertNotIn(secret, evidence)
        self.assertEqual(result["rate_limit_events"][0]["request_id"], "azure-request")

    def test_real_http_non429_error_is_not_retried(self):
        with http_stub([(503, {"Retry-After": "1"}, {"error": {"message": "Unavailable"}})]) as (client, requests):
            result = run_agent("Task", TOOLS, lambda *_: None, client, lambda _: None)
        self.assertEqual(len(requests), 1)
        self.assertEqual(result["model_requests"], 1)
        self.assertEqual(result["rate_limit_events"], [])

    def test_deadline_during_backoff_preserves_rate_limit_kind(self):
        def wait(_):
            raise PhaseDeadline("task deadline")
        with http_stub([(429, {}, {"error": {"message": "Rate limit"}})]) as (client, requests):
            result = run_agent("Task", TOOLS, lambda *_: None, client, lambda _: None, sleep=wait)
        self.assertEqual(len(requests), 1)
        self.assertEqual((result["status"], result["error_kind"], result["rate_limit_reason"]),
                         ("timeout", "rate_limit", "deadline"))

    def test_interruption_preserves_completed_response_usage(self):
        for error, kind in ((KeyboardInterrupt("operator SIGINT"), "interrupted"), (SystemExit("cancel"), "cancelled")):
            def invoke(*_):
                raise error
            result = run_agent("Task", TOOLS, invoke, lambda *_: response(), lambda _: None)
            self.assertEqual((result["status"], result["error_kind"], result["interrupted_phase"]), (kind, kind, "agent"))
            self.assertEqual(result["successful_model_responses"], 1)
            self.assertEqual(len(result["usage"]), 1)

    def test_auth_failure_does_not_count_as_http_attempt(self):
        def credentials(**_):
            raise RuntimeError("authentication failed")
        result = run_agent("Task", TOOLS, lambda *_: None,
                           ResponsesClient("http://unused", token_provider=credentials), lambda _: None)
        self.assertEqual(result["model_requests"], 0)

    def test_azure_token_reused_then_refreshed_before_expiry(self):
        now = [1000]
        provider = AzureCliTokenProvider(clock=lambda: now[0])
        first = subprocess.CompletedProcess([], 0, json.dumps({"accessToken": "token-one", "expires_on": "2000"}))
        second = subprocess.CompletedProcess([], 0, json.dumps({"accessToken": "token-two", "expires_on": "5000"}))
        with patch("model_agent.subprocess.run", side_effect=[first, second]) as run:
            self.assertEqual(provider(), "token-one")
            now[0] = 1699
            self.assertEqual(provider(), "token-one")
            self.assertEqual(run.call_count, 1)
            now[0] = 1700
            self.assertEqual(provider(), "token-two")
            self.assertEqual(run.call_count, 2)

    def test_azure_legacy_expiry_and_failed_refresh_do_not_leak_token(self):
        now = [1000]
        provider = AzureCliTokenProvider(clock=lambda: now[0])
        expires = datetime.datetime.fromtimestamp(2000, datetime.timezone.utc).isoformat()
        first = subprocess.CompletedProcess([], 0, json.dumps({"accessToken": "private-token", "expiresOn": expires}))
        with patch("model_agent.subprocess.run", return_value=first):
            self.assertEqual(provider(), "private-token")
        now[0] = 1800
        failure = subprocess.CompletedProcess([], 1, "private-token", "private-token")
        with patch("model_agent.subprocess.run", return_value=failure):
            with self.assertRaises(RuntimeError) as raised:
                provider()
        self.assertNotIn("private-token", str(raised.exception))

    def test_responses_client_uses_refreshed_token_and_does_not_retry_401(self):
        provider = unittest.mock.Mock(return_value="fresh-token")
        client = ResponsesClient("https://example.test/v1", "old-token", token_provider=provider)
        def fail(request, **kwargs):
            self.assertEqual(request.get_header("Authorization"), "Bearer fresh-token")
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))
        with patch.object(client, "open", side_effect=fail) as request:
            with self.assertRaises(urllib.error.HTTPError):
                client([], [], 10)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(request.call_count, 1)

    def test_response_budget_executes_last_response_then_stops(self):
        calls, requests = [], []
        def model(*args):
            requests.append(1)
            return response()
        result = run_agent("Task", TOOLS, lambda *args: calls.append(args) or {"content": []},
                           model, lambda _: None, max_responses=2)
        self.assertEqual((len(calls), len(requests)), (2, 2))
        self.assertEqual(result["status"], "response_budget")
        self.assertEqual(result["successful_model_responses"], 2)

    def test_response_arriving_after_deadline_emits_no_action(self):
        now, calls = [0], []
        def model(*args):
            now[0] = 11
            return response()
        result = run_agent("Task", TOOLS, lambda *args: calls.append(args), model,
                           lambda _: None, timeout=10, clock=lambda: now[0])
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["successful_model_responses"], 1)
        self.assertEqual(calls, [])

    def test_transport_failure_is_not_retried_or_counted(self):
        requests = []
        def model(*args):
            requests.append(1)
            raise ConnectionError("disconnected")
        result = run_agent("Task", TOOLS, lambda *args: None, model, lambda _: None)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["successful_model_responses"], 0)
        self.assertEqual(len(requests), 1)

    def test_tool_timeout_preserves_successful_model_count(self):
        def invoke(*args):
            raise TimeoutError("input delivery unknown")
        result = run_agent("Task", TOOLS, invoke, lambda *args: response(), lambda _: None)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["successful_model_responses"], 1)

    def test_entire_model_batch_validated_before_first_input(self):
        value = response()
        value["output"].extend(response("unknown")["output"])
        calls = []
        result = run_agent("Task", TOOLS, lambda *args: calls.append(args),
                           lambda *args: value, lambda _: None)
        self.assertEqual(calls, [])
        self.assertEqual(result["status"], "error")

    def test_incomplete_model_output_never_executes(self):
        calls = []
        result = run_agent("Task", TOOLS, lambda *args: calls.append(args),
                           lambda *args: response(status="incomplete"), lambda _: None)
        self.assertEqual(calls, [])
        self.assertEqual(result["status"], "model_incomplete")
        self.assertEqual(result["successful_model_responses"], 0)
        self.assertEqual(result["received_model_responses"], 1)
        self.assertEqual(result["model_requests"], 1)

    def test_infeasible_is_explicit_and_distinct_from_failure(self):
        for expected in ["done", "infeasible", "failed"]:
            result = run_agent("Task", TOOLS, lambda *args: None,
                               lambda *args: response("finish", {"status": expected}), lambda _: None)
            self.assertEqual(result["status"], expected)

    def test_mcp_images_are_delivered_as_images_with_tool_association(self):
        data = base64.b64encode(b"png bytes").decode()
        original = {"isError": False, "content": [{"type": "text", "text": "1280x720"},
                    {"type": "image", "mimeType": "image/png", "data": data}],
                    "structuredContent": {"screen": [1280, 720]}}
        before, inputs = copy.deepcopy(original), []
        def model(messages, *args):
            inputs.append(copy.deepcopy(messages))
            return response() if len(inputs) == 1 else response("finish", {"status": "done"})
        result = run_agent("Task", TOOLS, lambda *args: original, model, lambda _: None)
        self.assertEqual(result["status"], "done")
        self.assertEqual(original, before)
        output, images = inputs[1][-2:]
        self.assertEqual(output["type"], "function_call_output")
        self.assertNotIn(data, output["output"])
        self.assertIn("structuredContent", output["output"])
        self.assertEqual(images["content"][1]["image_url"], "data:image/png;base64," + data)

    def test_corrupt_image_fails_instead_of_silently_omitting_evidence(self):
        with self.assertRaises(ValueError):
            result_content({"content": [{"type": "image", "mimeType": "image/png", "data": "%%%"}]})

    def test_selection_rejects_duplicates_and_path_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "examples" / "app").mkdir(parents=True)
            (root / "examples" / "app" / "id.json").write_text('{"id":"id"}')
            selection = root / "selection.json"
            with patch.object(waa_compare, "TASKS", root):
                selection.write_text('{"app":["id"]}')
                self.assertEqual(waa_compare.read_selection(selection)[0][1]["id"], "id")
                (root / "examples" / "app" / "id.json").write_text('{"id":"internal-id"}')
                task = waa_compare.read_selection(selection)[0][1]
                self.assertEqual((task["_selection_id"], task["id"]), ("id", "internal-id"))
                for invalid in ['{"app":["id","id"]}', '{"../app":["id"]}', '{}']:
                    selection.write_text(invalid)
                    with self.assertRaises(ValueError):
                        waa_compare.read_selection(selection)

    def test_phase_deadline_restores_signal_handler(self):
        previous = signal.getsignal(signal.SIGALRM)
        with self.assertRaises(PhaseDeadline):
            with waa_compare.deadline(10):
                signal.raise_signal(signal.SIGALRM)
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)

    def test_vm_cleanup_runs_after_scoring_evidence_is_saved(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            result = {"status": "done", "evaluator_score": 0.5, "evaluator_status": "completed"}
            def destroy(*args, **kwargs):
                saved = json.loads((folder / "result.json").read_text())
                self.assertEqual(saved["evaluator_score"], 0.5)
                self.assertEqual(saved["vm_cleanup_status"], "pending")
                self.assertTrue(saved["evidence_sealed_at"])
            with patch.object(waa_compare.subprocess, "run", side_effect=destroy) as run:
                waa_compare.cleanup_episode(folder, result, ["vm-owner", "destroy", "episode-1"], 20)
            run.assert_called_once()
            self.assertEqual(json.loads((folder / "result.json").read_text())["vm_cleanup_status"], "completed")

    def test_vm_cleanup_failure_does_not_replace_task_or_score(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            result = {"status": "timeout", "evaluator_score": 0.25, "evaluator_status": "completed"}
            with patch.object(waa_compare.subprocess, "run", side_effect=subprocess.TimeoutExpired("destroy", 2)):
                waa_compare.cleanup_episode(folder, result, ["vm-owner", "destroy"], 2)
            saved = json.loads((folder / "result.json").read_text())
            self.assertEqual((saved["status"], saved["evaluator_score"]), ("timeout", 0.25))
            self.assertEqual(saved["vm_cleanup_status"], "error")
            self.assertIn("TimeoutExpired", saved["vm_cleanup_error"])


if __name__ == "__main__":
    unittest.main()
