"""Settings from env/.env, tunables from a YAML file. Every number marked [TUNABLE] in the spec lives here."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_DIR = Path(__file__).resolve().parents[2]


class GroupTunables(BaseModel):
    """Limits of one GMGN path group (Cloudflare rate-limits per path prefix: /vas/ was challenged while /api/ and
    /defi/ answered 200, 2026-10-05)."""

    per_minute: float = Field(20.0, gt=0)
    burst: float = Field(4.0, ge=1)
    min_gap_sec: float = Field(1.0, ge=0)


def _default_groups() -> dict[str, GroupTunables]:
    return {
        # /vas/: wallet_activity (feed), token holder/trader stats, token_traders, pump lists — the challenged group
        "vas": GroupTunables(per_minute=20, burst=4, min_gap_sec=1.0),
        "api": GroupTunables(per_minute=30, burst=8, min_gap_sec=0.3),
        "defi": GroupTunables(per_minute=20, burst=6, min_gap_sec=0.3),
        "mrwapi": GroupTunables(per_minute=20, burst=6, min_gap_sec=0.3),
    }


class BudgetTunables(BaseModel):
    """Request budget (spec §10). Priorities: P0 trigger polls, P1 API calls (engine), P2 interval polls,
    P3 background (ranks, metrics, candidates). Strict priority inside a group: a lower priority never takes a token
    while a higher one is waiting there."""

    per_minute: int = Field(60, ge=1)  # global cap over all groups
    burst: int = Field(15, ge=1)
    min_gap_sec: float = Field(0.25, ge=0)  # global gap between two request starts
    groups: dict[str, GroupTunables] = Field(default_factory=_default_groups)
    # tokens a priority leaves free in its group (P0/P1 may take the last token)
    reserve: dict[str, float] = Field(default_factory=lambda: {"P0": 0.0, "P1": 0.0, "P2": 1.0, "P3": 2.0})
    max_wait_sec: dict[str, float] = Field(
        default_factory=lambda: {"P0": 20.0, "P1": 30.0, "P2": 60.0, "P3": 120.0}
    )
    # per-group cooldown after a 429/403: start, doubling, cap; back to the start after `cooldown_reset_sec` clean
    cooldown_start_sec: float = 15.0
    cooldown_max_sec: float = 300.0
    cooldown_reset_sec: float = 900.0
    # global pause only when this many groups are cooling at the same time
    global_pause_groups: int = 2
    global_pause_sec: float = 60.0

    @model_validator(mode="after")
    def _check(self) -> BudgetTunables:
        for name, g in self.groups.items():
            for p, r in self.reserve.items():
                if r >= g.burst:
                    raise ValueError(f"budget: reserve {p}={r} must be < burst of group {name}")
        return self


class TransportTunables(BaseModel):
    impersonate: str = "chrome"
    timeout_sec: float = 15.0
    retry_delay_sec: float = 2.0  # one retry on network/server errors


class CacheTunables(BaseModel):
    max_entries: int = 5000
    ttl_sec: dict[str, int] = Field(
        default_factory=lambda: {
            "rank_wallets": 300,
            "wallet_activity": 0,  # the watcher always wants fresh pages
            "wallet_new": 300,
            "wallet_common_stat": 3600,
            # enricher parts (engine: ~200-300 Migration survivors/day, repeated calls within minutes must be free)
            "token_window_info": 30,
            "token_multi_info": 60,
            "token_security": 600,
            "token_dev_info": 300,
            "dev_created_tokens": 1800,
            "token_stat": 120,
            "token_holder_stat": 120,
            "token_trader_stat": 120,
            "token_traders": 120,
            "rank_swaps": 20,
            "new_pairs": 20,
            "pump_lists": 20,
        }
    )


class RankTunables(BaseModel):
    enabled: bool = True
    interval_sec: int = 3600
    tags: list[str] = Field(default_factory=lambda: ["kol", "smart_degen"])
    periods: list[str] = Field(default_factory=lambda: ["7d", "30d"])
    first_delay_sec: float = 5.0


class CurationTunables(BaseModel):
    """Owner decision 2026-10-03 ("recommended"); every value [TUNABLE]."""

    enabled: bool = True
    interval_sec: int = 86400
    top_n: int = 50
    tags: list[str] = Field(default_factory=lambda: ["kol", "smart_degen"])
    min_profit_30d_usd: float = 0.0  # realized_profit_30d must be > this
    min_pnl_30d: float = 0.0  # pnl_30d (ROI ratio) must be > this
    min_winrate_30d: float = 0.50  # >=
    max_trades_per_day: float = 150.0  # (buy_30d + sell_30d) / 30 <= this; above = bot-paced
    min_trades_30d: int = 20  # buy_30d + sell_30d >= this (owner decision 2026-10-03: too few trades = no evidence)
    min_candidates: int = 50  # fewer candidates (failed rank fetch) → run skipped, nothing changes


class DirectoryTunables(BaseModel):
    manual_metrics_sec: int = 21600  # wallet_stat refresh for manual wallets outside the ranks


class WatchTunables(BaseModel):
    enabled: bool = True
    reload_sec: float = 60.0  # active set reload from the store
    hot_window_sec: int = 1800  # last trade within → hot
    warm_window_sec: int = 86400  # last trade within → warm, else cold
    # intervals used while the on-chain trigger is NOT healthy (first soak at 60/180/600 s: lag p50 122 s)
    hot_interval_sec: float = 45.0
    warm_interval_sec: float = 90.0
    cold_interval_sec: float = 300.0
    # fallback intervals while the trigger is healthy (it catches trades within seconds; these catch misses)
    fallback_hot_interval_sec: float = 120.0
    fallback_warm_interval_sec: float = 300.0
    fallback_cold_interval_sec: float = 900.0
    jitter_frac: float = 0.15
    page_limit: int = Field(20, ge=1, le=50)
    max_pages: int = Field(3, ge=1, le=10)
    max_concurrent: int = Field(2, ge=1)
    first_spread_sec: float = 120.0  # first polls after start are spread over this window
    tick_sec: float = 0.25


class TriggerTunables(BaseModel):
    """On-chain trigger (spec §6.1). Free public endpoint verified 2026-10-03 (phase 7 report)."""

    enabled: bool = True
    ws_url: str = "wss://api.mainnet-beta.solana.com"
    commitment: str = "confirmed"  # measured: +0.07 s vs processed, no rollback risk
    ping_interval_sec: float = 20.0
    reconnect_min_sec: float = 1.0
    reconnect_max_sec: float = 60.0
    debounce_sec: float = 1.0  # first GMGN poll this long after the notification
    # while the tx is not indexed yet. 2026-10-05: 95.4% of trigger events had lag <= 5 s and GMGN indexed a notified
    # signature after p50 1.4 s / p90 2.3 s; the old +15/+31 s retries mostly hit timeouts (1453 of 3849 triggers)
    # and doubled the bursts on the challenged /vas/ group → polls at +1, +3, +7 s only.
    retry_delays_sec: list[float] = Field(default_factory=lambda: [2.0, 4.0])
    max_wait_sec: float = 10.0  # give up on a notified signature after this (fallback polling still runs)
    max_concurrent: int = 2  # triggered polls in flight, separate from interval polls
    min_gap_sec: float = 2.0  # between two triggered polls of one wallet
    max_polls_per_min: int = 10  # triggered polls per wallet per minute; above → left to fallback polling
    sig_cache_sec: float = 1800.0  # notified signatures remembered for coverage stats


class CandidatesTunables(BaseModel):
    """Candidate source for the engine (spec §7.1): GMGN lists polled in the background (P3) into a cursor table."""

    enabled: bool = True
    pump_sec: float = 30.0  # POST /vas/api/v1/rank/sol (new / completing / completed) — the challenged group
    pump_limit: int = Field(50, ge=1, le=100)
    new_pairs_sec: float = 30.0  # /api/v1/pairs/sol/new_pairs/1m
    new_pairs_interval: str = "1m"
    new_pairs_limit: int = Field(50, ge=1, le=100)
    trending_sec: float = 60.0  # /defi/quotation/v1/rank/sol/swaps/{interval}
    trending_interval: str = "1h"
    trending_limit: int = Field(50, ge=1, le=100)
    pool_batch: int = Field(5, ge=1, le=5)  # mutil_window_token_info accepts 5 mints (20 → 40000300), verified
    pool_max_per_cycle: int = 20  # new trending mints whose pool is resolved per cycle
    retention_days: int = 7


class TokenTunables(BaseModel):
    traders_limit: int = 50
    dev_recent_tokens: int = 10
    feed_events: int = 50


class CopyScoreTunables(BaseModel):
    w_profit: float = 0.35
    w_winrate: float = 0.30
    w_positive_days: float = 0.20
    w_presence: float = 0.15
    presence_days: int = 7


class RetentionTunables(BaseModel):
    rank_snapshots_days: int = 180
    request_log_days: int = 30
    samples_days: int = 7
    interval_sec: int = 86400


class ApiTunables(BaseModel):
    max_limit: int = 1000
    feed_max_wait_sec: float = 30.0


class Tunables(BaseModel):
    budget: BudgetTunables = Field(default_factory=BudgetTunables)
    transport: TransportTunables = Field(default_factory=TransportTunables)
    cache: CacheTunables = Field(default_factory=CacheTunables)
    rank: RankTunables = Field(default_factory=RankTunables)
    curation: CurationTunables = Field(default_factory=CurationTunables)
    directory: DirectoryTunables = Field(default_factory=DirectoryTunables)
    watch: WatchTunables = Field(default_factory=WatchTunables)
    trigger: TriggerTunables = Field(default_factory=TriggerTunables)
    candidates: CandidatesTunables = Field(default_factory=CandidatesTunables)
    token: TokenTunables = Field(default_factory=TokenTunables)
    copy_score: CopyScoreTunables = Field(default_factory=CopyScoreTunables)
    retention: RetentionTunables = Field(default_factory=RetentionTunables)
    api: ApiTunables = Field(default_factory=ApiTunables)

    @classmethod
    def from_yaml(cls, path: Path) -> Tunables:
        if not path.exists():
            return cls()
        data = yaml.safe_load(path.read_text()) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path}: top level must be a mapping")
        return cls.model_validate(data)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GZETRYN_",
        env_file=PROJECT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = ""
    db_schema: str | None = None  # search_path override (tests use gzetryn_test)
    host: str = "127.0.0.1"
    port: int = 8793  # 8790 cdexio, 8791 xscout, 8792 bscout on the production VPS
    config_file: Path = PROJECT_DIR / "config" / "gzetryn.yaml"
    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def _async_driver(cls, v: str) -> str:
        if v and not v.startswith("postgresql+asyncpg://"):
            raise ValueError("GZETRYN_DATABASE_URL must start with postgresql+asyncpg://")
        return v

    @property
    def tunables(self) -> Tunables:
        return _load_tunables(self.config_file)


@lru_cache(maxsize=4)
def _load_tunables(path: Path) -> Tunables:
    return Tunables.from_yaml(path)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
