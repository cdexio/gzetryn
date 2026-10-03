"""curl_cffi transport: one Chrome-impersonated session, no cookies kept on purpose (phase 0: none needed)."""

from __future__ import annotations

import contextlib
import json
import time
from typing import Any, Protocol

from curl_cffi.requests import AsyncSession

from gzetryn.gmgn.answer import RawAnswer
from gzetryn.gmgn.endpoints import Endpoint

HEADERS = {"Referer": "https://gmgn.ai/", "Accept": "application/json, text/plain, */*"}


class Transport(Protocol):
    async def request(
        self, endpoint: Endpoint, path_params: dict[str, str], params: dict[str, Any], body: dict | None
    ) -> RawAnswer: ...

    async def close(self) -> None: ...


def query_pairs(fixed: dict, params: dict) -> list[tuple[str, str]]:
    """Query as (key, value) pairs; list values repeat the key (GMGN wants `type=buy&type=sell`)."""
    out: list[tuple[str, str]] = []
    for k, v in {**fixed, **params}.items():
        if v is None:
            continue
        for x in v if isinstance(v, (list, tuple)) else [v]:
            out.append((k, str(x).lower() if isinstance(x, bool) else str(x)))
    return out


class HttpTransport:
    def __init__(self, impersonate: str = "chrome", timeout_sec: float = 15.0):
        self._impersonate = impersonate
        self._timeout = timeout_sec
        self._session = AsyncSession(impersonate=impersonate, timeout=timeout_sec)

    async def request(
        self, endpoint: Endpoint, path_params: dict[str, str], params: dict[str, Any], body: dict | None
    ) -> RawAnswer:
        url = endpoint.url(**path_params)
        query = query_pairs(endpoint.fixed, params)
        t0 = time.monotonic()
        try:
            if endpoint.method == "POST":
                r = await self._session.post(
                    url,
                    params=query or None,
                    data=json.dumps(body or {}),
                    headers={**HEADERS, "Content-Type": "application/json"},
                )
            else:
                r = await self._session.get(url, params=query or None, headers=HEADERS)
        except Exception as e:  # curl errors: timeouts, resets, DNS
            return RawAnswer(0, None, latency_ms=(time.monotonic() - t0) * 1000, error=f"{type(e).__name__}: {e}"[:300])
        latency = (time.monotonic() - t0) * 1000
        content = r.content or b""
        parsed = None
        head = ""
        if content[:1] in (b"{", b"["):
            try:
                parsed = r.json()
            except ValueError:
                parsed = None
        if parsed is None:
            head = content[:300].decode("utf-8", "replace")
        return RawAnswer(r.status_code, parsed, size=len(content), latency_ms=latency, text_head=head)

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._session.close()
