"""The DSN and the project setting, over the standard library's HTTP client.

The core ``costkey`` SDK patches httpx, requests and aiohttp to capture model
calls. The recorder stays on urllib, so its own uploads never show up as calls.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
from urllib.parse import quote, unquote, urlsplit


@dataclass(frozen=True)
class Dsn:
    auth_key: str
    project_id: str
    #: Scheme, host and any path CostKey is served under, without a trailing slash.
    base_url: str


_HELP = "Expected https://<key>@<host>/<project-id>, from Settings, Projects."


def parse_dsn(dsn: str) -> Dsn:
    """``https://<key>@<host>[/<path>]/<project-id>``, the same DSN the core SDK takes."""
    try:
        parts = urlsplit(dsn)
    except ValueError:
        raise ValueError(f"[costkey-replay] Invalid DSN. {_HELP}") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"[costkey-replay] Invalid DSN. {_HELP}")
    auth_key = unquote(parts.username or "")
    segments = [segment for segment in parts.path.split("/") if segment]
    project_id = segments.pop() if segments else ""
    if not auth_key or not project_id:
        raise ValueError(f"[costkey-replay] The DSN needs a key and a project ID: https://<key>@<host>/<project-id>.")
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    if parts.port:
        host = f"{host}:{parts.port}"
    base_path = "/" + "/".join(segments) if segments else ""
    return Dsn(auth_key=auth_key, project_id=project_id, base_url=f"{parts.scheme}://{host}{base_path}")


def send(
    method: str,
    url: str,
    headers: Dict[str, str],
    body: Optional[bytes] = None,
    timeout: float = 15.0,
) -> Tuple[int, str]:
    """One HTTP request; returns (status, body). Raises on network errors."""
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - the DSN's own host
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        try:
            text = error.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            text = ""
        return error.code, text


def fetch_screen_capture_enabled(dsn: Dsn) -> Optional[bool]:
    """Whether the project allows screen recordings: the second half of the
    double opt-in. None when the server can't be reached, so the caller retries."""
    try:
        status, text = send(
            "GET",
            f"{dsn.base_url}/api/v1/projects/{quote(dsn.project_id, safe='')}/config",
            {"Authorization": f"Bearer {dsn.auth_key}", "Accept": "application/json"},
        )
    except Exception:  # noqa: BLE001
        return None
    if status in (401, 403, 404):
        return False
    if status < 200 or status >= 300:
        return None
    try:
        config = json.loads(text)
    except ValueError:
        return None
    capture = config.get("capture") if isinstance(config, dict) else None
    return isinstance(capture, dict) and capture.get("screenCaptureEnabled") is True
