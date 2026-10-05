"""Directory job (spec §5): rank refresh → snapshots → daily curation; metrics for manual wallets outside the ranks."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta

from gzetryn.clock import Clock
from gzetryn.config import Tunables
from gzetryn.core.curation import curate
from gzetryn.gateway.gateway import Gateway, GatewayError
from gzetryn.gmgn import endpoints as E
from gzetryn.gmgn import parse
from gzetryn.log import fields, get_logger
from gzetryn.store.wallets import WalletStore

log = get_logger("gzetryn.directory")


class Directory:
    def __init__(self, t: Tunables, gateway: Gateway, wallets: WalletStore, clock: Clock | None = None):
        self._t = t
        self._gw = gateway
        self._wallets = wallets
        self._clock = clock or Clock()
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._stopped = False
        self._tasks: set[asyncio.Task] = set()
        self.last_refresh_at: datetime | None = None
        self.last_refresh: dict | None = None
        self.last_refresh_error: str | None = None
        self.last_curation: dict | None = None

    def stop(self) -> None:
        self._stopped = True
        self._wake.set()

    # ---------- rank refresh ----------

    async def refresh(self, consumer: str = "gzetryn") -> dict:
        lists: dict[tuple[str, str], list[parse.WalletRow]] = {}
        errors: dict[str, str] = {}
        for tag in self._t.rank.tags:
            for period in self._t.rank.periods:
                try:
                    r = await self._gw.call(
                        E.RANK_WALLETS,
                        path={"period": period},
                        params={"tag": tag, "orderby": f"pnl_{period}"},
                        priority="P3",
                        consumer=consumer,
                        max_age_sec=60,
                    )
                    pg = parse.rank_wallets(r.body)
                    if not pg.items:
                        errors[f"{tag}:{period}"] = "empty list"
                        continue
                    lists[(tag, period)] = pg.items
                except GatewayError as e:
                    errors[f"{tag}:{period}"] = str(e)
        at = self._clock.now()
        stats = await self._wallets.store_rank(at, lists, complete=not errors) if lists else {}
        out = {"at": at.isoformat(), "lists": {f"{k[0]}:{k[1]}": len(v) for k, v in lists.items()}, **stats}
        if errors:
            out["errors"] = errors
        self.last_refresh_at = at if lists else self.last_refresh_at
        self.last_refresh = out
        self.last_refresh_error = "; ".join(f"{k}: {v}" for k, v in errors.items()) or None
        log.info("rank refresh", extra=fields(**out))
        return out

    # ---------- curation ----------

    async def curation_due(self) -> bool:
        last = await self._wallets.last_applied_curation_at()
        return last is None or (self._clock.now() - last).total_seconds() >= self._t.curation.interval_sec

    async def curate(self) -> dict:
        c = self._t.curation
        at = self._clock.now()
        params = c.model_dump()
        cands, oldest = await self._wallets.candidates(c.tags, self._t.rank.periods)
        max_age = timedelta(seconds=2 * self._t.rank.interval_sec + 600)
        if len(cands) < c.min_candidates:
            reason = f"only {len(cands)} candidates (< {c.min_candidates}); rank refresh failed?"
        elif oldest is None or at - oldest > max_age:
            reason = f"rank snapshots too old (oldest list at {oldest})"
        else:
            reason = None
        if reason:
            await self._wallets.skip_curation(at, params, reason, len(cands))
            out = {"status": "skipped", "reason": reason, "candidates": len(cands)}
        else:
            result = curate(cands, c)
            applied = await self._wallets.apply_curation(result, at, params)
            out = {
                "status": "applied",
                "candidates": result.candidates,
                "passed": result.passed,
                "rejected": result.rejected,
                **applied,
            }
        out["at"] = at.isoformat()
        self.last_curation = out
        log.info("curation", extra=fields(**out))
        return out

    # ---------- manual wallets ----------

    async def manual_metrics(self, only: str | None = None) -> int:
        at = self._clock.now()
        if only:
            todo = [only]
        else:
            todo = await self._wallets.manual_needing_metrics(at - timedelta(seconds=self._t.directory.manual_metrics_sec))
        done = 0
        for address in todo:
            try:
                r = await self._gw.call(E.WALLET_NEW, path={"address": address}, priority="P3")
                row = parse.wallet_new(r.body, address)
                ident = None
                with contextlib.suppress(GatewayError):
                    c = await self._gw.call(E.WALLET_COMMON_STAT, path={"address": address}, priority="P3")
                    ident = parse.wallet_common_stat(c.body)
                if row is not None:
                    await self._wallets.set_external_metrics(row, ident, self._clock.now())
                    done += 1
            except GatewayError as e:
                log.warning("manual wallet metrics failed", extra=fields(address=address, error=str(e)))
        return done

    def kick_manual(self, address: str) -> None:
        """Fetch a newly added manual wallet's metrics in the background (keeps a reference until done)."""
        task = asyncio.create_task(self._guarded(self.manual_metrics(only=address)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _guarded(self, coro) -> None:
        try:
            await coro
        except Exception:
            log.exception("directory task failed")

    # ---------- loop ----------

    async def cycle(self, force_curation: bool = False) -> dict:
        async with self._lock:
            out = {"refresh": await self.refresh()}
            if self._t.curation.enabled and (force_curation or await self.curation_due()):
                out["curation"] = await self.curate()
            out["manual_metrics"] = await self.manual_metrics()
            return out

    async def curate_now(self) -> dict:
        async with self._lock:
            return await self.curate()

    async def run(self) -> None:
        await self._clock.sleep(self._t.rank.first_delay_sec)
        while not self._stopped:
            try:
                await self.cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("directory cycle failed")
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._t.rank.interval_sec)

    def summary(self) -> dict:
        return {
            "last_refresh_at": self.last_refresh_at.isoformat() if self.last_refresh_at else None,
            "last_refresh": self.last_refresh,
            "last_refresh_error": self.last_refresh_error,
            "last_curation": self.last_curation,
        }
