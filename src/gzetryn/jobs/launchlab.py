"""Raydium LaunchLab lists as candidate sources (spec §7.3, phase 10).

`GET launch-mint-v1.raydium.io/get/list?sort=new|lastTrade` (verified 2026-10-05: only `new`, `lastTrade`,
`marketCap` sorts exist; `mintType=default`). `new` → kind `new`; `lastTrade` rows with
`completing_min_rate <= finishingRate < 100` → kind `completing` (finishingRate is Raydium's own percent; 100 after
migration). Requests go through the shared budget on their own rate line `launchlab` (bucket, gap, cooldown, probe).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

from curl_cffi.requests import AsyncSession

from gzetryn.clock import Clock
from gzetryn.config import LaunchLabTunables
from gzetryn.gateway.budget import Budget, Denied
from gzetryn.gmgn.parse import CANDIDATE_METRICS, CandidateRow, f, s
from gzetryn.log import fields, get_logger
from gzetryn.store.candidates import CandidateStore

log = get_logger("gzetryn.launchlab")
GROUP = "launchlab"


def _ms(v: Any) -> datetime | None:
    x = f(v)
    return datetime.fromtimestamp(x / 1000, UTC) if x and x > 0 else None


def parse_rows(body: Any, kind: str, min_rate: float | None = None) -> list[CandidateRow]:
    """LaunchLab list rows → candidates. For `completing`, keep min_rate <= finishingRate < 100."""
    rows = ((body or {}).get("data") or {}).get("rows") if isinstance(body, dict) else None
    out = []
    for r in rows or []:
        if not isinstance(r, dict) or not s(r.get("mint")):
            continue
        rate = f(r.get("finishingRate"))
        if kind == "completing" and (rate is None or rate >= 100 or rate < (min_rate or 0)):
            continue
        plat = r.get("platformInfo") if isinstance(r.get("platformInfo"), dict) else {}
        quote = r.get("mintB") if isinstance(r.get("mintB"), dict) else {}
        mcap = f(r.get("marketCap"))
        supply = f(r.get("supply"))
        metrics = dict.fromkeys(CANDIDATE_METRICS)
        metrics.update(
            progress=round(rate / 100, 6) if rate is not None else None,
            mcap_usd=mcap,
            price_usd=mcap / supply if mcap is not None and supply else None,
            volume_total_usd=f(r.get("volumeU")),
        )
        out.append(
            CandidateRow(
                kind=kind,
                source="launchlab",
                mint=s(r.get("mint")),
                symbol=s(r.get("symbol")),
                name=s(r.get("name")),
                pool_address=s(r.get("poolId")),
                exchange="ray_launchpad",
                launchpad="launchlab",
                launchpad_platform=s(plat.get("name")),
                quote_address=s(quote.get("address")),
                creator=s(r.get("creator")),
                created_at=_ms(r.get("createAt")),
                metrics=metrics,
            )
        )
    return out


class LaunchLab:
    def __init__(self, t: LaunchLabTunables, budget: Budget, store: CandidateStore, clock: Clock | None = None):
        self._t = t
        self._budget = budget
        self._store = store
        self._clock = clock or Clock()
        self._stopped = False
        self._session: AsyncSession | None = None
        self.stats: dict[str, Any] = {
            k: {"ok": 0, "failed": 0, "denied": 0, "rows": 0, "new": 0, "last_ok_at": None, "last_error": None}
            for k in ("new", "completing")
        }

    def stop(self) -> None:
        self._stopped = True

    async def fetch(self, sort: str, size: int) -> Any:
        """One list call through the `launchlab` rate line. Raises Denied (cooling) or RuntimeError."""
        await self._budget.take("P3", GROUP)
        r = await self._session.get(
            self._t.url,
            params={"sort": sort, "size": size, "mintType": "default", "includeNsfw": "false"},
            headers={"Accept": "application/json", "Origin": "https://raydium.io", "Referer": "https://raydium.io/"},
        )
        if r.status_code in (403, 429):
            step = self._budget.throttle(GROUP)
            raise RuntimeError(f"HTTP {r.status_code}; cooldown {step:.0f} s")
        try:
            body = r.json()
        except ValueError as e:
            self._budget.released(GROUP)
            raise RuntimeError(f"HTTP {r.status_code} non-JSON") from e
        if r.status_code != 200 or not body.get("success"):
            self._budget.ok(GROUP)  # a real answer: not a block
            raise RuntimeError(f"HTTP {r.status_code}: {str(body.get('msg'))[:100]}")
        self._budget.ok(GROUP)
        return body

    async def cycle(self, kind: str) -> int:
        st = self.stats[kind]
        sort, size = ("new", self._t.new_size) if kind == "new" else ("lastTrade", self._t.last_trade_size)
        try:
            body = await self.fetch(sort, size)
        except Denied as d:
            st["denied"] += 1
            st["last_error"] = f"denied: {d.reason}"
            return 0
        except Exception as e:
            st["failed"] += 1
            st["last_error"] = f"{type(e).__name__}: {str(e)[:150]}"
            return 0
        rows = parse_rows(body, kind, self._t.completing_min_rate)
        n = await self._store.upsert(rows, self._clock.now())
        st["ok"] += 1
        st["rows"] += len(rows)
        st["new"] += n
        st["last_ok_at"] = self._clock.now().isoformat()
        return n

    async def run(self) -> None:
        self._session = AsyncSession(impersonate="chrome", timeout=15)
        nxt = {"new": 0.0, "completing": 5.0}
        every = {"new": self._t.new_sec, "completing": self._t.last_trade_sec}
        start = self._clock.monotonic()
        nxt = {k: start + v for k, v in nxt.items()}
        try:
            while not self._stopped:
                now = self._clock.monotonic()
                for kind in ("new", "completing"):
                    if now >= nxt[kind]:
                        nxt[kind] = now + every[kind]
                        try:
                            await self.cycle(kind)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            log.exception("launchlab cycle failed", extra=fields(kind=kind))
                await self._clock.sleep(1.0)
        finally:
            with contextlib.suppress(Exception):
                await self._session.close()

    def summary(self) -> dict:
        return {**self.stats, "completing_min_rate": self._t.completing_min_rate}
