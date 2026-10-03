"""Every GMGN endpoint gzetryn calls (phase 0 report, verified 2026-10-03). The only place that knows their paths."""

from __future__ import annotations

from dataclasses import dataclass, field

BASE = "https://gmgn.ai"
CHAIN = "sol"


@dataclass(frozen=True)
class Endpoint:
    name: str  # also the cache/TTL key and the counter label
    path: str  # template: {chain} {address} {mint} {period} {interval}
    method: str = "GET"
    fixed: dict[str, str | list[str]] = field(default_factory=dict)  # query params always sent

    def url(self, **path_params: str) -> str:
        return BASE + self.path.format(chain=CHAIN, **path_params)


RANK_WALLETS = Endpoint("rank_wallets", "/defi/quotation/v1/rank/{chain}/wallets/{period}", fixed={"direction": "desc"})
WALLET_ACTIVITY = Endpoint(
    "wallet_activity", "/vas/api/v1/wallet_activity/{chain}", fixed={"type": ["buy", "sell"]}
)
# wallet_stat/{period} is not used: without login every period metric is 0 (phase 0). walletNew has real 7d/30d values.
WALLET_NEW = Endpoint("wallet_new", "/defi/quotation/v1/smartmoney/{chain}/walletNew/{address}", fixed={"period": "30d"})
WALLET_COMMON_STAT = Endpoint("wallet_common_stat", "/api/v1/wallet_common_stat/{chain}/{address}")

TOKEN_WINDOW_INFO = Endpoint("token_window_info", "/api/v1/mutil_window_token_info", method="POST")
# creator_address (filled even when token_dev_info blanks it) + launchpad / bonding progress
TOKEN_MULTI_INFO = Endpoint("token_multi_info", "/mrwapi/v1/multi_token_info", method="POST")
TOKEN_SECURITY = Endpoint("token_security", "/api/v1/token_security_sol/{chain}/{mint}")
TOKEN_DEV_INFO = Endpoint("token_dev_info", "/api/v1/token_dev_info/{chain}/{mint}")
DEV_CREATED_TOKENS = Endpoint("dev_created_tokens", "/api/v1/dev_created_tokens/{chain}/{address}")
TOKEN_STAT = Endpoint("token_stat", "/api/v1/token_stat/{chain}/{mint}")
TOKEN_HOLDER_STAT = Endpoint("token_holder_stat", "/vas/api/v1/token_holder_stat/{chain}/{mint}")
TOKEN_TRADER_STAT = Endpoint("token_trader_stat", "/vas/api/v1/token_trader_stat/{chain}/{mint}")
TOKEN_TRADERS = Endpoint(
    "token_traders", "/vas/api/v1/token_traders/{chain}/{mint}", fixed={"orderby": "profit", "direction": "desc"}
)

RANK_SWAPS = Endpoint("rank_swaps", "/defi/quotation/v1/rank/{chain}/swaps/{interval}", fixed={"orderby": "swaps", "direction": "desc"})
NEW_PAIRS = Endpoint(
    "new_pairs", "/api/v1/pairs/{chain}/new_pairs/{interval}", fixed={"orderby": "open_timestamp", "direction": "desc"}
)
PUMP_LISTS = Endpoint("pump_lists", "/vas/api/v1/rank/{chain}", method="POST")

ALL = {
    e.name: e
    for e in (
        RANK_WALLETS,
        WALLET_ACTIVITY,
        WALLET_NEW,
        WALLET_COMMON_STAT,
        TOKEN_WINDOW_INFO,
        TOKEN_MULTI_INFO,
        TOKEN_SECURITY,
        TOKEN_DEV_INFO,
        DEV_CREATED_TOKENS,
        TOKEN_STAT,
        TOKEN_HOLDER_STAT,
        TOKEN_TRADER_STAT,
        TOKEN_TRADERS,
        RANK_SWAPS,
        NEW_PAIRS,
        PUMP_LISTS,
    )
}

PERIODS = ("7d", "30d")
RANK_PERIODS = ("1d", "7d", "30d")
INTERVALS = ("1m", "5m", "1h", "6h", "24h")
TRADER_TAGS = {"kol": "renowned", "smart_degen": "smart_degen"}  # our name → GMGN token_traders tag


def pump_lists_body(limit: int, platform: str = "Pump.fun") -> dict:
    """POST body for /vas/api/v1/rank/sol (phase 0: without launchpad_platform all lists are empty)."""
    part = {"limit": limit, "launchpad_platform_v2": True, "launchpad_platform": [platform]}
    return {"new_creation": dict(part), "near_completion": dict(part), "completed": dict(part), "version": "v2"}
