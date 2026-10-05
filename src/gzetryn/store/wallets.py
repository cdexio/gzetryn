"""Wallet directory: rank snapshots, wallet upserts, curation, manual wallets, watch state, read queries."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, case, func, or_, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gzetryn.core.curation import Candidate, CurationResult, diff
from gzetryn.gmgn.parse import WalletRow, iso
from gzetryn.store.models import CurationRun, RankSnapshot, Wallet

METRIC_FIELDS = (
    "realized_profit_7d",
    "realized_profit_30d",
    "pnl_7d",
    "pnl_30d",
    "winrate_7d",
    "winrate_30d",
    "buy_30d",
    "sell_30d",
    "txs_30d",
    "avg_holding_sec_30d",
    "sol_balance",
    "follow_count",
    "daily_profit_7d",
    "last_active_at",
)
IDENTITY_FIELDS = ("name", "twitter_username", "twitter_name", "twitter_fans", "avatar")


def status_of(w: Wallet) -> str:
    if w.curated and w.manual:
        return "curated+manual"
    if w.curated:
        return "curated"
    if w.manual:
        return "manual"
    if w.rank_lists:
        return "ranked"
    return "inactive"


def wallet_dict(w: Wallet) -> dict:
    return {
        "address": w.address,
        "status": status_of(w),
        "active": bool(w.curated or w.manual),
        "curated": w.curated,
        "curated_rank": w.curated_rank,
        "curated_at": iso(w.curated_at),
        "uncurated_at": iso(w.uncurated_at),
        "manual": w.manual,
        "label": w.label,
        "user_tags": w.user_tags or [],
        "note": w.note,
        "added_by": w.added_by,
        "added_at": iso(w.added_at),
        "name": w.name,
        "twitter_username": w.twitter_username,
        "twitter_name": w.twitter_name,
        "twitter_fans": w.twitter_fans,
        "avatar": w.avatar,
        "gmgn_tags": w.gmgn_tags or [],
        "rank_lists": w.rank_lists or [],
        "metrics": {
            "realized_profit_7d": w.realized_profit_7d,
            "realized_profit_30d": w.realized_profit_30d,
            "pnl_7d": w.pnl_7d,
            "pnl_30d": w.pnl_30d,
            "winrate_7d": w.winrate_7d,
            "winrate_30d": w.winrate_30d,
            "buy_30d": w.buy_30d,
            "sell_30d": w.sell_30d,
            "txs_30d": w.txs_30d,
            "trades_per_day_30d": w.trades_per_day_30d,
            "avg_holding_sec_30d": w.avg_holding_sec_30d,
            "sol_balance": w.sol_balance,
            "follow_count": w.follow_count,
            "daily_profit_7d": w.daily_profit_7d,
            "last_active_at": iso(w.last_active_at),
            "at": iso(w.metrics_at),
            "source": w.metrics_source,
        },
        "first_seen_at": iso(w.first_seen_at),
        "last_ranked_at": iso(w.last_ranked_at),
        "watch": {
            "started_at": iso(w.watch_started_at),
            "last_poll_at": iso(w.last_poll_at),
            "last_poll_ok_at": iso(w.last_poll_ok_at),
            "last_poll_error": w.last_poll_error,
            "last_trade_at": iso(w.last_trade_at),
            "polls_ok": w.polls_ok,
            "polls_failed": w.polls_failed,
        },
    }


def snapshot_dict(r: RankSnapshot) -> dict:
    return {
        "snapshot_at": iso(r.snapshot_at),
        "period": r.period,
        "tag": r.tag,
        "rank": r.rank,
        "realized_profit_7d": r.realized_profit_7d,
        "realized_profit_30d": r.realized_profit_30d,
        "pnl_7d": r.pnl_7d,
        "pnl_30d": r.pnl_30d,
        "winrate_7d": r.winrate_7d,
        "winrate_30d": r.winrate_30d,
        "buy_30d": r.buy_30d,
        "sell_30d": r.sell_30d,
        "txs_30d": r.txs_30d,
        "sol_balance": r.sol_balance,
    }


def _metrics(row: WalletRow) -> dict:
    out = {k: getattr(row, k) for k in METRIC_FIELDS}
    out["trades_per_day_30d"] = row.trades_per_day_30d
    return out


class WalletStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    # ---------- rank refresh ----------

    async def store_rank(
        self, at: datetime, lists: dict[tuple[str, str], list[WalletRow]], complete: bool
    ) -> dict[str, int]:
        """Snapshots for every fetched list + wallet upserts. `complete`: every configured list was fetched."""
        by_addr: dict[str, tuple[WalletRow, set[str]]] = {}
        snaps = []
        for (tag, period), rows in lists.items():
            for r in rows:
                snaps.append(
                    {
                        "snapshot_at": at,
                        "period": period,
                        "tag": tag,
                        "rank": r.rank or 0,
                        "address": r.address,
                        "realized_profit_7d": r.realized_profit_7d,
                        "realized_profit_30d": r.realized_profit_30d,
                        "pnl_7d": r.pnl_7d,
                        "pnl_30d": r.pnl_30d,
                        "winrate_7d": r.winrate_7d,
                        "winrate_30d": r.winrate_30d,
                        "buy_30d": r.buy_30d,
                        "sell_30d": r.sell_30d,
                        "txs_30d": r.txs_30d,
                        "sol_balance": r.sol_balance,
                        "tags": r.tags,
                        "daily_profit_7d": r.daily_profit_7d,
                    }
                )
                prev = by_addr.get(r.address)
                labels = (prev[1] if prev else set()) | {f"{tag}:{period}"}
                # rows of one refresh carry the same metrics; prefer the 30d list's row
                keep = r if prev is None or period == "30d" else prev[0]
                by_addr[r.address] = (keep, labels)
        async with self._sessions() as s, s.begin():
            if snaps:
                await s.execute(insert(RankSnapshot), snaps)
            for addr, (r, labels) in by_addr.items():
                values = {
                    "address": addr,
                    **{k: getattr(r, k) for k in IDENTITY_FIELDS},
                    **_metrics(r),
                    "gmgn_tags": r.tags,
                    "rank_lists": sorted(labels),
                    "metrics_at": at,
                    "metrics_source": "rank",
                    "last_ranked_at": at,
                }
                stmt = insert(Wallet).values(**values)
                ex = stmt.excluded
                set_ = {k: ex[k] for k in values if k != "address" and k not in IDENTITY_FIELDS}
                for k in IDENTITY_FIELDS:
                    set_[k] = func.coalesce(ex[k], getattr(Wallet, k))
                set_["updated_at"] = func.now()
                await s.execute(stmt.on_conflict_do_update(index_elements=["address"], set_=set_))
            dropped = 0
            if complete:
                res = await s.execute(
                    update(Wallet)
                    .where(Wallet.address.not_in(list(by_addr)), func.jsonb_array_length(Wallet.rank_lists) > 0)
                    .values(rank_lists=[])
                )
                dropped = res.rowcount or 0
        return {"snapshots": len(snaps), "wallets": len(by_addr), "left_ranks": dropped}

    async def latest_list_times(self) -> dict[tuple[str, str], datetime]:
        async with self._sessions() as s:
            rows = await s.execute(
                select(RankSnapshot.tag, RankSnapshot.period, func.max(RankSnapshot.snapshot_at)).group_by(
                    RankSnapshot.tag, RankSnapshot.period
                )
            )
            return {(t, p): at for t, p, at in rows}

    async def _latest_rows(self, s: AsyncSession, keys: list[tuple[str, str]]) -> list[RankSnapshot]:
        latest = (
            select(RankSnapshot.tag, RankSnapshot.period, func.max(RankSnapshot.snapshot_at).label("at"))
            .group_by(RankSnapshot.tag, RankSnapshot.period)
            .subquery()
        )
        q = select(RankSnapshot).join(
            latest,
            and_(
                RankSnapshot.tag == latest.c.tag,
                RankSnapshot.period == latest.c.period,
                RankSnapshot.snapshot_at == latest.c.at,
            ),
        )
        if keys:
            q = q.where(tuple_(RankSnapshot.tag, RankSnapshot.period).in_(keys))
        return list((await s.execute(q.order_by(RankSnapshot.rank))).scalars())

    async def candidates(self, tags: list[str], periods: list[str]) -> tuple[list[Candidate], datetime | None]:
        keys = [(t, p) for t in tags for p in periods]
        async with self._sessions() as s:
            rows = await self._latest_rows(s, keys)
            names = {
                a: (n, tw)
                for a, n, tw in await s.execute(
                    select(Wallet.address, Wallet.name, Wallet.twitter_username).where(
                        Wallet.address.in_({r.address for r in rows})
                    )
                )
            }
        merged: dict[str, Candidate] = {}
        oldest = min((r.snapshot_at for r in rows), default=None)
        for r in rows:
            c = merged.get(r.address)
            row_tags = {r.tag, *[str(x) for x in (r.tags or [])]}
            if c is None or r.period == "30d":
                n, tw = names.get(r.address, (None, None))
                merged[r.address] = Candidate(
                    address=r.address,
                    tags=row_tags | (c.tags if c else set()),
                    realized_profit_30d=r.realized_profit_30d,
                    pnl_30d=r.pnl_30d,
                    winrate_30d=r.winrate_30d,
                    buy_30d=r.buy_30d,
                    sell_30d=r.sell_30d,
                    name=n,
                    twitter_username=tw,
                )
            else:
                c.tags |= row_tags
        return list(merged.values()), oldest

    # ---------- curation ----------

    async def last_curation(self) -> CurationRun | None:
        async with self._sessions() as s:
            return (
                await s.execute(select(CurationRun).order_by(CurationRun.at.desc()).limit(1))
            ).scalar_one_or_none()

    async def last_applied_curation_at(self) -> datetime | None:
        async with self._sessions() as s:
            return (
                await s.execute(select(func.max(CurationRun.at)).where(CurationRun.status == "applied"))
            ).scalar_one()

    async def apply_curation(self, result: CurationResult, at: datetime, params: dict) -> dict:
        async with self._sessions() as s, s.begin():
            prev = set((await s.execute(select(Wallet.address).where(Wallet.curated.is_(True)))).scalars())
            added, removed = diff(prev, result.addresses)
            for x in result.selected:
                values: dict = {"curated": True, "curated_rank": x["rank"], "uncurated_at": None}
                if x["address"] not in prev:
                    values["curated_at"] = at
                    # newly active: trades before this moment are baseline; a manual wallet was already watched
                    values["watch_started_at"] = case((Wallet.manual.is_(True), Wallet.watch_started_at), else_=at)
                await s.execute(update(Wallet).where(Wallet.address == x["address"]).values(**values))
            if removed:
                await s.execute(
                    update(Wallet)
                    .where(Wallet.address.in_(removed))
                    .values(curated=False, curated_rank=None, uncurated_at=at)
                )
            run = CurationRun(
                at=at,
                status="applied",
                params=params,
                candidates=result.candidates,
                passed=result.passed,
                rejected=result.rejected,
                selected=result.selected,
                added=added,
                removed=removed,
            )
            s.add(run)
        return {"selected": len(result.selected), "added": len(added), "removed": len(removed)}

    async def skip_curation(self, at: datetime, params: dict, reason: str, candidates: int) -> None:
        async with self._sessions() as s, s.begin():
            s.add(
                CurationRun(
                    at=at,
                    status="skipped",
                    reason=reason,
                    params=params,
                    candidates=candidates,
                    passed=0,
                    rejected=None,
                    selected=[],
                    added=[],
                    removed=[],
                )
            )

    async def curation_runs(self, limit: int) -> list[dict]:
        async with self._sessions() as s:
            rows = (await s.execute(select(CurationRun).order_by(CurationRun.at.desc()).limit(limit))).scalars()
            return [curation_dict(r) for r in rows]

    # ---------- manual wallets ----------

    async def add_manual(
        self, address: str, label: str | None, tags: list[str] | None, note: str | None, consumer: str, at: datetime
    ) -> tuple[dict, bool]:
        async with self._sessions() as s, s.begin():
            w = await s.get(Wallet, address, with_for_update=True)
            created = w is None or not w.manual
            if w is None:
                w = Wallet(address=address, first_seen_at=at, gmgn_tags=[], rank_lists=[], user_tags=[])
                s.add(w)
            if not (w.curated or w.manual):
                w.watch_started_at = at
            if not w.manual:
                w.added_by, w.added_at = consumer, at
            w.manual = True
            if label is not None:
                w.label = label
            if tags is not None:
                w.user_tags = sorted(set(tags))
            if note is not None:
                w.note = note
            await s.flush()
            await s.refresh(w)
            return wallet_dict(w), created

    async def update_manual(
        self, address: str, label: str | None, tags: list[str] | None, note: str | None
    ) -> dict | None:
        async with self._sessions() as s, s.begin():
            w = await s.get(Wallet, address, with_for_update=True)
            if w is None or not w.manual:
                return None
            if label is not None:
                w.label = label or None
            if tags is not None:
                w.user_tags = sorted(set(tags))
            if note is not None:
                w.note = note or None
            await s.flush()
            await s.refresh(w)
            return wallet_dict(w)

    async def remove_manual(self, address: str) -> dict | None:
        async with self._sessions() as s, s.begin():
            w = await s.get(Wallet, address, with_for_update=True)
            if w is None or not w.manual:
                return None
            w.manual = False
            await s.flush()
            await s.refresh(w)
            return wallet_dict(w)

    async def manual_needing_metrics(self, older_than: datetime) -> list[str]:
        """Manual wallets outside every rank list whose metrics are missing or older than `older_than`."""
        async with self._sessions() as s:
            q = select(Wallet.address).where(
                Wallet.manual.is_(True),
                func.jsonb_array_length(Wallet.rank_lists) == 0,
                or_(Wallet.metrics_at.is_(None), Wallet.metrics_at < older_than),
            )
            return list((await s.execute(q)).scalars())

    async def set_external_metrics(self, row: WalletRow, identity: dict | None, at: datetime) -> None:
        values = {k: v for k, v in _metrics(row).items() if v is not None}
        ident = {k: getattr(row, k) for k in IDENTITY_FIELDS if getattr(row, k) is not None}
        gmgn_tags = row.tags or None
        if identity:
            ident.update({k: v for k, v in identity.items() if k in IDENTITY_FIELDS and v is not None})
            gmgn_tags = gmgn_tags or identity.get("tags") or None
        if gmgn_tags:
            ident["gmgn_tags"] = gmgn_tags
        async with self._sessions() as s, s.begin():
            await s.execute(
                update(Wallet)
                .where(Wallet.address == row.address)
                .values(**values, **ident, metrics_at=at, metrics_source="wallet_new")
            )

    # ---------- watch state ----------

    async def active(self) -> list[Wallet]:
        async with self._sessions() as s:
            q = select(Wallet).where(or_(Wallet.curated.is_(True), Wallet.manual.is_(True)))
            return list((await s.execute(q)).scalars())

    async def tagged(self, tags: list[str], max_trades_per_day: float, limit: int) -> list[Wallet]:
        """Watch-only wallets (phase 14): ranked, neither curated nor manual, carrying one of `tags`, not bot-paced;
        highest 30d realized profit first."""
        async with self._sessions() as s:
            q = (
                select(Wallet)
                .where(
                    func.jsonb_array_length(Wallet.rank_lists) > 0,
                    Wallet.curated.is_(False),
                    Wallet.manual.is_(False),
                    or_(*(Wallet.gmgn_tags.contains([t]) for t in tags)),
                    func.coalesce(Wallet.trades_per_day_30d, 0.0) <= max_trades_per_day,
                )
                .order_by(Wallet.realized_profit_30d.desc().nulls_last(), Wallet.address)
                .limit(limit)
            )
            return list((await s.execute(q)).scalars())

    async def record_poll(
        self, address: str, at: datetime, ok: bool, error: str | None, last_trade_at: datetime | None
    ) -> None:
        values: dict = {"last_poll_at": at}
        if ok:
            values.update(last_poll_ok_at=at, last_poll_error=None, polls_ok=Wallet.polls_ok + 1)
            if last_trade_at is not None:
                values["last_trade_at"] = func.greatest(func.coalesce(Wallet.last_trade_at, last_trade_at), last_trade_at)
        else:
            values.update(last_poll_error=(error or "")[:500], polls_failed=Wallet.polls_failed + 1)
        async with self._sessions() as s, s.begin():
            await s.execute(update(Wallet).where(Wallet.address == address).values(**values))

    # ---------- reads ----------

    async def get(self, address: str) -> dict | None:
        async with self._sessions() as s:
            w = await s.get(Wallet, address)
            return wallet_dict(w) if w else None

    async def identities(self, addresses: list[str]) -> dict[str, dict]:
        if not addresses:
            return {}
        async with self._sessions() as s:
            rows = (await s.execute(select(Wallet).where(Wallet.address.in_(addresses)))).scalars()
            return {
                w.address: {
                    "name": w.name,
                    "twitter_username": w.twitter_username,
                    "label": w.label,
                    "status": status_of(w),
                }
                for w in rows
            }

    async def list_wallets(self, status: str, tag: str | None, q: str | None, limit: int) -> list[dict]:
        async with self._sessions() as s:
            stmt = select(Wallet)
            ranked = func.jsonb_array_length(Wallet.rank_lists) > 0
            if status == "curated":
                stmt = stmt.where(Wallet.curated.is_(True))
            elif status == "manual":
                stmt = stmt.where(Wallet.manual.is_(True))
            elif status == "active":
                stmt = stmt.where(or_(Wallet.curated.is_(True), Wallet.manual.is_(True)))
            elif status == "ranked":
                stmt = stmt.where(ranked, Wallet.curated.is_(False), Wallet.manual.is_(False))
            elif status == "inactive":
                stmt = stmt.where(Wallet.curated.is_(False), Wallet.manual.is_(False), ~ranked)
            if tag:
                stmt = stmt.where(or_(Wallet.gmgn_tags.contains([tag]), Wallet.user_tags.contains([tag])))
            if q:
                like = f"%{q}%"
                stmt = stmt.where(
                    or_(
                        Wallet.name.ilike(like),
                        Wallet.twitter_username.ilike(like),
                        Wallet.label.ilike(like),
                        Wallet.address.ilike(f"{q}%"),
                    )
                )
            stmt = stmt.order_by(
                Wallet.curated_rank.asc().nulls_last(), Wallet.realized_profit_30d.desc().nulls_last(), Wallet.address
            ).limit(limit)
            return [wallet_dict(w) for w in (await s.execute(stmt)).scalars()]

    async def history(self, address: str, period: str | None, since: datetime, limit: int) -> list[dict]:
        async with self._sessions() as s:
            q = select(RankSnapshot).where(RankSnapshot.address == address, RankSnapshot.snapshot_at >= since)
            if period:
                q = q.where(RankSnapshot.period == period)
            q = q.order_by(RankSnapshot.snapshot_at.desc(), RankSnapshot.tag).limit(limit)
            return [snapshot_dict(r) for r in (await s.execute(q)).scalars()]

    async def leaderboard(self, period: str, tags: list[str], sort: str, limit: int) -> tuple[list[dict], dict]:
        keys = [(t, period) for t in tags]
        async with self._sessions() as s:
            rows = await self._latest_rows(s, keys)
            if not rows:
                return [], {"snapshot_at": None}
            latest_at = max(r.snapshot_at for r in rows)
            # previous snapshot of the same lists, about 24 h earlier
            prev_at = (
                await s.execute(
                    select(func.max(RankSnapshot.snapshot_at)).where(
                        tuple_(RankSnapshot.tag, RankSnapshot.period).in_(keys),
                        RankSnapshot.snapshot_at <= latest_at - timedelta(hours=23),
                    )
                )
            ).scalar_one()
            prev: dict[str, tuple[int, float | None]] = {}
            if prev_at is not None:
                for r in (
                    await s.execute(
                        select(RankSnapshot).where(
                            tuple_(RankSnapshot.tag, RankSnapshot.period).in_(keys),
                            RankSnapshot.snapshot_at == prev_at,
                        )
                    )
                ).scalars():
                    if r.address not in prev or r.rank < prev[r.address][0]:
                        prev[r.address] = (r.rank, getattr(r, f"realized_profit_{period}"))
            wallets = {
                w.address: w
                for w in (
                    await s.execute(select(Wallet).where(Wallet.address.in_({r.address for r in rows})))
                ).scalars()
            }
        best: dict[str, RankSnapshot] = {}
        lists: dict[str, set[str]] = {}
        for r in rows:
            lists.setdefault(r.address, set()).add(r.tag)
            if r.address not in best or r.rank < best[r.address].rank:
                best[r.address] = r
        out = []
        for addr, r in best.items():
            w = wallets.get(addr)
            profit = getattr(r, f"realized_profit_{period}")
            p = prev.get(addr)
            out.append(
                {
                    "address": addr,
                    "name": w.name if w else None,
                    "twitter_username": w.twitter_username if w else None,
                    "lists": sorted(lists[addr]),
                    "gmgn_rank": r.rank,
                    "realized_profit": profit,
                    "pnl": getattr(r, f"pnl_{period}"),
                    "winrate": getattr(r, f"winrate_{period}"),
                    "realized_profit_30d": r.realized_profit_30d,
                    "winrate_30d": r.winrate_30d,
                    "trades_per_day_30d": ((r.buy_30d or 0) + (r.sell_30d or 0)) / 30.0
                    if r.buy_30d is not None or r.sell_30d is not None
                    else None,
                    "sol_balance": r.sol_balance,
                    "curated": bool(w and w.curated),
                    "curated_rank": w.curated_rank if w else None,
                    "manual": bool(w and w.manual),
                    "prev_rank": p[0] if p else None,
                    "profit_change": (profit - p[1]) if p and profit is not None and p[1] is not None else None,
                }
            )
        key = {"profit": "realized_profit", "pnl": "pnl", "winrate": "winrate"}[sort]
        out.sort(key=lambda x: (-(x[key] if x[key] is not None else float("-inf")), x["address"]))
        for n, x in enumerate(out, start=1):
            x["position"] = n
        return out[:limit], {"snapshot_at": iso(latest_at), "prev_snapshot_at": iso(prev_at)}

    async def copy_rows(self, tags: list[str], periods: list[str], presence_days: int, now: datetime) -> list[dict]:
        cands, _ = await self.candidates(tags, periods)
        if not cands:
            return []
        addrs = [c.address for c in cands]
        async with self._sessions() as s:
            presence = {
                a: n
                for a, n in await s.execute(
                    select(
                        RankSnapshot.address,
                        func.count(func.distinct(func.date_trunc(text("'day'"), RankSnapshot.snapshot_at))),
                    )
                    .where(
                        RankSnapshot.address.in_(addrs),
                        RankSnapshot.snapshot_at >= now - timedelta(days=presence_days),
                    )
                    .group_by(RankSnapshot.address)
                )
            }
            wallets = {w.address: w for w in (await s.execute(select(Wallet).where(Wallet.address.in_(addrs)))).scalars()}
        out = []
        for c in cands:
            w = wallets.get(c.address)
            out.append(
                {
                    "candidate": c,
                    "address": c.address,
                    "name": c.name,
                    "twitter_username": c.twitter_username,
                    "tags": sorted(c.tags),
                    "realized_profit_30d": c.realized_profit_30d,
                    "pnl_30d": c.pnl_30d,
                    "winrate_30d": c.winrate_30d,
                    "trades_per_day_30d": c.trades_per_day,
                    "daily_profit_7d": w.daily_profit_7d if w else None,
                    "presence_days": presence.get(c.address, 0),
                    "curated": bool(w and w.curated),
                    "manual": bool(w and w.manual),
                }
            )
        return out

    async def counts(self) -> dict:
        async with self._sessions() as s:
            row = (
                await s.execute(
                    select(
                        func.count(),
                        func.count().filter(Wallet.curated.is_(True)),
                        func.count().filter(Wallet.manual.is_(True)),
                        func.count().filter(or_(Wallet.curated.is_(True), Wallet.manual.is_(True))),
                        func.max(Wallet.last_poll_ok_at),
                    )
                )
            ).one()
            return {
                "wallets": row[0],
                "curated": row[1],
                "manual": row[2],
                "active": row[3],
                "last_poll_ok_at": iso(row[4]),
            }


def curation_dict(r: CurationRun) -> dict:
    return {
        "id": r.id,
        "at": iso(r.at),
        "status": r.status,
        "reason": r.reason,
        "params": r.params,
        "candidates": r.candidates,
        "passed": r.passed,
        "rejected": r.rejected,
        "selected": r.selected,
        "added": r.added,
        "removed": r.removed,
    }
