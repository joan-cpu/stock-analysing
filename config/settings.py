"""Configuration and environment variables.

This module centralises two things:

1. ``Settings`` - runtime/environment configuration loaded from a ``.env`` file
   or the process environment (API keys, model id, dry-run flag, ...).
2. ``TradingRules`` - the *immutable* risk parameters loaded from
   ``config/trading_rules.json``. These are frozen so no other layer can mutate
   them at runtime; Layer 3 (``guardrails.py``) reads them but the enforcement
   logic itself is hardcoded in Python.

Nothing here talks to the network - it only resolves configuration.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Tuple

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
CONFIG_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CONFIG_DIR.parent
TRADING_RULES_PATH = CONFIG_DIR / "trading_rules.json"
ENV_PATH = PROJECT_ROOT / ".env"


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

    # --- Alpaca broker (Layer 4) ---
    alpaca_api_key: str = Field(default="", alias="ALPACA_API_KEY")
    alpaca_secret_key: str = Field(default="", alias="ALPACA_SECRET_KEY")
    alpaca_base_url: str = Field(
        default="https://paper-api.alpaca.markets", alias="ALPACA_BASE_URL"
    )

    # --- Safety switch ---
    dry_run: bool = Field(default=True, alias="DRY_RUN")

    # --- Simulation fallback account (used when no broker keys are present) ---
    simulated_total_equity: float = Field(
        default=100_000.0, alias="SIMULATED_TOTAL_EQUITY"
    )
    simulated_cash_balance: float = Field(
        default=100_000.0, alias="SIMULATED_CASH_BALANCE"
    )

    @property
    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_alpaca(self) -> bool:
        return bool(self.alpaca_api_key and self.alpaca_secret_key)

    @property
    def is_live_endpoint(self) -> bool:
        """True if the configured broker URL is NOT a paper endpoint."""
        return "paper" not in self.alpaca_base_url.lower()


# --------------------------------------------------------------------------- #
# Immutable trading rules
# --------------------------------------------------------------------------- #
class TradingRules(BaseModel):
    """Frozen risk parameters. Attempting to set an attribute raises."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    max_position_pct: float = Field(gt=0, le=1)
    stop_loss_pct: float = Field(gt=0, lt=1)
    take_profit_pct: float = Field(gt=0, lt=1)
    max_open_positions: int = Field(gt=0)
    min_trade_notional_usd: float = Field(ge=0)
    allowed_actions: Tuple[str, ...]
    allow_short_selling: bool = True


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
    "TRADING_RULES_PATH",
]
