"""Tolerant parsers: GMGN answers → plain gzetryn records (phase 0 field names).

GMGN sends most numbers as strings; missing or empty values become None. A malformed row is skipped and counted,
never fatal. Only this module and gmgn/endpoints.py know GMGN's wire format.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

WSOL = "So11111111111111111111111111111111111111112"


# ---------- scalar helpers ----------


def f(v: Any) -> float | None:
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def i(v: Any) -> int | None:
    x = f(v)
    return None if x is None else int(x)


def s(v: Any) -> str | None:
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def ts(v: Any) -> datetime | None:
    """Unix seconds (or milliseconds) → UTC datetime; 0/empty → None."""
    x = f(v)
    if not x or x <= 0:
        return None
    if x > 1e12:  # milliseconds
        x /= 1000.0
    try:
        return datetime.fromtimestamp(x, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def b(v: Any) -> bool | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).lower() in ("1", "true", "yes")


def data(body: Any) -> Any:
    return body.get("data") if isinstance(body, dict) else None


def _list(v: Any) -> list:
    return v if isinstance(v, list) else []


def _dict(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


@dataclass
class Page:
    items: list = field(default_factory=list)
    skipped: int = 0
    next: str | None = None


# ---------- wallets ----------


@dataclass
class WalletRow:
    """A wallet's identity + metrics, from a rank row (rank set) or from walletNew (rank None)."""

    address: str
    rank: int | None = None
    name: str | None = None
    twitter_username: str | None = None
    twitter_name: str | None = None
    twitter_fans: int | None = None
    avatar: str | None = None
    tags: list[str] = field(default_factory=list)
    realized_profit_7d: float | None = None
    realized_profit_30d: float | None = None
    pnl_7d: float | None = None
    pnl_30d: float | None = None
    winrate_7d: float | None = None
    winrate_30d: float | None = None
    buy_30d: int | None = None
    sell_30d: int | None = None
    txs_30d: int | None = None
    avg_holding_sec_30d: float | None = None
    sol_balance: float | None = None
    follow_count: int | None = None
    daily_profit_7d: list[dict] | None = None
    last_active_at: datetime | None = None

    @property
    def trades_per_day_30d(self) -> float | None:
        if self.buy_30d is None and self.sell_30d is None:
            return None
        return ((self.buy_30d or 0) + (self.sell_30d or 0)) / 30.0

    def as_dict(self) -> dict:
        d = asdict(self)
        d["last_active_at"] = iso(self.last_active_at)
        d["trades_per_day_30d"] = self.trades_per_day_30d
        return d


def _daily_profit(v: Any) -> list[dict] | None:
    out = []
    for x in _list(v):
        if isinstance(x, dict) and ts(x.get("timestamp")):
            out.append({"date": ts(x.get("timestamp")).date().isoformat(), "profit": f(x.get("profit"))})
    return out or None


def rank_wallets(body: Any) -> Page:
    pg = Page()
    rows = _dict(data(body)).get("rank")
    for n, r in enumerate(_list(rows), start=1):
        addr = s(r.get("wallet_address") or r.get("address")) if isinstance(r, dict) else None
        if not addr:
            pg.skipped += 1
            continue
        pg.items.append(
            WalletRow(
                address=addr,
                rank=n,
                name=s(r.get("name")) or s(r.get("twitter_name")) or s(r.get("nickname")),
                twitter_username=s(r.get("twitter_username")),
                twitter_name=s(r.get("twitter_name")),
                avatar=s(r.get("avatar")),
                tags=[str(t) for t in _list(r.get("tags"))],
                realized_profit_7d=f(r.get("realized_profit_7d")),
                realized_profit_30d=f(r.get("realized_profit_30d")),
                pnl_7d=f(r.get("pnl_7d")),
                pnl_30d=f(r.get("pnl_30d")),
                winrate_7d=f(r.get("winrate_7d")),
                winrate_30d=f(r.get("winrate_30d")),
                buy_30d=i(r.get("buy_30d")),
                sell_30d=i(r.get("sell_30d")),
                txs_30d=i(r.get("txs_30d")),
                avg_holding_sec_30d=f(r.get("avg_holding_period_30d")),
                sol_balance=f(r.get("sol_balance") or r.get("balance")),
                follow_count=i(r.get("follow_count")),
                daily_profit_7d=_daily_profit(r.get("daily_profit_7d")),
                last_active_at=ts(r.get("last_active")),
            )
        )
    return pg


def wallet_new(body: Any, address: str) -> WalletRow | None:
    """walletNew?period=30d: real 7d/30d profit, PnL, buys/sells; win rate is null without login (phase 0)."""
    d = data(body)
    if not isinstance(d, dict) or not d:
        return None
    return WalletRow(
        address=address,
        name=s(d.get("name")) or s(d.get("twitter_name")),
        twitter_username=s(d.get("twitter_username")),
        twitter_name=s(d.get("twitter_name")),
        twitter_fans=i(d.get("twitter_fans_num")),
        avatar=s(d.get("avatar")),
        tags=[str(t) for t in _list(d.get("tags"))],
        realized_profit_7d=f(d.get("realized_profit_7d")),
        realized_profit_30d=f(d.get("realized_profit_30d")),
        pnl_7d=f(d.get("pnl_7d")),
        pnl_30d=f(d.get("pnl_30d")),
        winrate_30d=f(d.get("winrate")),
        buy_30d=i(d.get("buy_30d")),
        sell_30d=i(d.get("sell_30d")),
        sol_balance=f(d.get("sol_balance")),
        last_active_at=ts(d.get("last_active_timestamp")),
    )


def wallet_common_stat(body: Any) -> dict:
    d = _dict(data(body))
    return {
        "name": s(d.get("name")) or s(d.get("nick_name")) or s(d.get("twitter_name")),
        "twitter_username": s(d.get("twitter_username")),
        "twitter_name": s(d.get("twitter_name")),
        "twitter_fans": i(d.get("twitter_fans_num")),
        "avatar": s(d.get("avatar")),
        "tags": [str(t) for t in _list(d.get("tags"))],
        "follow_count": i(d.get("follow_count")),
        "created_token_count": i(d.get("created_token_count")),
        "fund_from_address": s(d.get("fund_from_address")),
        "fund_from": s(d.get("fund_from")),
        "fund_from_at": iso(ts(d.get("fund_from_ts"))),
    }


# ---------- trades (wallet_activity) ----------


@dataclass
class TradeRow:
    wallet: str
    tx_hash: str
    trade_at: datetime
    side: str  # buy | sell
    mint: str
    symbol: str | None
    token_amount: float
    sol_amount: float | None
    quote_symbol: str | None
    usd_amount: float | None
    price_usd: float | None
    price_sol: float | None
    total_supply: float | None
    mcap_usd: float | None
    open_or_close: bool | None
    launchpad: str | None
    launchpad_platform: str | None
    payload: dict


def wallet_activity(body: Any, wallet: str) -> Page:
    d = _dict(data(body))
    pg = Page(next=s(d.get("next")))
    for r in _list(d.get("activities")):
        try:
            side = s(r.get("event_type"))
            token = _dict(r.get("token"))
            mint = s(token.get("address"))
            tx = s(r.get("tx_hash"))
            at = ts(r.get("timestamp"))
            amount = f(r.get("token_amount"))
            if side not in ("buy", "sell") or not mint or not tx or at is None or amount is None:
                pg.skipped += 1
                continue
            quote = _dict(r.get("quote_token"))
            quote_addr = s(r.get("quote_address")) or s(quote.get("token_address"))
            is_sol = quote_addr == WSOL
            price_usd = f(r.get("price_usd"))
            supply = f(token.get("total_supply"))
            pg.items.append(
                TradeRow(
                    wallet=s(r.get("wallet")) or wallet,
                    tx_hash=tx,
                    trade_at=at,
                    side=side,
                    mint=mint,
                    symbol=s(token.get("symbol")),
                    token_amount=amount,
                    sol_amount=f(r.get("quote_amount")) if is_sol else None,
                    quote_symbol="SOL" if is_sol else s(quote.get("symbol")),
                    usd_amount=f(r.get("cost_usd")),
                    price_usd=price_usd,
                    price_sol=f(r.get("price")) if is_sol else None,
                    total_supply=supply,
                    mcap_usd=price_usd * supply if price_usd is not None and supply else None,
                    open_or_close=b(r.get("is_open_or_close")),
                    launchpad=s(r.get("launchpad")),
                    launchpad_platform=s(r.get("launchpad_platform")),
                    payload={
                        "quote_amount": f(r.get("quote_amount")),
                        "quote_address": quote_addr,
                        "buy_cost_usd": f(r.get("buy_cost_usd")),
                        "gas_usd": f(r.get("gas_usd")),
                        "dex_usd": f(r.get("dex_usd")),
                        "priority_fee": f(r.get("priority_fee")),
                        "tip_fee": f(r.get("tip_fee")),
                    },
                )
            )
        except (AttributeError, TypeError):
            pg.skipped += 1
    return pg


# ---------- token parts ----------


def _change(now: float | None, before: float | None) -> float | None:
    if now is None or not before:
        return None
    return now / before - 1.0


def token_window_info(body: Any) -> dict | None:
    rows = _list(data(body))
    d = rows[0] if rows and isinstance(rows[0], dict) else None
    if not d:
        return None
    price = _dict(d.get("price"))
    pool = _dict(d.get("pool"))
    p = f(price.get("price"))
    supply = f(d.get("total_supply"))
    circ = f(d.get("circulating_supply"))
    windows = ("1m", "5m", "1h", "6h", "24h")
    return {
        "info": {
            "mint": s(d.get("address")),
            "symbol": s(d.get("symbol")),
            "name": s(d.get("name")),
            "decimals": i(d.get("decimals")),
            "logo": s(d.get("logo")),
            "total_supply": supply,
            "circulating_supply": circ,
            "holder_count": i(d.get("holder_count")),
            "liquidity_usd": f(d.get("liquidity")),
            "created_at": iso(ts(d.get("creation_timestamp"))),
            "open_at": iso(ts(d.get("open_timestamp"))),
            "migrated_at": iso(ts(d.get("migrated_timestamp"))),
            "pool": {
                "address": s(pool.get("pool_address") or d.get("biggest_pool_address")),
                "exchange": s(pool.get("exchange")),
                "quote_symbol": s(pool.get("quote_symbol")),
                "quote_reserve": f(pool.get("quote_reserve")),
                "initial_liquidity_usd": f(pool.get("initial_liquidity")),
                "created_at": iso(ts(pool.get("creation_timestamp"))),
            },
        },
        "price": {
            "price_usd": p,
            "mcap_usd": p * supply if p is not None and supply else None,
            "change": {w: _change(p, f(price.get(f"price_{w}"))) for w in windows},
            "volume_usd": {w: f(price.get(f"volume_{w}")) for w in windows},
            "buy_volume_usd": {w: f(price.get(f"buy_volume_{w}")) for w in windows},
            "sell_volume_usd": {w: f(price.get(f"sell_volume_{w}")) for w in windows},
            "buys": {w: i(price.get(f"buys_{w}")) for w in windows},
            "sells": {w: i(price.get(f"sells_{w}")) for w in windows},
            "swaps": {w: i(price.get(f"swaps_{w}")) for w in windows},
            "hot_level": i(price.get("hot_level")),
        },
    }


def token_multi_info(body: Any) -> dict | None:
    rows = data(body)
    rows = rows if isinstance(rows, list) else _list(_dict(rows).get("tokens"))
    d = rows[0] if rows and isinstance(rows[0], dict) else None
    if not d:
        return None
    tpool = _dict(d.get("tpool"))
    return {
        "creator": s(d.get("creator_address")),
        "launchpad": s(d.get("launchpad")),
        "launchpad_platform": s(d.get("launchpad_platform")),
        "launchpad_status": i(d.get("launchpad_status")),
        "bonding_progress": f(d.get("launchpad_progress")),
        "migrated": bool(ts(d.get("migrated_timestamp"))) or tpool.get("launch_type") == "migrated",
        "migrated_at": iso(ts(d.get("migrated_timestamp"))),
        "migration_mcap": f(d.get("migration_market_cap")),
        "migration_mcap_quote": s(d.get("migration_market_cap_quote")),
        "exchange": s(tpool.get("exchange")),
        "ath_price_usd": f(d.get("ath_price")),
        "holder_count": i(d.get("holder_count")),
        "liquidity_usd": f(d.get("liquidity")),
    }


def token_security(body: Any) -> dict | None:
    d = data(body)
    if not isinstance(d, dict) or not d:
        return None
    lock = _dict(d.get("lock_summary"))
    return {
        "mint_authority_renounced": b(d.get("renounced_mint")),
        "freeze_authority_renounced": b(d.get("renounced_freeze_account")),
        "top_10_holder_rate": f(d.get("top_10_holder_rate")),
        "burn_ratio": f(d.get("burn_ratio")),
        "burn_status": s(d.get("burn_status")),
        "dev_token_burn_ratio": f(d.get("dev_token_burn_ratio")),
        "buy_tax": f(d.get("buy_tax")),
        "sell_tax": f(d.get("sell_tax")),
        "is_show_alert": b(d.get("is_show_alert")),
        "lp_locked": b(lock.get("is_locked")),
        "lp_lock_percent": f(lock.get("lock_percent")),
    }


def token_dev_info(body: Any) -> dict | None:
    d = data(body)
    if not isinstance(d, dict) or not d:
        return None
    return {
        "creator": s(d.get("creator_address")),
        "creator_token_balance": f(d.get("creator_token_balance")),
        "creator_token_status": s(d.get("creator_token_status")),
        "creator_open_count": i(d.get("creator_open_count")),
        "fund_from": s(d.get("fund_from")),
        "fund_from_at": iso(ts(d.get("fund_from_ts"))),
        "cto_flag": b(d.get("cto_flag")),
        "dexscreener_ad": b(d.get("dexscr_ad")),
        "dexscreener_update_link": b(d.get("dexscr_update_link")),
        "dexscreener_boost_fee": f(d.get("dexscr_boost_fee")),
        "twitter_rename_count": i(d.get("twitter_rename_count")),
        "twitter_del_post_token_count": i(d.get("twitter_del_post_token_count")),
        "twitter_create_token_count": i(d.get("twitter_create_token_count")),
    }


def dev_created_tokens(body: Any, recent: int = 10) -> dict | None:
    d = data(body)
    if not isinstance(d, dict) or not d:
        return None
    ath = _dict(d.get("creator_ath_info"))
    tokens = [t for t in _list(d.get("tokens")) if isinstance(t, dict)]
    tokens.sort(key=lambda t: f(t.get("create_timestamp")) or 0, reverse=True)
    inner, opened = i(d.get("inner_count")), i(d.get("open_count"))
    return {
        "created_total": (inner or 0) + (opened or 0) if inner is not None or opened is not None else None,
        "never_migrated": inner,
        "migrated": opened,
        "migrated_ratio": f(d.get("open_ratio")),
        "last_create_at": iso(ts(d.get("last_create_timestamp"))),
        "ath": {
            "mint": s(ath.get("ath_token")),
            "symbol": s(ath.get("token_symbol")),
            "mcap_usd": f(ath.get("ath_mc")),
        }
        if ath
        else None,
        "listed": len(tokens),
        "recent": [
            {
                "mint": s(t.get("token_address")),
                "symbol": s(t.get("symbol")),
                "created_at": iso(ts(t.get("create_timestamp"))),
                "migrated": b(t.get("is_open")),
                "mcap_usd": f(t.get("market_cap")),
                "ath_mcap_usd": f(t.get("token_ath_mc")),
                "holders": i(t.get("holders")),
                "launchpad_platform": s(t.get("launchpad_platform")),
            }
            for t in tokens[:recent]
        ],
    }


TAG_COUNT_KEYS = (
    "smart_degen_count",
    "renowned_count",
    "sniper_count",
    "bundler_count",
    "insider_count",
    "dev_count",
    "fresh_wallet_count",
    "dex_bot_count",
    "bluechip_owner_count",
    "following_count",
)


def tag_counts(body: Any) -> dict | None:
    d = data(body)
    if not isinstance(d, dict) or not d:
        return None
    return {k.removesuffix("_count").replace("renowned", "kol"): i(d.get(k)) for k in TAG_COUNT_KEYS}


def token_stat(body: Any) -> dict | None:
    d = data(body)
    if not isinstance(d, dict) or not d:
        return None
    return {
        "holder_count": i(d.get("holder_count")),
        "top_10_holder_rate": f(d.get("top_10_holder_rate")),
        "creator_hold_rate": f(d.get("creator_hold_rate")),
        "dev_team_hold_rate": f(d.get("dev_team_hold_rate")),
        "sniper_hold_rate_top70": f(d.get("top70_sniper_hold_rate")),
        "fresh_wallet_rate": f(d.get("fresh_wallet_rate")),
        "bot_degen_rate": f(d.get("bot_degen_rate")),
        "bundler_trader_rate": f(d.get("top_bundler_trader_percentage")),
        "insider_trader_rate": f(d.get("top_rat_trader_percentage")),
        "entrapment_trader_rate": f(d.get("top_entrapment_trader_percentage")),
        "bluechip_owner_rate": f(d.get("bluechip_owner_percentage")),
        "private_vault_hold_rate": f(d.get("private_vault_hold_rate")),
        "creator_created_count": i(d.get("creator_created_count")),
    }


def token_traders(body: Any, tag: str) -> Page:
    """token_traders?tag=renowned|smart_degen rows → smart trader records (`tag` is our name: kol | smart_degen)."""
    d = _dict(data(body))
    pg = Page(next=s(d.get("next")))
    for r in _list(d.get("list")):
        addr = s(r.get("address")) if isinstance(r, dict) else None
        if not addr:
            pg.skipped += 1
            continue
        pg.items.append(
            {
                "address": addr,
                "tag": tag,
                "gmgn_tags": [str(t) for t in _list(r.get("tags"))],
                "buy_usd": f(r.get("buy_volume_cur")),
                "sell_usd": f(r.get("sell_volume_cur")),
                "buys": i(r.get("buy_tx_count_cur")),
                "sells": i(r.get("sell_tx_count_cur")),
                "holding_rate": f(r.get("amount_percentage")),
                "holding_usd": f(r.get("usd_value")),
                "avg_cost_usd": f(r.get("avg_cost")),
                "avg_sold_usd": f(r.get("avg_sold")),
                "profit_usd": f(r.get("profit")),
                "realized_profit_usd": f(r.get("realized_profit")),
                "unrealized_profit_usd": f(r.get("unrealized_profit")),
                "first_at": iso(ts(r.get("start_holding_at"))),
                "exited_at": iso(ts(r.get("end_holding_at"))),
                "last_active_at": iso(ts(r.get("last_active_timestamp"))),
            }
        )
    return pg


# ---------- market lists (light normalization, GMGN field names kept) ----------


def market_rows(body: Any, key: str) -> list[dict]:
    return [r for r in _list(_dict(data(body)).get(key)) if isinstance(r, dict)]


def pump_lists(body: Any) -> dict[str, list[dict]]:
    d = _dict(data(body))
    return {
        "new": [r for r in _list(d.get("new_creation")) if isinstance(r, dict)],
        "completing": [r for r in _list(d.get("pump")) if isinstance(r, dict)],
        "completed": [r for r in _list(d.get("completed")) if isinstance(r, dict)],
    }
