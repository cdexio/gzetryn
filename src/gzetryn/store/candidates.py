"""Market candidates (spec §7.1): one row per (kind, mint); seq assigned at the first sighting only, so a cursor
reader sees each candidate once per kind. Later sightings update `last`, `last_seen_at`, `seen_count` and fill a
missing pool address."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, func, literal, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gzetryn.gmgn.parse import CandidateRow, iso
from gzetryn.store.models import Candidate

CANDIDATES_LOCK = 7_301_994  # advisory lock: candidate seq values commit in order


def candidate_dict(c: Candidate) -> dict:
    return {
        "seq": c.seq,
        "kind": c.kind,
        "mint": c.mint,
        "source": c.source,
        "first_seen_at": iso(c.first_seen_at),
        "last_seen_at": iso(c.last_seen_at),
        "seen_count": c.seen_count,
        "symbol": c.symbol,
        "name": c.name,
        "pool_address": c.pool_address,
        "exchange": c.exchange,
        "launchpad": c.launchpad,
        "launchpad_platform": c.launchpad_platform,
        "quote_address": c.quote_address,
        "creator": c.creator,
        "created_at": iso(c.created_at),
        "open_at": iso(c.open_at),
        "complete_at": iso(c.complete_at),
        "first": c.first,
        "last": c.last,
    }


class CandidateStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def known(self, kind: str, mints: list[str]) -> dict[str, str | None]:
        """{mint: pool_address} of mints already stored for `kind`."""
        if not mints:
            return {}
        async with self._sessions() as s:
            rows = await s.execute(
                select(Candidate.mint, Candidate.pool_address).where(Candidate.kind == kind, Candidate.mint.in_(mints))
            )
            return dict(rows.all())

    async def upsert(self, rows: list[CandidateRow], at: datetime) -> int:
        """Insert first sightings (in list order) and refresh known ones; returns the number of new candidates."""
        if not rows:
            return 0
        seen: set[tuple[str, str]] = set()
        uniq = []
        for r in rows:
            if (r.kind, r.mint) not in seen:
                seen.add((r.kind, r.mint))
                uniq.append(r)
        new = 0
        async with self._sessions() as s, s.begin():
            await s.execute(text("select pg_advisory_xact_lock(:k)"), {"k": CANDIDATES_LOCK})
            for r in uniq:
                values = {
                    "kind": r.kind,
                    "mint": r.mint,
                    "source": r.source,
                    "first_seen_at": at,
                    "last_seen_at": at,
                    "symbol": (r.symbol or "")[:64] or None,
                    "name": (r.name or "")[:128] or None,
                    "pool_address": r.pool_address,
                    "exchange": (r.exchange or "")[:32] or None,
                    "launchpad": (r.launchpad or "")[:32] or None,
                    "launchpad_platform": (r.launchpad_platform or "")[:32] or None,
                    "quote_address": r.quote_address,
                    "creator": r.creator,
                    "created_at": r.created_at,
                    "open_at": r.open_at,
                    "complete_at": r.complete_at,
                    "first": r.metrics,
                    "last": r.metrics,
                }
                res = await s.execute(
                    insert(Candidate).values(**values).on_conflict_do_nothing(constraint="uq_candidates_kind_mint")
                )
                if res.rowcount:
                    new += 1
                    continue
                # merge: a source's null fields do not erase the other source's values (GMGN holders/tags survive
                # chain updates; chain progress/price survive GMGN rows without them)
                merged = Candidate.last.op("||")(func.jsonb_strip_nulls(literal(r.metrics, JSONB)))
                await s.execute(
                    update(Candidate)
                    .where(Candidate.kind == r.kind, Candidate.mint == r.mint)
                    .values(
                        last=merged,
                        symbol=func.coalesce(Candidate.symbol, values["symbol"]),
                        name=func.coalesce(Candidate.name, values["name"]),
                        creator=func.coalesce(Candidate.creator, r.creator),
                        created_at=func.coalesce(Candidate.created_at, r.created_at),
                        last_seen_at=at,
                        seen_count=Candidate.seen_count + 1,
                        pool_address=func.coalesce(Candidate.pool_address, r.pool_address),
                        complete_at=func.coalesce(Candidate.complete_at, r.complete_at),
                        open_at=func.coalesce(Candidate.open_at, r.open_at),
                    )
                )
        return new

    async def read(self, after: int, kinds: list[str] | None, limit: int) -> tuple[list[dict], int]:
        async with self._sessions() as s:
            hi = (await s.execute(select(func.coalesce(func.max(Candidate.seq), 0)))).scalar_one()
            q = select(Candidate).where(Candidate.seq > after, Candidate.seq <= hi)
            if kinds:
                q = q.where(Candidate.kind.in_(kinds))
            q = q.order_by(Candidate.seq).limit(limit)
            rows = [candidate_dict(c) for c in (await s.execute(q)).scalars()]
        cursor = rows[-1]["seq"] if len(rows) >= limit else max(hi, after)
        return rows, cursor

    async def by_mint(self, mint: str) -> list[dict]:
        async with self._sessions() as s:
            q = select(Candidate).where(Candidate.mint == mint).order_by(Candidate.seq)
            return [candidate_dict(c) for c in (await s.execute(q)).scalars()]

    async def last_seq(self) -> int:
        async with self._sessions() as s:
            return (await s.execute(select(func.coalesce(func.max(Candidate.seq), 0)))).scalar_one()

    async def counts(self, since: datetime) -> dict:
        async with self._sessions() as s:
            rows = await s.execute(
                select(Candidate.kind, func.count(), func.count().filter(Candidate.pool_address.is_(None)))
                .where(Candidate.first_seen_at >= since)
                .group_by(Candidate.kind)
            )
            return {k: {"new": n, "without_pool": nopool} for k, n, nopool in rows}

    async def retention(self, days: int, now: datetime) -> int:
        async with self._sessions() as s, s.begin():
            res = await s.execute(delete(Candidate).where(Candidate.last_seen_at < now - timedelta(days=days)))
            return res.rowcount or 0
