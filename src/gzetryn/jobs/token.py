"""Token intel (spec §7): up to 10 cached GMGN calls + our feed, assembled per part; partial failures per part."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from gzetryn.config import TokenTunables
from gzetryn.gateway.gateway import Gateway, GatewayError, NotFound, Result, Unavailable
from gzetryn.gmgn import endpoints as E
from gzetryn.gmgn import parse
from gzetryn.store.feed import FeedStore
from gzetryn.store.wallets import WalletStore

PARTS = ("info", "launchpad", "security", "dev", "dev_history", "holders", "smart_traders", "feed")
GMGN_PARTS = tuple(p for p in PARTS if p != "feed")


class TokenIntel:
    def __init__(self, t: TokenTunables, gateway: Gateway, wallets: WalletStore, feed: FeedStore):
        self._t = t
        self._gw = gateway
        self._wallets = wallets
        self._feed = feed

    async def get(self, mint: str, parts: set[str], consumer: str, max_age_sec: float | None = None) -> dict:
        results: list[Result] = []

        async def call(ep: E.Endpoint, **kw) -> Any:
            r = await self._gw.call(ep, priority="P1", consumer=consumer, max_age_sec=max_age_sec, **kw)
            results.append(r)
            return r.body

        out: dict[str, Any] = {"mint": mint}
        errors: dict[str, str] = {}
        body_one = {"chain": E.CHAIN, "addresses": [mint]}

        async def part(name: str, coro) -> None:
            try:
                out[name] = await coro
            except GatewayError as e:
                errors[name] = _err(e)
                out[name] = None

        async def info():
            p = parse.token_window_info(await call(E.TOKEN_WINDOW_INFO, body=body_one))
            if p is None:
                raise NotFound("token not found")
            out["price"] = p["price"]
            return p["info"]

        async def launchpad():
            return parse.token_multi_info(await call(E.TOKEN_MULTI_INFO, body=body_one))

        async def security():
            return parse.token_security(await call(E.TOKEN_SECURITY, path={"mint": mint}))

        async def dev():
            return parse.token_dev_info(await call(E.TOKEN_DEV_INFO, path={"mint": mint}))

        async def holders():
            stat, hstat, tstat = await asyncio.gather(
                call(E.TOKEN_STAT, path={"mint": mint}),
                call(E.TOKEN_HOLDER_STAT, path={"mint": mint}),
                call(E.TOKEN_TRADER_STAT, path={"mint": mint}),
            )
            return {
                "rates": parse.token_stat(stat),
                "holder_counts_by_tag": parse.tag_counts(hstat),
                "trader_counts_by_tag": parse.tag_counts(tstat),
            }

        async def smart_traders():
            pages = await asyncio.gather(
                *(
                    call(E.TOKEN_TRADERS, path={"mint": mint}, params={"limit": self._t.traders_limit, "tag": gtag})
                    for gtag in E.TRADER_TAGS.values()
                )
            )
            merged: dict[str, dict] = {}
            for ours, body in zip(E.TRADER_TAGS, pages, strict=True):
                for row in parse.token_traders(body, ours).items:
                    if row["address"] in merged:
                        merged[row["address"]]["tags"].append(ours)
                    else:
                        row["tags"] = [ours]
                        del row["tag"]
                        merged[row["address"]] = row
            ids = await self._wallets.identities(list(merged))
            rows = []
            for addr, row in merged.items():
                ident = ids.get(addr) or {}
                rows.append(
                    {
                        **row,
                        "name": ident.get("name"),
                        "twitter_username": ident.get("twitter_username"),
                        "label": ident.get("label"),
                        "directory_status": ident.get("status"),
                    }
                )
            rows.sort(key=lambda x: x.get("first_at") or "")
            return rows

        wanted = [p for p in GMGN_PARTS if p in parts and p != "dev_history"]
        jobs = {"info": info, "launchpad": launchpad, "security": security, "dev": dev, "holders": holders,
                "smart_traders": smart_traders}
        # dev_history needs the creator: from multi_token_info (filled even for CTO tokens), else token_dev_info
        if "dev_history" in parts and "launchpad" not in wanted:
            wanted.append("launchpad")
        await asyncio.gather(*(part(p, jobs[p]()) for p in wanted))
        if "dev_history" in parts:
            creator = (out.get("launchpad") or {}).get("creator") or (out.get("dev") or {}).get("creator")
            if creator:
                await part(
                    "dev_history",
                    _then(
                        call(E.DEV_CREATED_TOKENS, path={"address": creator}),
                        lambda b: parse.dev_created_tokens(b, self._t.dev_recent_tokens),
                    ),
                )
                if out.get("dev_history") is not None:
                    out["dev_history"]["creator"] = creator
            else:
                out["dev_history"] = None
                errors.setdefault("dev_history", "creator unknown")
        if "feed" in parts:
            out["feed"] = await self._feed_part(mint)
        for p in list(out):
            if p not in parts and p not in ("mint", "price"):
                out.pop(p)
        if "info" not in parts:
            out.pop("price", None)

        gmgn_asked = [p for p in GMGN_PARTS if p in parts]
        failed = [p for p in gmgn_asked if p in errors and errors[p] != "creator unknown"]
        if gmgn_asked and len(failed) == len(gmgn_asked):
            if all(errors[p].startswith("not_found") for p in failed):
                raise NotFound(f"GMGN knows no token {mint}")
            raise Unavailable("gmgn: " + "; ".join(f"{p}: {errors[p]}" for p in failed), 30.0)
        out["errors"] = errors
        oldest: datetime | None = min((r.fetched_at for r in results), default=None)
        out["_meta"] = {
            "cached": bool(results) and all(r.cached for r in results),
            "stale": any(r.stale for r in results),
            "age_sec": round(max((r.age_sec for r in results), default=0.0), 1),
            "fetched_at": oldest.isoformat() if oldest else None,
            "gmgn_calls": len(results),
        }
        return out

    async def _feed_part(self, mint: str) -> dict:
        events = await self._feed.for_mint(mint, self._t.feed_events)
        per: dict[str, dict] = {}
        for e in events:
            w = per.setdefault(
                e["wallet"],
                {
                    "wallet": e["wallet"],
                    "name": e["wallet_name"],
                    "twitter_username": e["twitter_username"],
                    "label": e["label"],
                    "buys": 0,
                    "sells": 0,
                    "buy_usd": 0.0,
                    "sell_usd": 0.0,
                    "buy_sol": 0.0,
                    "sell_sol": 0.0,
                    "first_buy_at": None,
                    "last_trade_at": None,
                },
            )
            side = e["side"]
            w[f"{side}s"] += 1
            w[f"{side}_usd"] += e["usd_amount"] or 0.0
            w[f"{side}_sol"] += e["sol_amount"] or 0.0
            if side == "buy" and (w["first_buy_at"] is None or e["trade_at"] < w["first_buy_at"]):
                w["first_buy_at"] = e["trade_at"]
            if w["last_trade_at"] is None or e["trade_at"] > w["last_trade_at"]:
                w["last_trade_at"] = e["trade_at"]
        return {"wallets": sorted(per.values(), key=lambda x: x["first_buy_at"] or "~"), "events": events}


async def _then(coro, fn):
    return fn(await coro)


def _err(e: GatewayError) -> str:
    if isinstance(e, NotFound):
        return f"not_found: {e}"
    if isinstance(e, Unavailable):
        return f"unavailable: {e.reason}"
    return f"{type(e).__name__}: {e}"
