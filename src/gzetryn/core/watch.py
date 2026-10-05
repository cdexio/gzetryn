"""Watcher decisions (spec §6). Pure: interval tier, paging and baseline."""

from __future__ import annotations

from datetime import datetime

from gzetryn.config import WatchTunables


def tier(last_trade_at: datetime | None, now: datetime, t: WatchTunables) -> str:
    if last_trade_at is None:
        return "cold"
    age = (now - last_trade_at).total_seconds()
    if age <= t.hot_window_sec:
        return "hot"
    if age <= t.warm_window_sec:
        return "warm"
    return "cold"


def interval(
    last_trade_at: datetime | None, now: datetime, t: WatchTunables, fallback: bool = False, sweep: bool = False
) -> float:
    """Polling interval by tier; `fallback` = the on-chain trigger is healthy, so interval polls only catch misses;
    `sweep` = notified swaps go to the chain decoder (D-2026-10-05-14), GMGN only sweeps for what it cannot see."""
    k = tier(last_trade_at, now, t)
    if sweep:
        return {"hot": t.sweep_hot_interval_sec, "warm": t.sweep_warm_interval_sec}.get(k, t.sweep_cold_interval_sec)
    if fallback:
        return {"hot": t.fallback_hot_interval_sec, "warm": t.fallback_warm_interval_sec}.get(
            k, t.fallback_cold_interval_sec
        )
    return {"hot": t.hot_interval_sec, "warm": t.warm_interval_sec}.get(k, t.cold_interval_sec)


def next_retry(attempt: int, delays: list[float]) -> float | None:
    """Delay before triggered retry number `attempt` (1-based) while a notified tx is not indexed yet; None = stop."""
    return delays[attempt - 1] if 1 <= attempt <= len(delays) else None


def want_next_page(
    page_size: int,
    rows_on_page: int,
    inserted_on_page: int,
    oldest_on_page: datetime | None,
    known_last_trade_at: datetime | None,
    has_cursor: bool,
) -> bool:
    """Fetch the next page only when the whole (full) page was new and still newer than anything stored.

    A wallet seen for the first time (known_last_trade_at None) is not paged: its history is baseline anyway.
    """
    if not has_cursor or rows_on_page < page_size or inserted_on_page < rows_on_page:
        return False
    if known_last_trade_at is None or oldest_on_page is None:
        return False
    return oldest_on_page > known_last_trade_at


def is_baseline(trade_at: datetime, watch_started_at: datetime | None) -> bool:
    """A trade older than the moment the wallet became active is history, not a signal."""
    return watch_started_at is None or trade_at < watch_started_at
