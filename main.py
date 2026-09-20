"""Orchestrator - stitches the four layers together sequentially.

Pipeline:  Ingest (L1) -> Analyze (L2) -> Risk Intercept/Validate (L3) -> Execute (L4)

Usage
-----
    python main.py --ticker AAPL                 # safe dry-run (default)
    python main.py --ticker MSFT --live          # submit to Alpaca *paper* API
    python main.py --ticker TSLA --qty 100       # propose a size; L3 downsizes it

Safety
------
* ``--live`` still only ever hits the Alpaca **paper** endpoint (enforced in L4).
* Without ``--live`` the executor prints the payload and sends nothing.
"""

from __future__ import annotations

import argparse
import logging
import sys

from config.settings import get_settings
from src.analysis.analyst_agent import AnalystAgent, AnalystError
from src.execution.order_executor import ExecutionError, OrderExecutor
from src.ingestion.data_fetcher import DataFetchError, fetch_market_snapshot
from src.risk.guardrails import RiskInterceptor, RiskViolation, get_account_state


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def run_pipeline(ticker: str, *, live: bool, proposed_qty: int | None) -> int:
    """Run the full Ingest -> Analyze -> Risk -> Execute pipeline for one ticker."""
    settings = get_settings()
    log = logging.getLogger("main")

    dry_run = not live
    log.info("=== Swing Trading Pipeline | %s | dry_run=%s ===", ticker.upper(), dry_run)

    # --- Layer 1: Ingest ---
    try:
        snapshot = fetch_market_snapshot(ticker)
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
    log.info("Account: equity=$%.2f cash=$%.2f (source=%s)",
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

    # --- Layer 4: Execute ---
    try:
        result = OrderExecutor(settings, dry_run=dry_run).execute(order)
    except ExecutionError as exc:
        log.error("Execution failed: %s", exc)
        return 5

    log.info("Execution status: %s (submitted=%s, dry_run=%s)",
             result.status, result.submitted, result.dry_run)
    log.info("=== Pipeline complete ===")
    return 0


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI Swing Trading System orchestrator.")
    parser.add_argument("--ticker", "-t", required=True, help="Ticker symbol, e.g. AAPL")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Submit to the Alpaca PAPER API instead of dry-run logging.",
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
    return run_pipeline(args.ticker, live=args.live, proposed_qty=args.qty)


if __name__ == "__main__":
    sys.exit(main())
