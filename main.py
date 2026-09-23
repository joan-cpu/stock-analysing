"""Orchestrator - stitches the four layers together sequentially.

Pipeline:  Ingest (L1) -> Analyze (L2) -> Risk Intercept/Validate (L3) -> Execute (L4)

Market: Indian equities via Dhan. Prices/analysis come from Yahoo Finance
(real prices); orders go to the Dhan **sandbox** by default (no real money).

Usage
-----
    python main.py --symbol RELIANCE                 # safe dry-run (default)
    python main.py --symbol TCS --exchange NSE       # choose exchange
    python main.py --symbol INFY --submit            # send to Dhan sandbox
    python main.py --symbol SBIN --qty 1000          # propose a size; L3 downsizes

Safety
------
* Default is dry-run: prints the exact order + risk plan, sends nothing.
* ``--submit`` sends to whatever ``DHAN_ENV`` selects. Sandbox = mock money.
* Live (real money) also requires DHAN_ENV=live AND DHAN_ENABLE_LIVE_TRADING=true.
"""

from __future__ import annotations

import argparse
import logging
import sys

from config.settings import get_settings
from src.analysis.analyst_agent import AnalystAgent, AnalystError
from src.execution.instruments import DhanInstruments, InstrumentError
from src.execution.order_executor import ExecutionError, OrderExecutor
from src.ingestion.data_fetcher import (
    DataFetchError,
    fetch_market_snapshot,
    to_yfinance_ticker,
)
from src.risk.guardrails import RiskInterceptor, RiskViolation, get_account_state


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def run_pipeline(symbol: str, *, exchange: str, submit: bool, proposed_qty: int | None) -> int:
    """Run Ingest -> Analyze -> Risk -> Execute for one Indian equity symbol."""
    settings = get_settings()
    log = logging.getLogger("main")

    dry_run = not submit
    log.info(
        "=== Swing Pipeline | %s (%s) | env=%s | dry_run=%s ===",
        symbol.upper(), exchange.upper(), settings.dhan_env, dry_run,
    )

    # --- Layer 1: Ingest (real prices via Yahoo Finance) ---
    yf_ticker = to_yfinance_ticker(symbol, exchange)
    try:
        snapshot = fetch_market_snapshot(yf_ticker)
    except DataFetchError as exc:
        log.error("Data ingestion failed: %s", exc)
        return 2

    # --- Layer 2: Analyze ---
    try:
        decision = AnalystAgent(settings).analyze(snapshot)
    except AnalystError as exc:
        log.error("Analyst failed: %s", exc)
        return 3
    log.info("Reasoning: %s", decision.reasoning)

    # --- Layer 3: Risk intercept (hard safety) ---
    account = get_account_state(settings)
    log.info("Account: equity=Rs %.2f cash=Rs %.2f (source=%s)",
             account.total_equity, account.cash_balance, account.source)
    try:
        order = RiskInterceptor().validate(
            decision,
            current_price=snapshot.current_price,
            account_state=account,
            proposed_quantity=proposed_qty,
        )
    except RiskViolation as exc:
        log.error("RISK VIOLATION - execution killed: %s", exc)
        return 4

    if not order.is_tradeable:
        log.info("No actionable order (%s). Pipeline complete.", order.action)
        return 0

    # --- Resolve Dhan securityId for the order payload ---
    exchange_segment = settings.exchange_segment(exchange)
    security_id = "<unresolved>"
    try:
        security_id = DhanInstruments().resolve(symbol, exchange)
    except InstrumentError as exc:
        if not dry_run:
            log.error("Cannot resolve securityId for a real order: %s", exc)
            return 5
        log.warning("securityId unresolved (%s); continuing dry-run with placeholder.", exc)

    # --- Layer 4: Execute ---
    try:
        result = OrderExecutor(settings, dry_run=dry_run).execute(
            order, security_id=security_id, exchange_segment=exchange_segment
        )
    except ExecutionError as exc:
        log.error("Execution failed: %s", exc)
        return 6

    log.info("Execution status: %s (submitted=%s, env=%s)",
             result.status, result.submitted, result.environment)
    log.info("=== Pipeline complete ===")
    return 0


def parse_args(argv=None) -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="AI Swing Trading System orchestrator (India / Dhan).")
    parser.add_argument("--symbol", "-s", required=True, help="Plain symbol, e.g. RELIANCE")
    parser.add_argument("--exchange", "-e", default=settings.default_exchange,
                        choices=["NSE", "BSE"], help="Exchange (default from settings).")
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Send the order to Dhan (sandbox unless DHAN_ENV=live). Default is dry-run.",
    )
    parser.add_argument(
        "--qty",
        type=int,
        default=None,
        help="Optional proposed share quantity (risk layer will downsize if needed).",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging.")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    _configure_logging(args.verbose)
    return run_pipeline(
        args.symbol, exchange=args.exchange, submit=args.submit, proposed_qty=args.qty
    )


if __name__ == "__main__":
    sys.exit(main())
