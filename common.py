"""Environment and HTTP helpers shared by the GitLab API and LLM clients."""

import json
import os
import urllib.request
from http.client import HTTPResponse
from typing import cast

MAX_PROMPT_DIFF_CHARS = 150_000


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set")
    return value


def dry_run() -> bool:
    return os.environ.get("DRY_RUN") == "1"


def request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: object = None,
    *,
    timeout: float = 300,
) -> object:
    req = urllib.request.Request(
        url,
        method=method,
        headers={"Content-Type": "application/json", **headers},
        data=json.dumps(body).encode() if body is not None else None,
    )
    with cast(HTTPResponse, urllib.request.urlopen(req, timeout=timeout)) as resp:
        raw = resp.read()
    return cast(object, json.loads(raw)) if raw else None


def truncate(text: str, limit: int = MAX_PROMPT_DIFF_CHARS, what: str = "diff") -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[{what} truncated: {len(text) - limit} more characters]"
