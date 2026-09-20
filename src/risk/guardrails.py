"""Layer 3 - The Risk Interceptor  (CRITICAL SAFETY LAYER).

This layer is **100% hardcoded, deterministic Python**. It contains NO LLM calls
and makes NO AI-driven choices. It is the last line of defence between a model's
suggestion and real (or paper) capital.

Responsibilities
----------------
1. Fetch current account metrics (cash / equity) - from Alpaca if configured,
   otherwise from the simulation values in ``Settings``.
2. Reject any structurally invalid or unsafe decision with a hard
   :class:`RiskViolation` (kills execution).
3. Enforce the **Max Position Size Rule**: never allocate more than
   ``max_position_pct`` (default 2%) of total equity to a single trade. Oversized
   proposals are automatically **downsized** to the cap.
4. Append a mandatory **stop-loss** (default 3%) and **take-profit** (default 6%)
   to every actionable order.

The numeric parameters come from the immutable ``config/trading_rules.json``;
the *enforcement logic* lives here and cannot be overridden by configuration or
by the model.
"""

from __future__ import annotations

import logging
import math
from typing import Literal, Optional

from pydantic import BaseModel, Field

from config.settings import Settings, TradingRules, get_settings, load_trading_rules
from src.analysis.analyst_agent import AnalystDecision

logger = logging.getLogger(__name__)

Side = Literal["buy", "sell"]


class RiskViolation(RuntimeError):
    """Rigid, execution-killing error raised when a trade cannot be made safe."""


class AccountState(BaseModel):
    """Snapshot of account metrics the risk layer sizes against."""

    total_equity: float = Field(gt=0)
    cash_balance: float = Field(ge=0)
    open_positions: int = Field(default=0, ge=0)
    source: str = "simulation"


class ValidatedOrder(BaseModel):
    """A fully risk-checked order ready for Layer 4.

    For a HOLD (or any non-actionable outcome) ``is_tradeable`` is False and the
    quantity is zero; Layer 4 must skip these.
    """

    ticker: str
    action: str
    side: Optional[Side] = None
    is_tradeable: bool
    quantity: int = 0
    reference_price: float = 0.0
    notional: float = 0.0
    max_allowed_notional: float = 0.0
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    risk_pct_of_equity: float = 0.0
    was_downsized: bool = False
    reasoning: str = ""
    notes: str = ""


def get_account_state(settings: Optional[Settings] = None) -> AccountState:
    """Fetch current account metrics.

    Uses the Alpaca account endpoint when paper keys are configured; otherwise
    falls back to the simulated equity/cash from ``Settings``. Network failures
    degrade gracefully to simulation so the pipeline stays runnable.
    """
    settings = settings or get_settings()

    if settings.has_alpaca:
        try:
            import requests

            resp = requests.get(
                f"{settings.alpaca_base_url.rstrip('/')}/v2/account",
                headers={
                    "APCA-API-KEY-ID": settings.alpaca_api_key,
                    "APCA-API-SECRET-KEY": settings.alpaca_secret_key,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return AccountState(
                total_equity=float(data.get("equity", data.get("portfolio_value", 0))),
                cash_balance=float(data.get("cash", 0)),
                open_positions=0,
                source="alpaca",
            )
        except Exception as exc:  # noqa: BLE001 - fall back to simulation
            logger.warning("Alpaca account fetch failed (%s); using simulation.", exc)

    return AccountState(
        total_equity=settings.simulated_total_equity,
        cash_balance=settings.simulated_cash_balance,
        open_positions=0,
        source="simulation",
    )


class RiskInterceptor:
    """Deterministic validator that intercepts Layer 2 output before execution."""

    def __init__(self, rules: Optional[TradingRules] = None) -> None:
        self.rules = rules or load_trading_rules()

    # ---- public API ---- #
    def validate(
        self,
        decision: AnalystDecision,
        current_price: float,
        account_state: AccountState,
        proposed_quantity: Optional[int] = None,
    ) -> ValidatedOrder:
        """Validate and (if needed) downsize a decision into a safe order.

        Raises
        ------
        RiskViolation
            If the decision is structurally invalid, the price is non-positive,
            the action is not permitted, or the account cannot fund even one share
            within the risk cap.
        """
        action = decision.action.upper()

        # --- Structural guards (hard failures) ---
        if action not in self.rules.allowed_actions:
            raise RiskViolation(
                f"Action '{action}' is not in allowed actions {self.rules.allowed_actions}."
            )
        if action == "SELL" and not self.rules.allow_short_selling:
            raise RiskViolation("SELL blocked: short selling is disabled in trading rules.")

        # --- HOLD short-circuit: no order, no risk ---
        if action == "HOLD":
            return ValidatedOrder(
                ticker=decision.ticker,
                action="HOLD",
                is_tradeable=False,
                reasoning=decision.reasoning,
                notes="HOLD signal - no order generated.",
            )

        if current_price <= 0:
            raise RiskViolation(f"Non-positive current price ({current_price}); cannot size trade.")
        if account_state.total_equity <= 0:
            raise RiskViolation("Account total equity is non-positive; refusing to trade.")
        if account_state.open_positions >= self.rules.max_open_positions:
            raise RiskViolation(
                f"Open positions ({account_state.open_positions}) at or above cap "
                f"({self.rules.max_open_positions})."
            )

        # --- Max Position Size Rule (2% of equity, also capped by cash) ---
        max_notional = account_state.total_equity * self.rules.max_position_pct
        fundable_notional = min(max_notional, account_state.cash_balance)
        cap_qty = math.floor(fundable_notional / current_price)

        if cap_qty < 1:
            raise RiskViolation(
                f"Risk cap of ${max_notional:,.2f} (cash ${account_state.cash_balance:,.2f}) "
                f"cannot fund a single share at ${current_price:,.2f}."
            )

        # Size the trade: honour a proposed quantity but never exceed the cap.
        if proposed_quantity is None:
            quantity = cap_qty
            was_downsized = False
        else:
            if proposed_quantity < 1:
                raise RiskViolation(f"Proposed quantity {proposed_quantity} is below 1 share.")
            quantity = min(proposed_quantity, cap_qty)
            was_downsized = quantity < proposed_quantity

        notional = round(quantity * current_price, 2)
        if notional < self.rules.min_trade_notional_usd:
            raise RiskViolation(
                f"Trade notional ${notional:,.2f} below minimum "
                f"${self.rules.min_trade_notional_usd:,.2f}."
            )

        # --- Mandatory stop-loss / take-profit ---
        side, stop_loss, take_profit = self._bracket(action, current_price)

        order = ValidatedOrder(
            ticker=decision.ticker,
            action=action,
            side=side,
            is_tradeable=True,
            quantity=quantity,
            reference_price=round(current_price, 4),
            notional=notional,
            max_allowed_notional=round(max_notional, 2),
            stop_loss_price=stop_loss,
            take_profit_price=take_profit,
            risk_pct_of_equity=round(notional / account_state.total_equity * 100, 4),
            was_downsized=was_downsized,
            reasoning=decision.reasoning,
            notes=(
                f"Downsized to risk cap ({self.rules.max_position_pct:.0%} of equity)."
                if was_downsized else "Within risk cap."
            ),
        )
        logger.info(
            "Validated %s %s x%d @ %.2f (notional $%.2f = %.2f%% equity) SL=%.2f TP=%.2f%s",
            order.side, order.ticker, order.quantity, order.reference_price,
            order.notional, order.risk_pct_of_equity, order.stop_loss_price,
            order.take_profit_price, " [DOWNSIZED]" if was_downsized else "",
        )
        return order

    # ---- internals ---- #
    def _bracket(self, action: str, price: float):
        """Compute (side, stop_loss, take_profit) for a BUY (long) or SELL (short)."""
        sl_pct = self.rules.stop_loss_pct
        tp_pct = self.rules.take_profit_pct
        if action == "BUY":
            return "buy", round(price * (1 - sl_pct), 2), round(price * (1 + tp_pct), 2)
        # SELL == short entry: stop above, target below.
        return "sell", round(price * (1 + sl_pct), 2), round(price * (1 - tp_pct), 2)


__all__ = [
    "RiskInterceptor",
    "RiskViolation",
    "AccountState",
    "ValidatedOrder",
    "get_account_state",
]
