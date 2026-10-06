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
    # P0 (engine token intel) starts this far apart instead of min_gap_sec; None = min_gap_sec
    p0_min_gap_sec: float | None = Field(None, ge=0)
    # tokens a priority leaves free in this group; None = BudgetTunables.reserve
    reserve: dict[str, float] | None = None


def _default_groups() -> dict[str, GroupTunables]:
    return {
        # /vas/: token holder/trader stats + token_traders (engine intel), pump lists, wallet_activity — the
        # challenged group. D-2026-10-05-14: intel first. Burst 5 with P2/P3 leaving 4 tokens, so the 4 vas calls of
        # one survivor (holder_stat, trader_stat, token_traders x2) always find tokens and start 0.25 s apart
        # (min gap 1 s made them take >= 3 s of the engine's 4 s timeout). 12/min: re-challenges followed reopenings
        # that flushed 13-15 req/60 s. [TUNABLE]
        "vas": GroupTunables(
            per_minute=12, burst=5, min_gap_sec=1.0, p0_min_gap_sec=0.25, reserve={"P0": 0, "P1": 0, "P2": 4, "P3": 4}
        ),
        "api": GroupTunables(per_minute=30, burst=8, min_gap_sec=0.3),
        "defi": GroupTunables(per_minute=20, burst=6, min_gap_sec=0.3),
        "mrwapi": GroupTunables(per_minute=20, burst=6, min_gap_sec=0.3),
        # not GMGN: Raydium LaunchLab list API (120 calls at 5 s + 20 at 0.3 s all 200; ~6/min needed)
        "launchlab": GroupTunables(per_minute=20, burst=3, min_gap_sec=1.0),
        # not GMGN: DEXTools pair page (71 calls at a 2 s gap all 200) for the measure-only snipers part
        "dextools": GroupTunables(per_minute=20, burst=3, min_gap_sec=2.0),
    }


class BudgetTunables(BaseModel):
    """Request budget (spec §10). Priorities (D-2026-10-05-14): P0 engine token intel (per Migration survivor, the
    engine waits 4 s), P1 other API calls and chain-event enrichment, P2 pump lists, P3 background (wallet_activity
    sweep, ranks, metrics, other candidate lists). Strict priority inside a group: a lower priority never takes a
    token while a higher one is waiting there."""

    per_minute: int = Field(60, ge=1)  # global cap over all groups
    burst: int = Field(15, ge=1)
    min_gap_sec: float = Field(0.25, ge=0)  # global gap between two request starts
    # [TUNABLE] global gap for P0: one survivor's ~9 intel calls over 4 groups would otherwise need >= 2 s to start
    p0_global_gap_sec: float = Field(0.05, ge=0)
    groups: dict[str, GroupTunables] = Field(default_factory=_default_groups)
    # tokens a priority leaves free in its group (P0/P1 may take the last token)
    reserve: dict[str, float] = Field(default_factory=lambda: {"P0": 0.0, "P1": 0.0, "P2": 1.0, "P3": 2.0})
    # P0 3 s [TUNABLE]: the engine aborts intel after 4 s, so a P0 call that cannot start within 3 s answers
    # "unavailable" at once instead of waiting (e.g. while /vas/ is cooling or another request probes it)
    max_wait_sec: dict[str, float] = Field(
        default_factory=lambda: {"P0": 3.0, "P1": 30.0, "P2": 60.0, "P3": 120.0}
    )
    # per-group cooldown after a 429/403: start, doubling on every consecutive throttle (failed probe), cap; reset by
    # the first success. 2026-10-05: /vas/ blocks outlast 300 s (probes at +300 s kept hitting 429) → cap 3600 s
    cooldown_start_sec: float = 15.0
    cooldown_max_sec: float = 3600.0
    # [TUNABLE] after a success the ladder steps down one level per this many clean seconds (no reset to 15 s):
    # /vas/ was re-challenged 30 s - 20 min after reopenings with the reset-on-success rule (2026-10-05 12:12-13:08)
    cooldown_decay_sec: float = 900.0
    reopen_slow_sec: float = 600.0  # [TUNABLE] after a reopen the group runs at reopen_rate_factor this long
    reopen_rate_factor: float = Field(0.5, gt=0, le=1)  # [TUNABLE] rate and gap factor after a reopen
    # global pause only when this many groups are cooling at the same time
    global_pause_groups: int = 2
    global_pause_sec: float = 60.0

    @model_validator(mode="after")
    def _check(self) -> BudgetTunables:
        for name, g in self.groups.items():
            for p, r in {**self.reserve, **(g.reserve or {})}.items():
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


class TaggedTunables(BaseModel):
    """Watch-only GMGN-tagged wallets for the own `tag_buyers` token part (phase 14, D-2026-10-05-15).

    Ranked wallets that are neither curated nor manual: decoded from the chain only (never a GMGN poll), stored in
    the feed table as `wallet_status = tagged`, left out of /v1/feed unless asked. 5 Oct: 248 ranked, 171 at
    <= 150 trades/day (avg 46.2/day). The public WS closes a connection at its 100th subscription attempt (code 1013,
    probed at 20 ms and 400 ms spacing), so tagged wallets get their own connections of `per_connection` each.
    """

    enabled: bool = True
    tags: list[str] = Field(default_factory=lambda: ["kol", "smart_degen", "renowned"])  # [TUNABLE]
    max_trades_per_day: float = 150.0  # [TUNABLE] same bot filter as the curation
    max_wallets: int = 200  # [TUNABLE]
    per_connection: int = Field(85, ge=1, le=95)  # [TUNABLE] below the 100-attempt cap, room for churn
    buyers_window_min: int = 30  # [TUNABLE] default look-back of the tag_buyers part


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
    # sweep intervals while notified swaps go to the chain decoder (trigger.chain_first and the trigger healthy).
    # The sweep finds what the chain path cannot see: 5 Oct 06:40-16:40 UTC, 95 of 1,458 live trades (6.5 %) were
    # not notified or classified not swap-like, 25 more notified swaps were missed by trigger poll + decoder.
    # 77 wallets (56 warm, 21 cold) → ~133 /vas/ requests/h instead of ~756/h at 300/900 s. [TUNABLE]
    sweep_hot_interval_sec: float = 900.0
    sweep_warm_interval_sec: float = 1800.0
    sweep_cold_interval_sec: float = 3600.0
    sweep_first_spread_sec: float = 900.0  # [TUNABLE] first sweep after a start spread over this (no 77-poll burst)
    # [TUNABLE] a notified swap the decoder could not fetch (RPC failure, dropped) pulls that wallet's sweep to now+this
    miss_poll_sec: float = 60.0
    jitter_frac: float = 0.15
    page_limit: int = Field(20, ge=1, le=50)
    max_pages: int = Field(3, ge=1, le=10)
    max_concurrent: int = Field(2, ge=1)
    first_spread_sec: float = 120.0  # first polls after start are spread over this window
    tick_sec: float = 0.25


class RpcEndpoint(BaseModel):
    url: str
    min_gap_sec: float = Field(2.0, ge=0)  # [TUNABLE] between two calls to this endpoint


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
    # chain decoder (spec §6.2): decode the notified swap from the transaction itself
    chain_fallback: bool = True
    # D-2026-10-05-14: every notified swap goes to the chain decoder, also while /vas/ is open; GMGN wallet_activity
    # only sweeps for trades the chain path cannot see (not notified, classifier misses, decoder misses). False =
    # the old GMGN trigger poll, chain only while /vas/ is closed.
    chain_first: bool = True
    # free public RPCs for getTransaction, used in turn by the soonest free one, each paced on its own:
    # mainnet-beta returned 429 after ~16 calls in 8 s (1 per 2.5 s clean); PublicNode 80/80 at 0.5 s, 60/60 at
    # 1 s, p50 0.27 s (2026-10-05 15:00 UTC) and 300/300 at 0.5 s over 160 s next to gzetryn's own calls (17:05).
    # Bursts: 14 swaps/min queued up to ~60 s on mainnet-beta alone; 58/min (16:35 UTC copy-trade cluster) queued up
    # to 73 s with PublicNode at 1 s → 0.5 s, ≈ 150 calls/min together. [TUNABLE]
    rpc_endpoints: list[RpcEndpoint] = Field(
        default_factory=lambda: [
            RpcEndpoint(url="https://solana-rpc.publicnode.com", min_gap_sec=0.5),
            RpcEndpoint(url="https://api.mainnet-beta.solana.com", min_gap_sec=2.0),
        ]
    )
    rpc_backoff_sec: float = 30.0  # after an RPC 429 (that endpoint only)
    rpc_retries: int = 3  # tx not found yet (RPC index lag) → retry every 2 s
    chain_max_age_sec: float = 120.0  # drop a queued signature older than this (GMGN fallback polling catches it)
    chain_queue_max: int = 200
    sol_usd_window_min: int = 30  # SOL/USD from the median of GMGN trades in the feed over this window
    # [TUNABLE] symbol/supply/SOL price lookups for a chain event wait at most this long for the /api/ budget, then
    # the event is stored without them (5 Oct 15:20-15:35 UTC: bursts of ~26 swaps/min waited 10-22 s on /api/)
    chain_enrich_max_wait_sec: float = 2.0


class CandidatesTunables(BaseModel):
    """Candidate source for the engine (spec §7.1): GMGN lists polled in the background (P3) into a cursor table."""

    enabled: bool = True
    pump_sec: float = 30.0  # POST /vas/api/v1/rank/sol (new / completing / completed) — the challenged group
    pump_limit: int = Field(50, ge=1, le=100)
    # D-2026-10-05-14: skip the pump lists while the pump.fun chain source is healthy (transactions within this many
    # seconds). 5 Oct 12:29-14:40 UTC: chain saw 90 of 92 migrated first (the 2 others completed before the chain
    # source started / during a restart); the engine drops completing and bonding-curve `new` rows, and reads
    # `first` metrics only, so the lists' later tag counts in `last` never reached it. [TUNABLE]
    pump_skip_when_chain_healthy: bool = True
    pump_chain_max_idle_sec: float = 120.0
    new_pairs_sec: float = 30.0  # /api/v1/pairs/sol/new_pairs/1m
    new_pairs_interval: str = "1m"
    new_pairs_limit: int = Field(50, ge=1, le=100)
    trending_sec: float = 60.0  # /defi/quotation/v1/rank/sol/swaps/{interval}
    trending_interval: str = "1h"
    trending_limit: int = Field(50, ge=1, le=100)
    pool_batch: int = Field(5, ge=1, le=5)  # mutil_window_token_info accepts 5 mints (20 → 40000300), verified
    pool_max_per_cycle: int = 20  # new trending mints whose pool is resolved per cycle
    retention_days: int = 7


class PumpChainTunables(BaseModel):
    """pump.fun completing/migrated from the chain (spec §7.2, phase 9 report). Uses the trigger's WS connection."""

    enabled: bool = True
    # GMGN's completing list spanned progress 0.5477-1.0 (113 rows, p05 0.58) → [TUNABLE]
    completing_min_progress: float = 0.55
    update_sec: float = 30.0  # [TUNABLE] at most one `last` update per mint per this many seconds
    flush_sec: float = 1.0  # [TUNABLE] queued candidate writes are flushed this often
    names_cache: int = 200_000  # [TUNABLE] CreateEvent (name, symbol, created) remembered per mint
    track_max: int = 20_000  # [TUNABLE] mints above the threshold kept in memory
    migrated: bool = True  # CompletePumpAmmMigrationEvent → kind migrated (pool from the event, verified 2/2)


class LaunchPathsTunables(BaseModel):
    """Curve path recorder (phase 15, engine research G1, D-2026-10-06-01): per pump.fun launch whose CreateEvent we
    saw, the first `window_sec` of its bonding curve as one compact row. Fed by the pump program stream (no extra
    connection or RPC). ~46k launches/day; ~1k in flight at a 30 min window."""

    enabled: bool = True
    window_sec: int = 1800  # [TUNABLE]
    # [TUNABLE] price checkpoints after the create (s): the last trade price at or before each offset
    checkpoints_sec: list[int] = Field(default_factory=lambda: [10, 30, 60, 120, 300, 600, 900, 1800])
    first_buyers: int = 10  # [TUNABLE] earliest distinct buyers kept per launch
    bundle_slots: int = 1  # [TUNABLE] buys in the create slot and this many slots after count as a bundle
    max_tracked_traders: int = 2000  # [TUNABLE] distinct buyers/sellers counted per launch (memory bound)
    max_in_flight: int = 20_000  # [TUNABLE] launches in memory; the oldest is finalized early beyond this
    flush_sec: float = 5.0  # [TUNABLE] finalized rows are written this often
    retention_days: int = 30  # [TUNABLE]


class LaunchLabTunables(BaseModel):
    """Raydium LaunchLab lists (spec §7.3, phase 10). Rate line `launchlab` in budget.groups."""

    enabled: bool = True
    url: str = "https://launch-mint-v1.raydium.io/get/list"
    new_sec: float = 15.0  # [TUNABLE] sort=new (launches appear 13-119 s after creation; polling faster gains little)
    new_size: int = Field(50, ge=1, le=100)
    last_trade_sec: float = 30.0  # [TUNABLE] sort=lastTrade → completing
    last_trade_size: int = Field(100, ge=1, le=100)
    # [TUNABLE] Raydium's finishingRate percent (100 after migration); lower than token-based progress → 25, not 55
    completing_min_rate: float = 25.0


class DexToolsTunables(BaseModel):
    """DEXTools pair page for the measure-only `snipers` token part (spec §7.4, phase 11)."""

    enabled: bool = True
    url: str = "https://www.dextools.io/shared/data/pair"
    cache_sec: int = 3600  # [TUNABLE] first makers do not change after launch


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
    tagged: TaggedTunables = Field(default_factory=TaggedTunables)
    directory: DirectoryTunables = Field(default_factory=DirectoryTunables)
    watch: WatchTunables = Field(default_factory=WatchTunables)
    trigger: TriggerTunables = Field(default_factory=TriggerTunables)
    candidates: CandidatesTunables = Field(default_factory=CandidatesTunables)
    pump_chain: PumpChainTunables = Field(default_factory=PumpChainTunables)
    launch_paths: LaunchPathsTunables = Field(default_factory=LaunchPathsTunables)
    launchlab: LaunchLabTunables = Field(default_factory=LaunchLabTunables)
    dextools: DexToolsTunables = Field(default_factory=DexToolsTunables)
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
