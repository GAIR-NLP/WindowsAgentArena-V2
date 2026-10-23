"""Run isolated WAA-V2 episodes against HCU or CUA with one shared model loop."""
from __future__ import annotations

import argparse
import contextlib
import datetime
import importlib.metadata
import json
import logging
import math
import os
import re
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.parse

from model_agent import AzureCliTokenProvider, PhaseDeadline, ResponsesClient, run_agent
from artifact_export import export_phase
from gateway_config import load_gateway_config, validate_model_url

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "src/win-arena-container/client"
TASKS = CLIENT / "evaluation_examples_windows"


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


@contextlib.contextmanager
def setup_logging(path):
    logger = logging.getLogger("desktopenv.setup")
    previous_level = logger.level
    with contextlib.closing(logging.FileHandler(path, encoding="utf-8")) as handler:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            yield
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)


def gateway_episode_route(base_url, run_id, arm, task_id):
    identity = json.dumps(["waa", run_id, arm, task_id], separators=(",", ":"))
    base = urllib.parse.urlsplit(base_url)
    path = "/t/" + urllib.parse.quote(identity, safe="") + "/v1"
    return urllib.parse.urlunsplit((base.scheme, base.netloc, path, "", "")), identity


def read_selection(path):
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError("Task selection must map domains to task ID lists")
    selected = []
    for domain, ids in data.items():
        if not isinstance(ids, list):
            raise ValueError("Domain selection must be a list")
        for task_id in ids:
            if any(not isinstance(value, str) or value in {".", ".."}
                   or not re.fullmatch(r"[A-Za-z0-9_.-]+", value) for value in (domain, task_id)):
                raise ValueError("Invalid domain/task identifier")
            filename = TASKS / "examples" / domain / (task_id + ".json")
            task = json.loads(filename.read_text())
            # Four upstream files use an internal id different from test_all's filename.
            # Keep evaluator metadata intact and bind reporting to the selected filename.
            task["_selection_id"] = task_id
            selected.append((domain, task))
    if not selected or len({task["_selection_id"] for _, task in selected}) != len(selected):
        raise ValueError("Selection must be nonempty without duplicate task IDs")
    return selected


@contextlib.contextmanager
def deadline(seconds):
    """Bound upstream HTTP calls that do not declare their own timeouts (Linux host)."""
    def expired(*_):
        raise PhaseDeadline("Episode phase exceeded its wall-clock deadline")
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def make_env(guest_url, cache, upstream_host=None):
    sys.path.insert(0, str(CLIENT))
    from desktop_env.envs.desktop_env import DesktopEnv
    from desktop_env.controllers.python import PythonController
    from desktop_env.controllers.setup import SetupController

    class AttachedEnv(DesktopEnv):
        def __init__(self):
            # QMP belongs to the external VM owner. Keep upstream setup and scoring intact.
            self.vm_ip = upstream_host or urllib.parse.urlparse(guest_url).hostname
            self.cache_dir_base = str(cache)
            self.controller = PythonController(self.vm_ip)
            self.controller.http_server = guest_url.rstrip("/")
            self.setup_controller = SetupController(self.vm_ip, str(cache))
            self.setup_controller.http_server = guest_url.rstrip("/")
            self.setup_controller.http_server_setup_root = guest_url.rstrip("/") + "/setup"
            self.action_history = []

        def _get_screenshot(self):
            return self.controller.get_screenshot()

    return AttachedEnv()


def prepare_env(env, task):
    env._set_task_info(task)
    env.setup_controller.reset_cache_dir(env.cache_dir)
    env.setup_controller.setup(env.config)


def cleanup_episode(folder, result, command, timeout):
    """Record scoring evidence and clean the owned VM even if evidence writes fail."""
    result["evidence_sealed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    result["vm_cleanup_status"] = "pending" if command else "external_owner"
    evidence_errors = []
    try:
        write_json(folder / "result.json", result)
    except Exception as exc:
        evidence_errors.append(exc)
    if command:
        try:
            if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg for arg in command):
                raise ValueError("cleanup_command must be a nonempty argv list")
            # A full evidence disk must not prevent the VM owner from cleaning up.
            with contextlib.ExitStack() as stack:
                try:
                    stream = stack.enter_context((folder / "cleanup.log").open("w"))
                except OSError as exc:
                    evidence_errors.append(exc)
                    stream = sys.stderr
                subprocess.run(command, check=True, timeout=timeout, stdout=stream, stderr=subprocess.STDOUT)
            result["vm_cleanup_status"] = "completed"
        except Exception as exc:
            result.update(vm_cleanup_status="error", vm_cleanup_error=f"{type(exc).__name__}: {exc}")
    if evidence_errors:
        result["evidence_write_errors"] = [f"{type(exc).__name__}: {exc}" for exc in evidence_errors]
    try:
        write_json(folder / "result.json", result)
    except Exception as exc:
        evidence_errors.append(exc)
        result["evidence_write_errors"] = [f"{type(err).__name__}: {err}" for err in evidence_errors]
    if evidence_errors:
        raise RuntimeError("Episode evidence write failed; VM cleanup status: "
                           + result["vm_cleanup_status"] + "; "
                           + result.get("vm_cleanup_error", "") + "; "
                           + "; ".join(result["evidence_write_errors"])) from evidence_errors[0]


def run_reset_hook(command, folder, timeout, cleanup_timeout):
    """Let the VM owner finish its cleanup before terminating a stalled reset tree."""
    evidence = {"status": "starting", "timeout_seconds": timeout, "cleanup_grace_seconds": cleanup_timeout,
                "progress_record": str(folder / "reset-episode.json")}
    record = folder / "reset-process.json"
    environment = dict(os.environ, WAA_RESET_RECORD_FILE=evidence["progress_record"])
    started = time.monotonic()

    def record_progress(primary_error=None):
        try:
            write_json(record, evidence)
        except Exception as exc:
            detail = f"Reset progress write failed: {type(exc).__name__}: {exc}"
            evidence.setdefault("evidence_write_errors", []).append(detail)
            if primary_error is None:
                raise
            primary_error.add_note(detail)

    with (folder / "reset.log").open("w") as errors, (folder / "reset.stdout").open("w") as output:
        process = subprocess.Popen(command, stdout=output, stderr=errors, text=True,
                                   start_new_session=True, env=environment)
        evidence.update(status="running", pid=process.pid)
        try:
            record_progress()
            process.wait(timeout=timeout)
        except BaseException as exc:
            evidence.update(status="terminating", reason=type(exc).__name__, sigterm_sent=True)
            record_progress(exc)
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=cleanup_timeout)
                evidence["status"] = "terminated_after_cleanup_grace"
            except subprocess.TimeoutExpired:
                evidence.update(status="forced_kill", sigkill_sent=True)
                record_progress(exc)
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=10)
            if isinstance(exc, subprocess.TimeoutExpired):
                raise TimeoutError("VM reset timed out; termination and cleanup evidence is in reset-process.json") from exc
            raise
        else:
            evidence["status"] = "completed" if process.returncode == 0 else "failed"
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, command)
        finally:
            evidence.update(returncode=process.poll(), elapsed_seconds=time.monotonic() - started)
            # A final persistence failure must not hide the original reset error.
            record_progress(sys.exc_info()[1])
    return (folder / "reset.stdout").read_text()


def run_episode(args, arm, driver, domain, task, folder):
    from bridge_client import BridgeClient
    folder.mkdir(parents=True, exist_ok=False)
    result = {"arm": arm, "task_id": task["_selection_id"], "source_task_id": task["id"], "domain": domain,
              "status": "setup_error", "evaluator_score": None, "evaluator_status": "not_run"}
    bridge, session, env = None, None, None
    guest_url, bridge_url = args.guest_url, args.bridge_url
    upstream_host = args.upstream_host
    cleanup_command = json.loads(args.cleanup_command) if args.cleanup_command else None
    phase = "reset" if args.reset_command else "setup"
    setup_completed = False

    def record_interruption(exc, interrupted_phase):
        status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "cancelled"
        result.setdefault("status_before_interruption", result["status"])
        if result.get("error") and "error_before_interruption" not in result:
            result["error_before_interruption"] = result["error"]
            result["error_kind_before_interruption"] = result.get("error_kind")
        result.setdefault("interrupted_phase", interrupted_phase)
        result.update(status=status, error_kind=status,
                      error=f"{type(exc).__name__}: {str(exc) or 'Episode interrupted'}")
        if getattr(exc, "__notes__", None):
            result["error_notes"] = exc.__notes__
    def log(event):
        with (folder / "trace.jsonl").open("a") as stream:
            stream.write(json.dumps({"time": datetime.datetime.now(datetime.timezone.utc).isoformat(), **event}) + "\n")
    def preserve_artifacts(stage):
        nonlocal phase
        phase = "artifact_export_" + stage
        try:
            report = export_phase(env.controller, task, folder,
                {"run_id": getattr(args, "run_id", None), "arm": arm,
                 "task_id": task["_selection_id"], "agent_status": result["status"],
                 "evaluator_status_at_capture": result["evaluator_status"]}, stage, deadline,
                state_stability="unstable_cleanup_failed" if result.get("mcp_cleanup_error")
                else "agent_stopped_not_application_quiescence_verified")
            result.setdefault("artifact_exports", []).append(report)
        except (Exception, PhaseDeadline) as exc:
            result.setdefault("artifact_exports", []).append(
                {"stage": stage, "status": "error", "error": f"{type(exc).__name__}: {exc}"})
        except BaseException as exc:
            result.setdefault("artifact_exports", []).append(
                getattr(exc, "artifact_export_report", None) or
                {"stage": stage, "status": "interrupted", "error": f"{type(exc).__name__}: {exc}"})
            record_interruption(exc, phase)
    try:
        write_json(folder / "task.json", task)
        if args.reset_command:
            command = json.loads(args.reset_command)
            if not isinstance(command, list) or not command or not all(isinstance(s, str) for s in command):
                raise ValueError("reset-command must be a nonempty JSON argv list")
            command = [s.replace("{arm}", arm).replace("{run_id}", args.run_id).replace("{task_id}", task["_selection_id"]) for s in command]
            try:
                reset_output = run_reset_hook(command, folder, args.setup_timeout, args.reset_cleanup_timeout)
            finally:
                # vm.py mirrors ownership before creating the container. Recover cleanup
                # even when timeout prevents the final stdout binding from arriving.
                reset_error = sys.exc_info()[1]
                try:
                    progress = folder / "reset-episode.json"
                    if progress.exists():
                        partial_binding = json.loads(progress.read_text())
                        cleanup_command = partial_binding.get("cleanup_command", cleanup_command)
                        result["episode_id"] = partial_binding.get("episode_id")
                        write_json(folder / "episode.json", partial_binding)
                    if (folder / "reset-process.json").exists():
                        result["reset_process"] = json.loads((folder / "reset-process.json").read_text())
                except Exception as exc:
                    if reset_error is None:
                        raise
                    reset_error.add_note(f"Reset evidence recovery failed: {type(exc).__name__}: {exc}")
            binding = json.loads(reset_output)
            cleanup_command = binding.get("cleanup_command", cleanup_command)
            if not cleanup_command:
                raise ValueError("Reset hook must return cleanup_command to destroy its VM")
            if not all(binding.get(k) for k in ["controller_url", "bridge_url", "episode_id"]):
                raise ValueError("Reset hook must return controller_url, bridge_url and episode_id")
            guest_url, bridge_url = binding["controller_url"], binding["bridge_url"]
            upstream_host = binding.get("vm_ip", upstream_host)
            result["episode_id"] = binding["episode_id"]
            write_json(folder / "episode.json", binding)
        phase = "setup"
        with setup_logging(folder / "setup.log"), deadline(args.setup_timeout):
            env = make_env(guest_url, folder / "cache", upstream_host)
            prepare_env(env, task)
            time.sleep(args.setup_settle)
            initial = env.controller.get_screenshot()
            if not initial:
                raise RuntimeError("Guest returned no initial screenshot")
            (folder / "initial.png").write_bytes(initial)
        setup_completed = True
        phase = "driver_start"
        result["status"] = "driver_error"
        bridge = BridgeClient(bridge_url, os.environ["WAA_BRIDGE_TOKEN"])
        session = bridge.start(arm, driver["command"])
        powershell_runtime = bridge.shell_runtime(session)
        result["powershell_runtime"] = powershell_runtime
        write_json(folder / "powershell-runtime.json", powershell_runtime)
        initialize = bridge.call(session, "initialize", {"protocolVersion": "2024-11-05",
            "capabilities": {}, "clientInfo": {"name": "waa-compare", "version": "0.1"}})
        bridge.call(session, "notifications/initialized", {})
        tool_list = bridge.call(session, "tools/list", {})
        if tool_list.get("nextCursor"):
            raise RuntimeError("Paginated tool inventory is not supported")
        write_json(folder / "mcp.json", {"initialize": initialize, "tools": tool_list})

        def invoke(name, arguments, remaining):
            if name == "powershell":
                if set(arguments) != {"script"} or not isinstance(arguments["script"], str):
                    return {"isError": True, "content": [{"type": "text", "text": "Expected script string"}]}
                if remaining <= 10:
                    raise TimeoutError("Insufficient time for PowerShell execution and cleanup")
                output = bridge.powershell(session, arguments["script"], timeout=min(120, remaining - 10))
                return {"isError": output.get("returncode") != 0 or output.get("timed_out") or output.get("cancelled"),
                        "content": [{"type": "text", "text": json.dumps(output)}]}
            return bridge.call(session, "tools/call", {"name": name, "arguments": arguments}, timeout=min(120, remaining))

        gateway_task_id = None
        gateway = getattr(args, "gateway", None)
        model_base_url = gateway["base_url"] if gateway else os.environ["OPENAI_BASE_URL"]
        if gateway:
            model_base_url, gateway_task_id = gateway_episode_route(model_base_url, args.run_id, arm, task["_selection_id"])
            result["model_route"] = {"gateway_task_id": gateway_task_id, "base_url": model_base_url,
                                     "routing_revision": gateway["routing_revision"]}
        model = ResponsesClient(model_base_url, None if gateway and gateway["authentication"] == "none" else os.environ.get("OPENAI_API_KEY"),
                                args.model, args.reasoning, initialize.get("instructions", ""),
                                token_provider=None if gateway_task_id else getattr(args, "model_token_provider", None),
                                gateway_task_id=gateway_task_id,
                                expected_routing=gateway["routing_revision"] if gateway else "task-affinity-v1",
                                routing_headers=gateway["headers"] if gateway else None)
        result["status"] = "error"
        phase = "agent"
        with deadline(args.timeout):
            agent = run_agent(task["instruction"], tool_list["tools"], invoke, model, log,
                              max_responses=args.max_responses, timeout=args.timeout,
                              rate_limit_max_retries=getattr(args, "rate_limit_max_retries", 3),
                              rate_limit_base_delay=getattr(args, "rate_limit_base_delay", 10.0),
                              rate_limit_max_delay=getattr(args, "rate_limit_max_delay", 60.0),
                              powershell_runtime=powershell_runtime)
        if agent["status"] in {"interrupted", "cancelled"}:
            agent.setdefault("status_before_interruption", result["status"])
        result.update(agent)
        if result["status"] not in {"interrupted", "cancelled"}:
            env.action_history.append("FAIL" if result["status"] == "infeasible" else "DONE")
    except (TimeoutError, PhaseDeadline) as exc:
        result.update(status="timeout" if result["status"] == "error" else result["status"], error=str(exc))
        if getattr(exc, "__notes__", None):
            result["error_notes"] = exc.__notes__
    except Exception as exc:
        result.update(error=f"{type(exc).__name__}: {exc}")
        if getattr(exc, "__notes__", None):
            result["error_notes"] = exc.__notes__
    except BaseException as exc:
        record_interruption(exc, phase)
    finally:
        try:
            phase = "mcp_cleanup"
            if bridge and session:
                try:
                    bridge.stop(session)
                except Exception as exc:
                    result["mcp_cleanup_error"] = str(exc)
                except BaseException as exc:
                    record_interruption(exc, phase)
                    result["mcp_cleanup_error"] = f"{type(exc).__name__}: MCP cleanup interrupted"
            if env and setup_completed:
                preserve_artifacts("pre-evaluator")
            # Operator cancellation stops further scoring work. Other terminal
            # outcomes retain the upstream partial-state score separately.
            if result["status"] in {"interrupted", "cancelled"}:
                result["evaluator_status"] = "not_run_interrupted"
            elif result.get("mcp_cleanup_error"):
                result.update(evaluator_status="blocked_cleanup", evaluator_error="Guest process cleanup failed; state may still be changing")
            elif env and setup_completed:
                phase = "evaluator"
                try:
                    with setup_logging(folder / "postconfig.log"), deadline(args.evaluator_timeout):
                        result["evaluator_score"] = float(env.evaluate())
                        result["evaluator_status"] = "completed"
                except (Exception, PhaseDeadline) as exc:
                    result.update(evaluator_status="error", evaluator_score=None,
                                  evaluator_error=f"{type(exc).__name__}: {exc}")
                except BaseException as exc:
                    record_interruption(exc, phase)
                    result["evaluator_status"] = "interrupted"
                finally:
                    preserve_artifacts("post-evaluator")
        finally:
            phase = "vm_cleanup"
            # A further Ctrl-C records cancellation but lets the bounded owner
            # cleanup finish. Restore the caller's handler before returning.
            previous_handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
            def cleanup_interrupted(signum, _frame):
                record_interruption(KeyboardInterrupt() if signum == signal.SIGINT else SystemExit(143), phase)
            for signum in previous_handlers:
                signal.signal(signum, cleanup_interrupted)
            try:
                cleanup_episode(folder, result, cleanup_command, args.cleanup_timeout)
            finally:
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-list", required=True, type=Path)
    parser.add_argument("--arm", choices=["hcu", "cua", "both"], default="both")
    parser.add_argument("--drivers", required=True, type=Path, help="JSON: each arm has version and command argv")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--baseline-id", required=True)
    parser.add_argument("--guest-url", default="http://127.0.0.1:5000")
    parser.add_argument("--upstream-host", help="Guest address reachable on upstream's original ports 5000, 9222 and 8080")
    parser.add_argument("--bridge-url", default="http://127.0.0.1:5001")
    parser.add_argument("--reset-command", help="Host argv JSON with {arm}/{run_id}/{task_id}; restores baseline and prints episode JSON")
    parser.add_argument("--prepared-guest", action="store_true", help="Diagnostic single episode only; owner already restored baseline")
    parser.add_argument("--cleanup-command", help="Host cleanup argv JSON; reset hook cleanup_command takes precedence")
    parser.add_argument("--cleanup-timeout", type=float, default=180)
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--reasoning", default="xhigh")
    parser.add_argument("--gateway-config", type=Path, help="Explicit task-affinity gateway JSON; credentials remain in the environment")
    parser.add_argument("--max-responses", type=int, default=100)
    parser.add_argument("--rate-limit-max-retries", type=int, default=3)
    parser.add_argument("--rate-limit-base-delay", type=float, default=10.0)
    parser.add_argument("--rate-limit-max-delay", type=float, default=60.0)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--setup-timeout", type=float, default=600)
    parser.add_argument("--reset-cleanup-timeout", type=float, default=360, help="Grace for reset to clean its VM after SIGTERM")
    parser.add_argument("--evaluator-timeout", type=float, default=300)
    parser.add_argument("--setup-settle", type=float, default=60)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.run_id.strip() or not args.baseline_id.strip() or not args.model.strip() or not args.reasoning.strip():
        parser.error("Run, baseline, model, and reasoning identifiers must be nonempty")
    azure_auth = os.environ.get("WAA_AZURE_CLI_AUTH") == "1"
    try:
        args.gateway = load_gateway_config(args.gateway_config) if args.gateway_config else None
        if not args.gateway and os.environ.get("OPENAI_BASE_URL"):
            validate_model_url(os.environ["OPENAI_BASE_URL"])
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.gateway and azure_auth:
        parser.error("Gateway configuration and Azure CLI authentication are mutually exclusive")
    selected = read_selection(args.task_list)
    arms = ["hcu", "cua"] if args.arm == "both" else [args.arm]
    drivers = json.loads(args.drivers.read_text())
    for arm in arms:
        if (not isinstance(drivers, dict) or not isinstance(drivers.get(arm), dict)
                or not isinstance(drivers[arm].get("version"), str) or not drivers[arm]["version"].strip()
                or not drivers[arm].get("command")):
            parser.error("Every selected driver requires version and command")
        command = drivers[arm]["command"]
        if not isinstance(command, list) or not all(isinstance(item, str) and item for item in command):
            parser.error("Driver command must be a nonempty argv list")
    if any(not math.isfinite(x) or x <= 0 for x in [args.max_responses, args.timeout, args.setup_timeout, args.evaluator_timeout, args.cleanup_timeout, args.reset_cleanup_timeout]) or not math.isfinite(args.setup_settle) or args.setup_settle < 0:
        parser.error("Budgets must be positive and setup-settle nonnegative")
    if (args.rate_limit_max_retries < 0 or not math.isfinite(args.rate_limit_base_delay)
            or not math.isfinite(args.rate_limit_max_delay) or args.rate_limit_base_delay <= 0
            or args.rate_limit_max_delay < args.rate_limit_base_delay):
        parser.error("Rate-limit retries must be nonnegative; finite delays require max-delay >= base-delay > 0")
    if not args.reset_command and not (args.prepared_guest and len(selected) * len(arms) == 1):
        parser.error("Require reset-command, or prepared-guest for a single diagnostic episode")
    if args.cleanup_command:
        try:
            cleanup = json.loads(args.cleanup_command)
        except json.JSONDecodeError:
            parser.error("cleanup-command must be JSON")
        if not isinstance(cleanup, list) or not cleanup or not all(isinstance(item, str) and item for item in cleanup):
            parser.error("cleanup-command must be a nonempty argv list")
    manifest = {"run_id": args.run_id, "benchmark": "GAIR-NLP/WindowsAgentArena-V2",
                "baseline_id": args.baseline_id, "task_list": str(args.task_list.resolve()),
                "selected_task_ids": [task["_selection_id"] for _, task in selected], "task_count": len(selected),
                "source_task_ids": {task["_selection_id"]: task["id"] for _, task in selected},
                "arms": {arm: drivers[arm] for arm in arms}, "model": args.model,
                "reasoning": args.reasoning, "max_successful_model_responses": args.max_responses,
                "task_timeout_seconds": args.timeout, "setup_timeout_seconds": args.setup_timeout,
                "reset_cleanup_grace_seconds": args.reset_cleanup_timeout,
                "setup_settle_seconds": args.setup_settle, "evaluator_timeout_seconds": args.evaluator_timeout,
                "common_execution": "guest PowerShell", "model_api": "Responses",
                "model_base_url": args.gateway["base_url"] if args.gateway else os.environ.get("OPENAI_BASE_URL"),
                "concurrency": 1, "evidence_root": str(args.output.resolve()),
                "harness_path": str(Path(__file__).resolve()), "guest_url": args.guest_url,
                "bridge_url": args.bridge_url, "upstream_host": args.upstream_host,
                "model_retry_policy": {"retry_http_statuses": [429],
                                       "rate_limit_max_retries": args.rate_limit_max_retries,
                                       "rate_limit_base_delay": args.rate_limit_base_delay,
                                       "rate_limit_max_delay": args.rate_limit_max_delay,
                                       "other_error_retries": 0},
                "model_auth": args.gateway["authentication"] if args.gateway else "azure_cli" if azure_auth else "static_api_key",
                "model_gateway": args.gateway,
                "diagnostic": args.prepared_guest, "reset_command": args.reset_command,
                "cleanup_command": args.cleanup_command, "cleanup_timeout_seconds": args.cleanup_timeout,
                "host_python": sys.version, "host_executable": sys.executable,
                "host_packages": dict(sorted((d.metadata["Name"], d.version) for d in importlib.metadata.distributions() if d.metadata["Name"])),
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    if args.dry_run:
        print(json.dumps(manifest, indent=2))
        return
    required = ["WAA_BRIDGE_TOKEN"] + ([] if args.gateway else ["OPENAI_BASE_URL"])
    if not azure_auth and not (args.gateway and args.gateway["authentication"] == "none"):
        required.append("OPENAI_API_KEY")
    for name in required:
        if not os.environ.get(name):
            parser.error(name + " must be set in the environment")
    args.model_token_provider = AzureCliTokenProvider() if azure_auth else None
    if args.model_token_provider:
        args.model_token_provider(timeout=20)
    args.output.mkdir(parents=True, exist_ok=False)
    write_json(args.output / "manifest.json", manifest)
    results = []
    for index, (domain, task) in enumerate(selected):
        # Alternate first arm to reduce fixed-order effects across a selected task set.
        for arm in (arms if index % 2 == 0 else list(reversed(arms))):
            results.append(run_episode(args, arm, drivers[arm], domain, task, args.output / arm / task["_selection_id"]))
            write_json(args.output / "results.json", results)
            if results[-1]["vm_cleanup_status"] == "error":
                raise RuntimeError("VM cleanup failed; stopping before another episode. See the saved result and cleanup.log")
            terminal = results[-1]
            if terminal.get("error_kind") in {"rate_limit", "routing"} or terminal["status"] in {"interrupted", "cancelled"}:
                reason = terminal["error_kind"] if terminal.get("error_kind") in {"rate_limit", "routing"} else terminal["status"]
                write_json(args.output / "dispatch-stop.json", {
                    "reason": reason, "task_id": terminal["task_id"], "arm": terminal["arm"],
                    "recorded_episode_count": len(results), "planned_episode_count": len(selected) * len(arms),
                    "vm_cleanup_status": terminal["vm_cleanup_status"],
                    "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
                print(json.dumps(results, indent=2))
                return 75 if reason == "rate_limit" else 78 if reason == "routing" else 130
    print(json.dumps(results, indent=2))


def terminate(*_):
    """Unwind the active episode so SIGTERM preserves evidence and owned cleanup."""
    raise SystemExit(143)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, terminate)
    raise SystemExit(main())
