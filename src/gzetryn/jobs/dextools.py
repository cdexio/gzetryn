"""DEXTools pair page → measure-only `snipers` token part (spec §7.4, phase 11).

`GET www.dextools.io/shared/data/pair?address=<AMM pool>&chain=solana&audit=true&locks=true`; DEXTools knows only AMM
pools (bonding curves → 400 "Pair not found", 26/26 verified). Calls go through the shared budget on the rate line
`dextools` (GMGN group policy); answers are cached (first makers do not change).
"""

from __future__ import annotations

import contextlib
from typing import Any

from curl_cffi.requests import AsyncSession

from gzetryn.clock import Clock
from gzetryn.config import DexToolsTunables
from gzetryn.gateway.budget import Budget
from gzetryn.gateway.cache import TtlCache
from gzetryn.gmgn.parse import iso, s, ts

GROUP = "dextools"
BONDING_EXCHANGES = {"pump", "ray_launchpad", "meteora_virtual_curve"}


class NoPair(Exception):
    pass


def parse_pair(body: Any, pool: str) -> dict | None:
    data = body.get("data") if isinstance(body, dict) else None
    p = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else None
    if p is None:
        return None
    tok = p.get("token") if isinstance(p.get("token"), dict) else {}
    fm = tok.get("firstMakers") if isinstance(tok.get("firstMakers"), dict) else {}
    wallets = [w for w in (fm.get("snipers") or []) if isinstance(w, str)]
    owner = s((tok.get("deployment") or {}).get("owner")) if isinstance(tok.get("deployment"), dict) else None
    mf = p.get("migratedFrom") if isinstance(p.get("migratedFrom"), dict) else None
    metrics = tok.get("metrics") if isinstance(tok.get("metrics"), dict) else {}
    return {
        "source": "dextools",
        "available": True,
        "measure_only": True,
        "pool": pool,
        "count": len(wallets),
        "count_excl_creator": len([w for w in wallets if w != owner]),
        "wallets": wallets,
        "creator": owner,
        "creator_is_sniper": bool(owner and owner in wallets),
        "promoted": tok.get("promoted") if isinstance(tok.get("promoted"), bool) else None,
        "migrated_from": {"exchange": s(mf.get("exchange")), "pair": s(mf.get("pair")), "date": s(mf.get("date"))}
        if mf
        else None,
        "holders": metrics.get("holders") if isinstance(metrics.get("holders"), int) else None,
        "pair_created_at": iso(ts(p.get("creationTime"))) if isinstance(p.get("creationTime"), (int, float)) else s(p.get("creationTime")),
    }


class DexTools:
    def __init__(self, t: DexToolsTunables, budget: Budget, clock: Clock | None = None):
        self._t = t
        self._budget = budget
        self._clock = clock or Clock()
        self._cache = TtlCache(5000, self._clock)
        self._session: AsyncSession | None = None
        self.stats = {"calls": 0, "ok": 0, "not_found": 0, "throttled": 0, "errors": 0, "cache_hits": 0}

    async def snipers(self, pool: str, priority: str = "P1") -> dict:
        """Parsed first makers for an AMM pool. Raises NoPair (DEXTools has no such pair) or RuntimeError."""
        key = ("dextools_pair", pool)
        hit = self._cache.get(key, self._t.cache_sec)
        if hit is not None:
            self.stats["cache_hits"] += 1
            return hit[0].value
        await self._budget.take(priority, GROUP)
        if self._session is None:
            self._session = AsyncSession(impersonate="chrome", timeout=15)
        self.stats["calls"] += 1
        try:
            r = await self._session.get(
                self._t.url,
                params={"address": pool, "chain": "solana", "audit": "true", "locks": "true"},
                headers={"Referer": "https://www.dextools.io/", "Accept": "application/json"},
            )
        except Exception as e:
            self._budget.released(GROUP)
            self.stats["errors"] += 1
            raise RuntimeError(f"dextools: {type(e).__name__}") from e
        if r.status_code in (403, 429):
            self.stats["throttled"] += 1
            step = self._budget.throttle(GROUP)
            raise RuntimeError(f"dextools HTTP {r.status_code}; cooldown {step:.0f} s")
        try:
            body = r.json()
        except ValueError as e:
            self._budget.released(GROUP)
            self.stats["errors"] += 1
            raise RuntimeError(f"dextools HTTP {r.status_code} non-JSON") from e
        self._budget.ok(GROUP)
        if r.status_code == 400 and "not found" in str(body).lower():
            self.stats["not_found"] += 1
            raise NoPair(f"DEXTools has no pair {pool}")
        out = parse_pair(body, pool)
        if r.status_code != 200 or out is None:
            self.stats["errors"] += 1
            raise RuntimeError(f"dextools HTTP {r.status_code}: {str(body)[:120]}")
        self.stats["ok"] += 1
        self._cache.put(key, out, self._clock.now())
        return out

    async def close(self) -> None:
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()
