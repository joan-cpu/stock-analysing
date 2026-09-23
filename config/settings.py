"""Configuration and environment variables.

This module centralises two things:

1. ``Settings`` - runtime/environment configuration loaded from a ``.env`` file
   or the process environment (Anthropic + Dhan credentials, environment
   selection, dry-run flag, ...).
2. ``TradingRules`` - the *immutable* risk parameters loaded from
   ``config/trading_rules.json``. These are frozen so no other layer can mutate
   them at runtime; Layer 3 (``guardrails.py``) reads them but the enforcement
   logic itself is hardcoded in Python.

Broker: Dhan (India). Amounts are in INR. The default environment is the Dhan
**sandbox** (mock) API, so the full pipeline can run end-to-end with no real
money. Switching to live requires deliberate, separate opt-in flags.

Nothing here talks to the network - it only resolves configuration.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
CONFIG_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CONFIG_DIR.parent
TRADING_RULES_PATH = CONFIG_DIR / "trading_rules.json"
ENV_PATH = PROJECT_ROOT / ".env"
DATA_DIR = PROJECT_ROOT / "data"

# Dhan API base URLs (verified against DhanHQ v2 docs).
DHAN_SANDBOX_BASE_URL = "https://sandbox.dhan.co/v2"
DHAN_LIVE_BASE_URL = "https://api.dhan.co/v2"

# Map a plain exchange to Dhan's exchangeSegment enum (equity cash).
EXCHANGE_SEGMENTS: Dict[str, str] = {
    "NSE": "NSE_EQ",
    "BSE": "BSE_EQ",
}


# --------------------------------------------------------------------------- #
# Environment settings
# --------------------------------------------------------------------------- #
class Settings(BaseSettings):
    """Environment-driven configuration.

    Values are read from ``.env`` (if present) and the process environment.
    Field names are case-insensitive against env var names.
    """

    model_config = SettingsConfigDict(
        env_file=str(ENV_PATH),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Anthropic (Layer 2) ---
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-sonnet-5", alias="ANTHROPIC_MODEL")

    # --- Dhan broker (Layer 4) ---
    dhan_client_id: str = Field(default="", alias="DHAN_CLIENT_ID")
    dhan_access_token: str = Field(default="", alias="DHAN_ACCESS_TOKEN")
    # "sandbox" (mock, default) or "live" (real money).
    dhan_env: str = Field(default="sandbox", alias="DHAN_ENV")
    # Second lock for real trading: even with dry_run False and dhan_env live,
    # nothing is sent unless this is explicitly True.
    dhan_enable_live_trading: bool = Field(
        default=False, alias="DHAN_ENABLE_LIVE_TRADING"
    )

    # --- Market defaults ---
    default_exchange: str = Field(default="NSE", alias="DEFAULT_EXCHANGE")
    # CNC = delivery (multi-day swing). INTRADAY/MARGIN/MTF/BO also valid on Dhan.
    product_type: str = Field(default="CNC", alias="PRODUCT_TYPE")

    # --- Safety switch ---
    dry_run: bool = Field(default=True, alias="DRY_RUN")

    # --- Simulation fallback account (used when no Dhan creds are present) ---
    # Defaults mirror the Dhan sandbox's daily virtual capital of Rs 10,00,000.
    simulated_total_equity: float = Field(
        default=1_000_000.0, alias="SIMULATED_TOTAL_EQUITY"
    )
    simulated_cash_balance: float = Field(
        default=1_000_000.0, alias="SIMULATED_CASH_BALANCE"
    )

    @field_validator("dhan_env")
    @classmethod
    def _normalise_env(cls, v: str) -> str:
        v = (v or "sandbox").strip().lower()
        if v not in ("sandbox", "live"):
            raise ValueError("DHAN_ENV must be 'sandbox' or 'live'.")
        return v

    @field_validator("default_exchange")
    @classmethod
    def _normalise_exchange(cls, v: str) -> str:
        v = (v or "NSE").strip().upper()
        if v not in EXCHANGE_SEGMENTS:
            raise ValueError(f"DEFAULT_EXCHANGE must be one of {list(EXCHANGE_SEGMENTS)}.")
        return v

    @property
    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_dhan(self) -> bool:
        return bool(self.dhan_client_id and self.dhan_access_token)

    @property
    def is_sandbox(self) -> bool:
        return self.dhan_env == "sandbox"

    @property
    def base_url(self) -> str:
        return DHAN_SANDBOX_BASE_URL if self.is_sandbox else DHAN_LIVE_BASE_URL

    def auth_headers(self) -> Dict[str, str]:
        """Headers for Dhan v2 REST calls."""
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "access-token": self.dhan_access_token,
        }

    def exchange_segment(self, exchange: str | None = None) -> str:
        ex = (exchange or self.default_exchange).strip().upper()
        if ex not in EXCHANGE_SEGMENTS:
            raise ValueError(f"Unknown exchange '{ex}'. Expected one of {list(EXCHANGE_SEGMENTS)}.")
        return EXCHANGE_SEGMENTS[ex]


# --------------------------------------------------------------------------- #
# Immutable trading rules
# --------------------------------------------------------------------------- #
class TradingRules(BaseModel):
    """Frozen risk parameters (INR). Attempting to set an attribute raises."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    max_position_pct: float = Field(gt=0, le=1)
    stop_loss_pct: float = Field(gt=0, lt=1)
    take_profit_pct: float = Field(gt=0, lt=1)
    max_open_positions: int = Field(gt=0)
    min_trade_notional: float = Field(ge=0)
    allowed_actions: Tuple[str, ...]
    allow_short_selling: bool = False


@lru_cache(maxsize=1)
def load_trading_rules(path: Path = TRADING_RULES_PATH) -> TradingRules:
    """Load and validate the immutable trading rules (cached)."""
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    raw.pop("_comment", None)  # strip the human note before validation
    return TradingRules(**raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()


__all__ = [
    "Settings",
    "TradingRules",
    "get_settings",
    "load_trading_rules",
    "PROJECT_ROOT",
    "DATA_DIR",
    "TRADING_RULES_PATH",
    "EXCHANGE_SEGMENTS",
    "DHAN_SANDBOX_BASE_URL",
    "DHAN_LIVE_BASE_URL",
]
