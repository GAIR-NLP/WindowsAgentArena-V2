# Configurable HCU/CUA model runner

`waa_compare.py` runs the same Responses API loop with either Windows MCP driver.
The runner uses an external VM owner and the guest MCP bridge. It does not provision a gateway.

The host requires Linux, Python 3.11 or later, and the WAA client dependencies.
The guest requires the bridge, the selected driver, and the applications required by the task.
Use the repaired upstream `SetupController`: setup failures must raise before the agent starts.
Chrome remote debugging requires a usable baseline profile configured for the installed Chrome version.
This runner does not rewrite Chrome profiles or task setup commands.

## Configure a run

1. Create a task selection JSON that maps domain names to task filename IDs.
   The runner reads the original instructions from `evaluation_examples_windows/examples`.
2. Create a driver JSON with an `hcu` entry, a `cua` entry, or both entries.
   Each entry requires `version` and a nonempty `command` argv list.
   Add `candidate_id` for an unreleased build and use an explicit versioned executable path.
   The runner records all driver fields in the host manifest.
3. Configure model authentication on the host with `OPENAI_API_KEY`.
   Set `WAA_BRIDGE_TOKEN` separately for the guest bridge.
   Keep credentials out of task files, driver commands, and gateway configuration.
4. For a direct Responses endpoint, set `OPENAI_BASE_URL` to its `/v1` base URL.
   For task affinity, supply `--gateway-config` with a JSON file such as:

```json
{
  "base_url": "https://gateway.example/v1",
  "configuration_id": "my-model-pool-v1",
  "routing_revision": "task-affinity-v1",
  "authentication": "api_key",
  "headers": {
    "endpoint": "X-Gateway-Endpoint",
    "routing": "X-Gateway-Routing",
    "request_id": "X-Gateway-Request-Id"
  }
}
```

Header names are configurable. Set `authentication` to `none` only when the gateway accepts unauthenticated inbound requests.
The runner then omits the model credential even if a stale `OPENAI_API_KEY` exists.
For direct Azure endpoints, `WAA_AZURE_CLI_AUTH=1` uses and refreshes a host Azure CLI token.
Azure CLI authentication and gateway configuration are mutually exclusive.

5. Configure a reset command that owns a fresh VM for each episode.
   The runner substitutes `{run_id}`, `{arm}`, and `{task_id}` in its argv.
   The command must print one JSON object with `controller_url`, `bridge_url`, `episode_id`, and `cleanup_command`.
   An optional `vm_ip` supplies the upstream address for CDP and other original guest ports.
   The owner should write this binding to `WAA_RESET_RECORD_FILE` before provisioning resources.
   The runner can then recover cleanup ownership if reset fails before printing the binding.
6. Inspect the manifest with `--dry-run` before executing the same command without that flag.

```sh
python3 integration/waa_compare.py \
  --task-list selected-tasks.json --drivers selected-drivers.json \
  --arm both --run-id example-attempt-1 --baseline-id windows-baseline-v1 \
  --gateway-config gateway.json --model YOUR_MODEL --reasoning high \
  --max-responses 100 --timeout 1800 --output results/example-attempt-1 \
  --reset-command '["python3", "vm-owner.py", "reset", "--episode", "{run_id}-{arm}-{task_id}"]' \
  --dry-run
```

`vm-owner.py` represents the operator's owner command; it is not shipped by this feature.
The VM lifecycle integration provides an implementation separately.
Use `--prepared-guest` only for a single diagnostic episode with an externally restored guest.
Without a cleanup command, that guest remains the external owner's responsibility.

## Execution and evidence

The response budget counts completed model responses. HTTP attempts, received responses,
and completed responses have separate counters. An incomplete response is recorded and stops the agent.
The last permitted completed response can still execute its tools.
Each episode has its own time limit, MCP session, task route, and output directory.
The serial runner alternates which arm executes first. Use separate output directories and
independent VM owners when running multiple runner processes concurrently.

Gateway requests use `/t/<encoded-episode-identity>/v1/responses`.
The identity encodes the run, arm, and task without delimiter collisions.
Each successful HTTP response must carry the configured endpoint, routing protocol, and request ID headers.
The endpoint must stay fixed within an episode. Redirects are rejected before forwarding model context.
Routing metadata stays in host evidence and is not included in model messages.

Only HTTP 429 rejections receive automatic retries. Retries use exponential backoff,
respect `Retry-After`, and share the task deadline. The default allows three retries per response.
The configured maximum delay caps exponential backoff; a longer server `Retry-After` takes precedence
only if it fits within the remaining task deadline. Quota exhaustion stops immediately.
Unknown transport failures, other HTTP errors, and tool mutations are not automatically retried.
A terminal rate-limit or routing error stops dispatch after preserving the episode.
Operator interruption and VM cleanup failure also stop subsequent episodes.

The model receives the original task instruction, a general completion contract,
the driver tool schemas/instructions, and the common PowerShell tool.
Evaluator configuration, reference outputs, repair notes, and engineering instructions stay on the host.
Both arms may use PowerShell; these runs evaluate a hybrid agent, not a GUI-only policy.

The runner stops its MCP session before evaluating the task. Failed session cleanup blocks evaluation
because application state may still change. The result keeps agent status and evaluator status separate.
An unavailable evaluator score is `null`, never an inferred zero.

The runner copies declared actual-result files and screenshots before and after evaluation.
It does not read expected files or save applications during export.
Each export has a shared time budget and records unavailable, partial-write, and interrupted reads.
Exported bytes alone do not prove that an application persisted the intended edit.
Evidence write failures still trigger the owned VM cleanup command and remain fatal to dispatch.
Keep artifact reports, `result.json`, `trace.jsonl`, and the run manifest together.
`setup.log` and `postconfig.log` retain the upstream setup diagnostics for each phase.

## Tests

```sh
python3 -m unittest discover -s integration -p 'test_model_harness.py'
python3 -m unittest discover -s integration -p 'test_gateway_runner.py'
python3 -m unittest discover -s integration -p 'test_artifact_export.py'
python3 -m unittest discover -s integration -p 'test_cleanup_evidence.py'
python3 -m unittest discover -s integration -p 'test_interrupt_dispatch.py'
python3 -m unittest discover -s integration -p 'test_reset_harness.py'
```

The tests use local HTTP fixtures and subprocess owners. They do not start Windows VMs,
contact a model provider, or establish benchmark scores.
