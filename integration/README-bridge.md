# Windows MCP bridge

The bridge runs a configured Model Context Protocol (MCP) server in the logged-in
Windows desktop session. A host controller sends requests through `bridge_client.py`.
The bridge preserves MCP results, tool schemas, notifications, and image data.
It uses Python's standard library and adds no model-visible tools.

This is an optional transport for agent integrations. It does not modify the
existing WAA runner, install a driver, reset a VM, or choose evaluation tasks.

## Guest setup

Use Python 3.10 or newer and an unlocked interactive Windows desktop.
The bridge rejects Session 0. A nonzero session ID does not prove the desktop is unlocked.
Protect the token and configuration files with permissions for the operator account.
Anyone with the token can run PowerShell as that account.

1. Copy `guest_mcp_bridge.py` and `bridge-config.example.json` to a guest directory.
2. Install the MCP server with its dependencies in the guest.
3. Edit the configuration's command array to match the installed server.
4. Select the installed PowerShell executable by absolute path.
5. Create a private token file containing a random secret shared with the controller.
6. Start the bridge from the logged-in user's terminal:

```powershell
python .\guest_mcp_bridge.py --config .\bridge-config.example.json --token-file .\bridge-token.txt
```

The default listener is `127.0.0.1:5051`. For a controller on another host, select
a guest interface with `--host` and restrict the guest firewall to that controller.
Use an SSH tunnel or another protected network path. The HTTP transport does not encrypt the token or payload.
Do not expose this service to untrusted networks. Stop it when the controller finishes.

Each `arms` entry names one configured server. `command` must be a nonempty array
whose executable path is absolute. Optional `cwd` and `env` fields configure the
MCP subprocess. The bridge never interprets the command array as a shell string.
A caller-supplied command must exactly match the configured array.

The optional `powershell` object accepts `executable` and `expected_version`.
Set `expected_version` to the installed runtime's exact version when version
matching is required. The bridge checks executable identity and the configured
version before executing task scripts. A missing or mismatched selected runtime
fails without falling back. If no executable is selected, the bridge uses the
absolute system Windows PowerShell path; it does not search `PATH`.

## Controller API

Add `integration` to the controller's Python import path. Read the URL and token
from your deployment configuration; do not store tokens in source control.

```python
from bridge_client import BridgeClient

client = BridgeClient(guest_url, token)
session = client.start("desktop")
try:
    initialization = client.call(session, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "waa-controller", "version": "1"},
    })
    client.call(session, "notifications/initialized")
    tools = client.call(session, "tools/list")
    runtime = client.shell_runtime(session)
finally:
    client.stop(session)
```

The controller owns MCP initialization and protocol negotiation. It must advertise
empty client capabilities: sampling and elicitation are unavailable. The bridge
answers server `ping` requests and rejects other server requests.

Every route uses POST, a JSON object, and the `X-Bridge-Token` header.

| Route | Request fields | Result |
| --- | --- | --- |
| `/health` | None | Desktop session, configured arms, active commands, failed sessions |
| `/session/start` | `arm`, optional `command` | Session ID and desktop session |
| `/session/call` | `session_id`, `method`, optional `params`, `timeout` | Complete JSON-RPC `response` and `notifications` |
| `/session/runtime` | `session_id` | Verified PowerShell executable, version, edition, .NET runtime |
| `/session/powershell` | `session_id`, `script`, optional `timeout` | Output, error, return code, runtime, interruption and truncation flags |
| `/session/stop` | `session_id` | `{"stopped": true}` after cleanup succeeds |

`call()` returns the MCP `result` unchanged. It raises `BridgeError` for protocol
errors while retaining the response in `exception.response`. `call_raw()` returns
the full envelope. `client.notifications` holds the last completed call's notifications.
Notifications sent by the client have no request ID and return no response.

One desktop owns one active session. Concurrent MCP or PowerShell calls receive
HTTP 409. MCP timeouts are greater than zero and at most 1800 seconds;
PowerShell timeouts are greater than zero and at most 120 seconds.
The MCP deadline covers both sending the request and waiting for its response.
The first runtime probe consumes the same timeout budget as its task script.

## Failure and cleanup

Neither the bridge nor its client retries mutations. After a transport failure
or timeout, an action may already have executed; its outcome remains unknown.
An MCP timeout marks the session failed and attempts to stop the subprocess.
Stop the failed session and reset the task environment before a new attempt.

Session stop closes MCP stdin and waits up to five seconds for a clean exit.
It then terminates a remaining parent process and waits another five seconds.
If stdin is blocked, stop terminates the parent before waiting for the writer.
If a descendant retains that input pipe, cleanup fails instead of waiting indefinitely.
If output pipes remain open, cleanup fails with HTTP 503 and desktop ownership
remains reserved. `/health` exposes that failure. Inspect and stop remaining
children, then repeat the stop request before resetting the task environment.
The bridge does not force-kill an unknown MCP descendant tree.
Pipe checks cannot detect detached descendants that close their inherited pipes.
The bridge trusts the configured server's output; notifications and MCP message
sizes are not bounded. Run only trusted MCP servers in the isolated guest.

PowerShell uses temporary files for output, so an intentionally launched GUI
can survive successful shell completion without blocking on inherited pipes.
On Windows, a supervisor waits until the bridge assigns it to a Job Object.
The bridge then permits it to start the script, so descendants inherit the job.
Timeout or session stop terminates an active command's job and checks for remaining processes.
Successful commands preserve their launched applications; stopping the session
does not close applications left by earlier successful commands.

PowerShell stdout and stderr remain separate UTF-8 text streams. Each response
stream is limited to 1 MiB and includes an explicit truncation flag.
The child uses `-OutputFormat Text` and `NO_COLOR=1`, including for parser errors.
Literal XML text is not decoded. Interrupted commands report `effect: unknown`;
successful exit codes report `effect: not_verified`, because exit status does not prove task completion.
The response limit does not cap temporary disk usage while a command runs.

## Tests

Run from the repository root:

```bash
PYTHONPATH=integration python3 -m unittest discover -s integration/tests -p 'test_*.py' -v
```

The tests use a real local HTTP server and fake MCP subprocess. They cover
authentication, persistent state, full images, notifications, ownership, errors,
timeouts, cleanup, and runtime selection. Runtime identity tests use mocks.
Set `WAA_TEST_PWSH` to an installed PowerShell 7 executable to run the text-stream
tests. Without that variable, those tests are explicitly skipped.
Run the command-lifetime tests on Windows to exercise native Job Object ownership.
Linux test results do not establish Windows desktop or driver compatibility.
