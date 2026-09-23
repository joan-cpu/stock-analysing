"""Layer 4 - Executor (Dhan).

Turns a risk-validated :class:`ValidatedOrder` into a Dhan v2 order payload and
either:

* ``dry_run=True``  (DEFAULT) - logs the exact payload + risk plan and returns a
  simulated acknowledgement. No network call. The safe path.
* ``dry_run=False`` - submits the order via the Dhan REST API. Which environment
  it hits is decided by ``DHAN_ENV``:
    - ``sandbox`` (default): the Dhan mock API. No real money, fills are simulated.
    - ``live``: real money - and additionally gated behind
      ``DHAN_ENABLE_LIVE_TRADING=true`` so a real order can never fire by accident.

Stop-loss / take-profit handling:
    For CNC (delivery / multi-day swing) the entry is a market order and the
    SL/TP levels computed by Layer 3 are attached to the result as a *risk plan*
    (in the Indian cash market these are placed as separate GTT/exit orders, a
    documented extension point). For the bracket product ``BO`` (intraday) the
    SL/TP are attached directly to the order via ``boStopLossValue`` /
    ``boProfitValue``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from pydantic import BaseModel

from config.settings import Settings, get_settings
from src.risk.guardrails import ValidatedOrder

logger = logging.getLogger(__name__)


class ExecutionResult(BaseModel):
    """Outcome of an execution attempt."""

    submitted: bool
    dry_run: bool
    environment: str
    status: str
    payload: Dict[str, Any]
    risk_plan: Dict[str, Any] = {}
    broker_response: Optional[Dict[str, Any]] = None


class ExecutionError(RuntimeError):
    """Raised when a submission cannot proceed safely."""


class OrderExecutor:
    """Builds and (optionally) submits Dhan order payloads."""

    def __init__(self, settings: Optional[Settings] = None, dry_run: Optional[bool] = None) -> None:
        self.settings = settings or get_settings()
        # Explicit arg wins; otherwise fall back to the settings/.env value.
        self.dry_run = self.settings.dry_run if dry_run is None else dry_run

    def build_payload(
        self, order: ValidatedOrder, security_id: str, exchange_segment: str
    ) -> Dict[str, Any]:
        """Construct a Dhan v2 order payload from a validated order."""
        product = self.settings.product_type.upper()
        payload: Dict[str, Any] = {
            "dhanClientId": self.settings.dhan_client_id,
            "transactionType": "BUY" if order.side == "buy" else "SELL",
            "exchangeSegment": exchange_segment,
            "productType": product,
            "orderType": "MARKET",
            "validity": "DAY",
            "securityId": str(security_id),
            "quantity": int(order.quantity),
            "price": 0,
            "triggerPrice": 0,
            "disclosedQuantity": 0,
        }
        # BO (bracket, intraday) can carry SL/TP directly as point offsets.
        if product == "BO" and order.stop_loss_price and order.take_profit_price:
            payload["boProfitValue"] = round(abs(order.take_profit_price - order.reference_price), 2)
            payload["boStopLossValue"] = round(abs(order.reference_price - order.stop_loss_price), 2)
        return payload

    def _risk_plan(self, order: ValidatedOrder) -> Dict[str, Any]:
        return {
            "reference_price": order.reference_price,
            "stop_loss_price": order.stop_loss_price,
            "take_profit_price": order.take_profit_price,
            "note": (
                "For CNC, place SL/TP as separate GTT/exit orders. "
                "For BO they are attached to the order above."
            ),
        }

    def execute(
        self, order: ValidatedOrder, security_id: str, exchange_segment: str
    ) -> ExecutionResult:
        """Execute (or simulate) a validated order."""
        env = self.settings.dhan_env

        if not order.is_tradeable:
            logger.info("Skipping non-tradeable order for %s (%s).", order.ticker, order.action)
            return ExecutionResult(
                submitted=False, dry_run=self.dry_run, environment=env,
                status="skipped_hold", payload={},
            )

        payload = self.build_payload(order, security_id, exchange_segment)
        risk_plan = self._risk_plan(order)

        if self.dry_run:
            logger.info(
                "[DRY-RUN] Would submit to Dhan %s:\n%s\nRisk plan: %s",
                env, json.dumps(payload, indent=2), json.dumps(risk_plan),
            )
            return ExecutionResult(
                submitted=False, dry_run=True, environment=env,
                status="dry_run_logged", payload=payload, risk_plan=risk_plan,
            )

        # --- Real submission path ---
        if not self.settings.has_dhan:
            raise ExecutionError(
                "dry_run is False but Dhan credentials are missing. Refusing to send."
            )
        if env == "live" and not self.settings.dhan_enable_live_trading:
            raise ExecutionError(
                "LIVE order blocked: DHAN_ENV=live requires DHAN_ENABLE_LIVE_TRADING=true. "
                "This is the real-money safety lock."
            )

        from src.execution.dhan_client import DhanClient

        client = DhanClient(self.settings)
        broker_response = client.place_order(payload)
        logger.info(
            "Order submitted to Dhan %s. orderId=%s status=%s",
            env, broker_response.get("orderId"), broker_response.get("orderStatus"),
        )
        return ExecutionResult(
            submitted=True, dry_run=False, environment=env,
            status=f"submitted_{env}", payload=payload, risk_plan=risk_plan,
            broker_response=broker_response,
        )


__all__ = ["OrderExecutor", "ExecutionResult", "ExecutionError"]
