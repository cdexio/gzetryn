"""Settings from env/.env, tunables from a YAML file. Every number marked [TUNABLE] in the spec lives here."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_DIR = Path(__file__).resolve().parents[2]


class BudgetTunables(BaseModel):
    per_minute: int = Field(40, ge=1)  # sustained requests per minute to GMGN, all priorities together
    burst: int = Field(8, ge=1)  # token bucket capacity
    min_gap_sec: float = Field(0.3, ge=0)  # minimum time between two request starts
    reserve_p1: float = 2.0  # tokens P1 leaves for P0
    reserve_p2: float = 4.0  # tokens P2 leaves for P0/P1
    max_wait_sec: dict[str, float] = Field(default_factory=lambda: {"P0": 10.0, "P1": 60.0, "P2": 120.0})
    throttle_pause_sec: float = 120.0  # first pause after 429/403, doubles
    throttle_pause_max_sec: float = 1800.0
    throttle_reset_sec: float = 1800.0  # clean time after which the pause resets to the first step

    @model_validator(mode="after")
    def _reserves(self) -> BudgetTunables:
        if not 0 <= self.reserve_p1 <= self.reserve_p2 < self.burst:
            raise ValueError("budget: need 0 <= reserve_p1 <= reserve_p2 < burst")
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
            "token_window_info": 30,
            "token_multi_info": 30,
            "token_security": 120,
            "token_dev_info": 60,
            "dev_created_tokens": 600,
            "token_stat": 45,
            "token_holder_stat": 45,
            "token_trader_stat": 45,
            "token_traders": 45,
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
    min_candidates: int = 50  # fewer candidates (failed rank fetch) → run skipped, nothing changes


class DirectoryTunables(BaseModel):
    manual_metrics_sec: int = 21600  # wallet_stat refresh for manual wallets outside the ranks


class WatchTunables(BaseModel):
    enabled: bool = True
    reload_sec: float = 60.0  # active set reload from the store
    hot_window_sec: int = 1800  # last trade within → hot
    warm_window_sec: int = 86400  # last trade within → warm, else cold
    hot_interval_sec: float = 60.0
    warm_interval_sec: float = 180.0
    cold_interval_sec: float = 600.0
    jitter_frac: float = 0.15
    page_limit: int = Field(20, ge=1, le=50)
    max_pages: int = Field(3, ge=1, le=10)
    max_concurrent: int = Field(2, ge=1)
    first_spread_sec: float = 120.0  # first polls after start are spread over this window
    tick_sec: float = 1.0


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
