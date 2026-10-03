"""Operational tables: hourly request counters, raw samples, retention, and the reads behind /v1/stats."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gzetryn.config import RetentionTunables
from gzetryn.store.models import RankSnapshot, RequestLog, Sample


class OpsStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def flush_counts(self, counts: Counter, latency: dict, at: datetime) -> None:
        if not counts:
            return
        bucket = at.replace(minute=0, second=0, microsecond=0)
        async with self._sessions() as s, s.begin():
            for (consumer, endpoint, outcome), n in counts.items():
                if n <= 0:
                    continue
                stmt = insert(RequestLog).values(
                    bucket_start=bucket,
                    consumer=consumer[:32],
                    endpoint=endpoint[:32],
                    outcome=outcome[:16],
                    count=n,
                    latency_ms_sum=float(latency.get((consumer, endpoint, outcome), 0.0)),
                )
                await s.execute(
                    stmt.on_conflict_do_update(
                        constraint="uq_request_log_bucket",
                        set_={
                            "count": RequestLog.count + stmt.excluded.count,
                            "latency_ms_sum": RequestLog.latency_ms_sum + stmt.excluded.latency_ms_sum,
                        },
                    )
                )

    async def add_sample(self, at: datetime, endpoint: str, status: int, reason: str, body: str | None) -> None:
        async with self._sessions() as s, s.begin():
            s.add(Sample(at=at, endpoint=endpoint[:32], status=status, reason=reason[:64], body=(body or "")[:4000]))

    async def retention(self, t: RetentionTunables, now: datetime) -> dict[str, int]:
        out = {}
        async with self._sessions() as s, s.begin():
            for name, model, col, days in (
                ("rank_snapshots", RankSnapshot, RankSnapshot.snapshot_at, t.rank_snapshots_days),
                ("request_log", RequestLog, RequestLog.bucket_start, t.request_log_days),
                ("samples", Sample, Sample.at, t.samples_days),
            ):
                res = await s.execute(delete(model).where(col < now - timedelta(days=days)))
                out[name] = res.rowcount or 0
        return out

    async def requests_since(self, since: datetime) -> dict:
        """{endpoint: {outcome: count, ..., avg_latency_ms}} over GMGN requests (cache hits excluded from latency)."""
        async with self._sessions() as s:
            rows = await s.execute(
                select(
                    RequestLog.endpoint,
                    RequestLog.outcome,
                    func.sum(RequestLog.count),
                    func.sum(RequestLog.latency_ms_sum),
                )
                .where(RequestLog.bucket_start >= since)
                .group_by(RequestLog.endpoint, RequestLog.outcome)
            )
            out: dict[str, dict] = {}
            for endpoint, outcome, n, lat in rows:
                e = out.setdefault(endpoint, {"outcomes": {}, "requests": 0, "latency_ms_sum": 0.0})
                e["outcomes"][outcome] = int(n)
                if outcome not in ("cache", "shared"):
                    e["requests"] += int(n)
                    e["latency_ms_sum"] += float(lat or 0)
            for e in out.values():
                e["avg_latency_ms"] = round(e["latency_ms_sum"] / e["requests"], 1) if e["requests"] else None
                del e["latency_ms_sum"]
            return out

    async def ping(self) -> bool:
        async with self._sessions() as s:
            await s.execute(select(1))
            return True
