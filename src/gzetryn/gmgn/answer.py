"""Classify one raw GMGN answer into an outcome the gateway acts on (phase 0 codes, spec §10)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

OK = "ok"
BAD_PARAM = "bad_param"  # our input → 400, no pause
NEEDS_LOGIN = "needs_login"  # 401 40101611: endpoint needs a GMGN login (never used on purpose)
NOT_FOUND = "not_found"  # 404 path
THROTTLED = "throttled"  # 429, or Cloudflare 403 → IP pause, never data
SERVER_ERROR = "server_error"  # 5xx or code 50001300 → one retry
NETWORK = "network"  # no answer (timeout, reset)
UNKNOWN = "unknown"  # non-JSON 200, unexpected code → sample

RETRYABLE = {SERVER_ERROR, NETWORK}


@dataclass(frozen=True)
class RawAnswer:
    status: int
    body: Any  # parsed JSON, or None when the body was not JSON
    size: int = 0
    latency_ms: float = 0.0
    error: str | None = None  # transport-level failure (no HTTP answer)
    text_head: str = ""  # first bytes of a non-JSON body (for samples)


@dataclass(frozen=True)
class Verdict:
    outcome: str
    code: str | None = None
    message: str | None = None


def classify(raw: RawAnswer) -> Verdict:
    if raw.error is not None:
        return Verdict(NETWORK, message=raw.error)
    if raw.status == 429:
        return Verdict(THROTTLED, message="HTTP 429")
    body = raw.body
    if raw.status == 403:
        return Verdict(THROTTLED, message="HTTP 403 (Cloudflare)")
    if raw.status == 404:
        return Verdict(NOT_FOUND, message="HTTP 404")
    if not isinstance(body, dict):
        if raw.status >= 500:
            return Verdict(SERVER_ERROR, message=f"HTTP {raw.status}")
        return Verdict(UNKNOWN, message=f"HTTP {raw.status} non-JSON ({raw.size} bytes)")
    code = body.get("code")
    code_s = None if code is None else str(code)
    msg = body.get("msg") or body.get("message")
    message = msg if isinstance(msg, str) else None
    if raw.status == 200 and code_s == "0":
        return Verdict(OK, code_s)
    if raw.status == 401 or code_s == "40101611":
        return Verdict(NEEDS_LOGIN, code_s, message)
    if raw.status >= 500 or (code_s or "").startswith("500"):
        return Verdict(SERVER_ERROR, code_s, message)
    if (code_s or "").startswith("400") or 400 <= raw.status < 500:
        return Verdict(BAD_PARAM, code_s, message)
    return Verdict(UNKNOWN, code_s, message)
