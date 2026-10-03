"""Health and stats reports from the live runtime (spec §10, §11)."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from gzetryn import __version__
from gzetryn.config import PROJECT_DIR
from gzetryn.runtime import Runtime


def _iso(v):
    return v.isoformat() if v else None


def _revision() -> str | None:
    f = PROJECT_DIR / "REVISION"
    try:
        return f.read_text().strip() or None
    except OSError:
        return None


class RuntimeStatus:
    def __init__(self, rt: Runtime):
        self._rt = rt

    async def health(self) -> dict:
        rt = self._rt
        now = rt.clock.now()
        up = (now - rt.started_at).total_seconds() if rt.started_at else 0.0
        c: dict[str, dict] = {}
        try:
            await rt.ops.ping()
            counts = await rt.wallets.counts()
            c["db"] = {"status": "ok"}
        except Exception as e:  # report, never raise: health must answer
            c["db"] = {"status": "broken", "reason": type(e).__name__}
            counts = {}

        g = rt.gateway
        paused = rt.budget.paused_for()
        gm = {
            "status": "ok",
            "paused_sec": round(paused, 1),
            "throttles": rt.budget.throttles,
            "tokens": round(rt.budget.tokens, 2),
            "last_ok_at": _iso(g.last_ok_at),
            "last_throttle_at": _iso(g.last_throttle_at),
            "last_error": g.last_error,
        }
        if paused > 0:
            gm.update(status="degraded", reason="throttled by GMGN; paused")
        elif up > 600 and (g.last_ok_at is None or (now - g.last_ok_at).total_seconds() > 900):
            gm.update(status="degraded", reason="no ok GMGN answer in 15 min")
        c["gmgn"] = gm

        d = rt.directory
        dr = {"status": "ok", "last_refresh_at": _iso(d.last_refresh_at), "last_refresh_error": d.last_refresh_error}
        if not rt.t.rank.enabled:
            dr["status"] = "disabled"
        elif d.last_refresh_at is None:
            if up > 600:
                dr.update(status="degraded", reason="no rank refresh yet")
        elif (now - d.last_refresh_at).total_seconds() > 3 * rt.t.rank.interval_sec:
            dr.update(status="degraded", reason="rank refresh older than 3 intervals")
        c["directory"] = dr

        cu = {"status": "ok", "curated": counts.get("curated"), "manual": counts.get("manual"), "last": d.last_curation}
        if rt.t.curation.enabled and up > 900 and not counts.get("curated"):
            cu.update(status="degraded", reason="no curated wallets")
        c["curation"] = cu

        w = rt.watcher.summary()
        wa = {"status": "ok", **w}
        if not rt.t.watch.enabled:
            wa["status"] = "disabled"
        elif w["active_wallets"] and up > 900:
            last = rt.watcher.stats.last_ok_at
            if last is None or (now - last).total_seconds() > 900:
                wa.update(status="degraded", reason="no ok poll in 15 min")
        c["watcher"] = wa

        statuses = [v["status"] for v in c.values()]
        overall = "broken" if "broken" in statuses else "degraded" if "degraded" in statuses else "ok"
        return {
            "status": overall,
            "version": __version__,
            "revision": _revision(),
            "started_at": _iso(rt.started_at),
            "uptime_sec": round(up),
            "components": c,
        }

    async def stats(self) -> dict:
        rt = self._rt
        now = rt.clock.now()
        up_min = max(1e-9, ((now - rt.started_at).total_seconds() if rt.started_at else 0.0) / 60.0)
        by_ep: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        by_consumer: dict[str, int] = defaultdict(int)
        lat: dict[str, list[float]] = defaultdict(lambda: [0.0, 0])
        for (consumer, endpoint, outcome), n in rt.gateway.counts.items():
            by_ep[endpoint][outcome] += n
            if outcome not in ("cache", "shared"):
                by_consumer[consumer] += n
                lat[endpoint][0] += rt.gateway.latency_ms.get((consumer, endpoint, outcome), 0.0)
                lat[endpoint][1] += n
        upstream = sum(by_consumer.values())
        since_start = {
            ep: {**dict(o), "avg_latency_ms": round(lat[ep][0] / lat[ep][1], 1) if lat[ep][1] else None}
            for ep, o in by_ep.items()
        }
        day = await rt.ops.requests_since(now - timedelta(hours=24))
        day_total = sum(e["requests"] for e in day.values())
        throttled_24h = sum(e["outcomes"].get("throttled", 0) for e in day.values())
        return {
            "since_start": {
                "uptime_min": round(up_min, 1),
                "gmgn_requests": upstream,
                "gmgn_requests_per_min": round(upstream / up_min, 2),
                "by_consumer": dict(by_consumer),
                "by_endpoint": since_start,
            },
            "last_24h": {
                "gmgn_requests": day_total,
                "throttled": throttled_24h,
                "by_endpoint": day,
            },
            "budget": {
                "per_minute": rt.t.budget.per_minute,
                "burst": rt.t.budget.burst,
                "min_gap_sec": rt.t.budget.min_gap_sec,
                "tokens": round(rt.budget.tokens, 2),
                "paused_sec": round(rt.budget.paused_for(), 1),
                "throttles": rt.budget.throttles,
                "granted": rt.budget.granted,
                "denied": rt.budget.denied,
            },
            "cache": {"entries": len(rt.gateway.cache), "hits": rt.gateway.cache.hits, "misses": rt.gateway.cache.misses},
            "directory": rt.directory.summary(),
            "watcher": rt.watcher.summary(),
            "wallets": await rt.wallets.counts(),
            "feed_24h": await rt.feed.stats(now - timedelta(hours=24)),
            "feed_last_seq": await rt.feed.last_seq(),
        }
