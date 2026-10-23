"""Explicit, credential-free configuration for a task-affinity gateway."""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_HEADERS = {"endpoint": "X-Gateway-Endpoint", "routing": "X-Gateway-Routing",
                   "request_id": "X-Gateway-Request-Id"}


def validate_model_url(value):
    url = urlsplit(value)
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
            or url.query or url.fragment):
        raise ValueError("Model URL must be HTTP(S) without credentials, query or fragment")
    return url


def load_gateway_config(path):
    config = json.loads(Path(path).read_text())
    required = {"base_url", "configuration_id", "routing_revision", "authentication"}
    if not isinstance(config, dict) or not required <= config.keys() or config.keys() - required - {"headers"}:
        raise ValueError("Gateway config requires base_url, configuration_id, routing_revision and authentication")
    if any(not isinstance(config[key], str) or not config[key].strip() for key in required):
        raise ValueError("Gateway config values must be nonempty strings")
    url = validate_model_url(config["base_url"])
    if url.path.rstrip("/") != "/v1":
        raise ValueError("Gateway base_url must be an HTTP(S) /v1 URL without credentials, query or fragment")
    if config["authentication"] not in {"api_key", "none"}:
        raise ValueError("Gateway authentication must be api_key or explicitly none")
    headers = config.get("headers", DEFAULT_HEADERS)
    if (not isinstance(headers, dict) or headers.keys() != DEFAULT_HEADERS.keys()
            or any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9-]+", value) for value in headers.values())
            or len({value.lower() for value in headers.values()}) != 3):
        raise ValueError("Gateway headers must name three distinct HTTP headers")
    return dict(config, base_url=config["base_url"].rstrip("/"), headers=dict(headers))
