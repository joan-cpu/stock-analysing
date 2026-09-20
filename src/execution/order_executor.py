"""Layer 4 - Executor.

Turns a risk-validated :class:`ValidatedOrder` into a broker payload (Alpaca
bracket order) and either:

* ``dry_run=True``  (DEFAULT) - logs the exact payload and returns a simulated
  acknowledgement. No network call is made. This is the safe path.
* ``dry_run=False`` - POSTs the order to the configured Alpaca **paper** endpoint.

The executor refuses to place non-tradeable orders (e.g. HOLD) and refuses to
send anything if broker credentials are missing.
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
    status: str
    payload: Dict[str, Any]
    broker_response: Optional[Dict[str, Any]] = None


class ExecutionError(RuntimeError):
    """Raised when a live submission cannot proceed safely."""


class OrderExecutor:
    """Builds and (optionally) submits broker order payloads."""

    def __init__(self, settings: Optional[Settings] = None, dry_run: Optional[bool] = None) -> None:
        self.settings = settings or get_settings()
        # Explicit arg wins; otherwise fall back to the settings/.env value.
        self.dry_run = self.settings.dry_run if dry_run is None else dry_run

    def build_payload(self, order: ValidatedOrder) -> Dict[str, Any]:
        """Construct an Alpaca bracket-order payload from a validated order."""
        return {
            "symbol": order.ticker,
            "qty": str(order.quantity),
            "side": order.side,
            "type": "market",
            "time_in_force": "gtc",
            "order_class": "bracket",
            "take_profit": {"limit_price": order.take_profit_price},
            "stop_loss": {"stop_price": order.stop_loss_price},
        }

    def execute(self, order: ValidatedOrder) -> ExecutionResult:
        """Execute (or simulate) a validated order."""
        if not order.is_tradeable:
            logger.info("Skipping non-tradeable order for %s (%s).", order.ticker, order.action)
            return ExecutionResult(
                submitted=False,
                dry_run=self.dry_run,
                status="skipped_hold",
                payload={},
            )

        payload = self.build_payload(order)

        if self.dry_run:
            logger.info(
                "[DRY-RUN] Would submit order payload:\n%s",
                json.dumps(payload, indent=2),
            )
            return ExecutionResult(
                submitted=False,
                dry_run=True,
                status="dry_run_logged",
                payload=payload,
            )

        # --- Live (paper) submission path ---
        if not self.settings.has_alpaca:
            raise ExecutionError(
                "dry_run is False but Alpaca credentials are missing. Refusing to send."
            )
        if self.settings.is_live_endpoint:
            raise ExecutionError(
                "Refusing to submit: ALPACA_BASE_URL is not a paper endpoint. "
                "This scaffold only supports paper trading."
            )

        try:
            import requests

            resp = requests.post(
                f"{self.settings.alpaca_base_url.rstrip('/')}/v2/orders",
                headers={
                    "APCA-API-KEY-ID": self.settings.alpaca_api_key,
                    "APCA-API-SECRET-KEY": self.settings.alpaca_secret_key,
                    "Content-Type": "application/json",
                },
                data=json.dumps(payload),
                timeout=15,
            )
            resp.raise_for_status()
            broker_response = resp.json()
        except Exception as exc:  # noqa: BLE001 - surface broker failures
            raise ExecutionError(f"Order submission failed: {exc}") from exc

        logger.info("Order submitted for %s (paper). Broker id=%s",
                    order.ticker, broker_response.get("id"))
        return ExecutionResult(
            submitted=True,
            dry_run=False,
            status="submitted_paper",
            payload=payload,
            broker_response=broker_response,
        )


__all__ = ["OrderExecutor", "ExecutionResult", "ExecutionError"]
