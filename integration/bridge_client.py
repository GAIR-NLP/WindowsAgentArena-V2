"""Controller-side client. Requests are sent once; mutations are never retried."""

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class BridgeError(RuntimeError):
    def __init__(self, message, response=None):
        super().__init__(message)
        self.response = response


class BridgeClient:
    def __init__(self, url, token, timeout=120):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.notifications = []

    def _post(self, route, body, timeout=None):
        request = Request(self.url + route, data=json.dumps(body).encode("utf-8"),
                          headers={"Content-Type": "application/json", "X-Bridge-Token": self.token})
        try:
            with urlopen(request, timeout=(timeout or self.timeout) + 10) as response:
                return json.load(response)
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise BridgeError("Bridge HTTP %s: %s" % (exc.code, raw), raw) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise BridgeError("Bridge transport failed; outcome unknown; no retry: " + str(exc)) from exc

    def health(self):
        return self._post("/health", {})

    def start(self, arm, command=None):
        body = {"arm": arm}
        if command is not None:
            body["command"] = command
        return self._post("/session/start", body)["session_id"]

    def call_raw(self, session_id, method, params=None, timeout=None):
        wait = timeout if timeout is not None else self.timeout
        body = {"session_id": session_id, "method": method, "timeout": wait}
        if params is not None:
            body["params"] = params
        return self._post("/session/call", body, wait)

    def call(self, session_id, method, params=None, timeout=None):
        envelope = self.call_raw(session_id, method, params, timeout)
        self.notifications = envelope.get("notifications", [])
        response = envelope["response"]
        if response is None:
            return None
        if "error" in response:
            raise BridgeError("MCP error: " + json.dumps(response["error"]), response)
        return response["result"]

    def stop(self, session_id):
        return self._post("/session/stop", {"session_id": session_id})

    def powershell(self, session_id, script, timeout=120):
        return self._post("/session/powershell", {
            "session_id": session_id, "script": script, "timeout": timeout}, timeout)

    def shell_runtime(self, session_id):
        return self._post("/session/runtime", {"session_id": session_id})
