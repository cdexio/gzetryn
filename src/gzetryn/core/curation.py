"""Curated list rule (spec §5, owner decision 2026-10-03 "recommended"). Pure: no I/O.

Candidates = wallets in the latest snapshot of any configured list. Pass = tag kol|smart_degen, 30d realized profit
> min, 30d PnL ratio > min, 30d win rate >= min, trades/day <= max. Order by 30d realized profit (GMGN's "PnL"),
take the top N.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gzetryn.config import CurationTunables


@dataclass
class Candidate:
    address: str
    tags: set[str]  # list tags it appears in ∪ GMGN row tags
    realized_profit_30d: float | None
    pnl_30d: float | None
    winrate_30d: float | None
    buy_30d: int | None
    sell_30d: int | None
    name: str | None = None
    twitter_username: str | None = None

    @property
    def trades_per_day(self) -> float | None:
        if self.buy_30d is None and self.sell_30d is None:
            return None
        return ((self.buy_30d or 0) + (self.sell_30d or 0)) / 30.0


@dataclass
class CurationResult:
    candidates: int
    passed: int
    rejected: dict[str, int]  # first failed rule → count
    selected: list[dict] = field(default_factory=list)  # ordered, rank 1..N

    @property
    def addresses(self) -> list[str]:
        return [x["address"] for x in self.selected]


def check(c: Candidate, t: CurationTunables) -> str | None:
    """Name of the first rule the candidate fails, or None when it passes."""
    if not c.tags & set(t.tags):
        return "tag"
    if c.realized_profit_30d is None or c.realized_profit_30d <= t.min_profit_30d_usd:
        return "profit_30d"
    if c.pnl_30d is None or c.pnl_30d <= t.min_pnl_30d:
        return "pnl_30d"
    if c.winrate_30d is None or c.winrate_30d < t.min_winrate_30d:
        return "winrate_30d"
    tpd = c.trades_per_day
    if tpd is None or tpd > t.max_trades_per_day:
        return "bot_paced"
    return None


def curate(candidates: list[Candidate], t: CurationTunables) -> CurationResult:
    rejected: dict[str, int] = {}
    passing: list[Candidate] = []
    for c in candidates:
        why = check(c, t)
        if why is None:
            passing.append(c)
        else:
            rejected[why] = rejected.get(why, 0) + 1
    passing.sort(key=lambda c: (-(c.realized_profit_30d or 0.0), c.address))
    selected = [
        {
            "address": c.address,
            "rank": n,
            "name": c.name,
            "twitter_username": c.twitter_username,
            "tags": sorted(c.tags),
            "realized_profit_30d": c.realized_profit_30d,
            "pnl_30d": c.pnl_30d,
            "winrate_30d": c.winrate_30d,
            "trades_per_day": round(c.trades_per_day or 0.0, 2),
        }
        for n, c in enumerate(passing[: t.top_n], start=1)
    ]
    return CurationResult(candidates=len(candidates), passed=len(passing), rejected=rejected, selected=selected)


def diff(previous: set[str], selected: list[str]) -> tuple[list[str], list[str]]:
    """(added, removed) between the previously curated set and the new selection."""
    now = set(selected)
    return [a for a in selected if a not in previous], sorted(previous - now)
