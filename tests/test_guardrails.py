"""Unit tests proving the Layer 3 safety controls actually hold.

These tests use NO network and NO LLM - they exercise the deterministic risk
logic directly. Run with:  ``pytest -q``
"""

from __future__ import annotations

import math

import pytest

from config.settings import TradingRules
from src.analysis.analyst_agent import AnalystDecision
from src.risk.guardrails import (
    AccountState,
    RiskInterceptor,
    RiskViolation,
)

# --- Fixtures --------------------------------------------------------------- #

RULES = TradingRules(
    max_position_pct=0.02,
    stop_loss_pct=0.03,
    take_profit_pct=0.06,
    max_open_positions=5,
    min_trade_notional_usd=1.0,
    allowed_actions=("BUY", "SELL", "HOLD"),
    allow_short_selling=True,
)


@pytest.fixture
def interceptor() -> RiskInterceptor:
    return RiskInterceptor(rules=RULES)


@pytest.fixture
def account() -> AccountState:
    # $100k equity => 2% cap = $2,000 per trade.
    return AccountState(total_equity=100_000.0, cash_balance=100_000.0, open_positions=0)


def _decision(action: str, ticker: str = "AAPL", target: float = 200.0) -> AnalystDecision:
    return AnalystDecision(action=action, ticker=ticker, target_price=target, reasoning="test")


# --- Max Position Size Rule ------------------------------------------------- #

def test_position_never_exceeds_2pct_cap(interceptor, account):
    """Auto-sized order must stay at or below 2% of total equity."""
    price = 100.0
    order = interceptor.validate(_decision("BUY"), current_price=price, account_state=account)
    assert order.is_tradeable
    # 2% of 100k = 2000 -> floor(2000/100) = 20 shares = $2,000.
    assert order.quantity == 20
    assert order.notional <= account.total_equity * RULES.max_position_pct + 1e-9
    assert order.risk_pct_of_equity <= 2.0 + 1e-9


def test_oversized_proposal_is_downsized(interceptor, account):
    """A proposed quantity that blows the cap is silently downsized, not sent as-is."""
    price = 100.0
    order = interceptor.validate(
        _decision("BUY"), current_price=price, account_state=account, proposed_quantity=10_000
    )
    assert order.was_downsized is True
    assert order.quantity == 20  # capped to $2,000 notional
    assert order.notional <= account.total_equity * RULES.max_position_pct + 1e-9


def test_small_proposal_is_respected(interceptor, account):
    """A proposal under the cap is honoured and not marked as downsized."""
    order = interceptor.validate(
        _decision("BUY"), current_price=100.0, account_state=account, proposed_quantity=5
    )
    assert order.quantity == 5
    assert order.was_downsized is False


def test_cap_is_bounded_by_available_cash(interceptor):
    """When cash < the 2% notional, sizing uses the smaller cash figure."""
    account = AccountState(total_equity=100_000.0, cash_balance=500.0, open_positions=0)
    order = interceptor.validate(_decision("BUY"), current_price=100.0, account_state=account)
    # min(2000 cap, 500 cash) = 500 -> 5 shares.
    assert order.quantity == 5


# --- Mandatory stop-loss / take-profit -------------------------------------- #

def test_buy_appends_3pct_stop_and_6pct_target(interceptor, account):
    price = 100.0
    order = interceptor.validate(_decision("BUY"), current_price=price, account_state=account)
    assert order.side == "buy"
    assert order.stop_loss_price == pytest.approx(97.0)   # -3%
    assert order.take_profit_price == pytest.approx(106.0)  # +6%


def test_sell_short_inverts_bracket(interceptor, account):
    price = 100.0
    order = interceptor.validate(_decision("SELL"), current_price=price, account_state=account)
    assert order.side == "sell"
    assert order.stop_loss_price == pytest.approx(103.0)   # +3% (stop above)
    assert order.take_profit_price == pytest.approx(94.0)  # -6% (target below)


# --- HOLD ------------------------------------------------------------------- #

def test_hold_produces_no_order(interceptor, account):
    order = interceptor.validate(_decision("HOLD"), current_price=100.0, account_state=account)
    assert order.is_tradeable is False
    assert order.quantity == 0
    assert order.side is None


# --- Hard failures (RiskViolation kills execution) -------------------------- #

def test_invalid_action_raises(interceptor, account):
    bad = AnalystDecision.model_construct(
        action="YOLO", ticker="AAPL", target_price=1.0, reasoning="x"
    )
    with pytest.raises(RiskViolation):
        interceptor.validate(bad, current_price=100.0, account_state=account)


def test_non_positive_price_raises(interceptor, account):
    with pytest.raises(RiskViolation):
        interceptor.validate(_decision("BUY"), current_price=0.0, account_state=account)


def test_insufficient_capital_raises(interceptor):
    """Cap can't fund even one share -> hard violation."""
    account = AccountState(total_equity=100.0, cash_balance=100.0, open_positions=0)
    # 2% of 100 = $2 cap; a $500 share can't be bought.
    with pytest.raises(RiskViolation):
        interceptor.validate(_decision("BUY"), current_price=500.0, account_state=account)


def test_position_cap_blocks_when_max_open_reached(interceptor):
    account = AccountState(total_equity=100_000.0, cash_balance=100_000.0, open_positions=5)
    with pytest.raises(RiskViolation):
        interceptor.validate(_decision("BUY"), current_price=100.0, account_state=account)


def test_short_selling_disabled_blocks_sell():
    rules = RULES.model_copy(update={"allow_short_selling": False})
    interceptor = RiskInterceptor(rules=rules)
    account = AccountState(total_equity=100_000.0, cash_balance=100_000.0)
    with pytest.raises(RiskViolation):
        interceptor.validate(_decision("SELL"), current_price=100.0, account_state=account)


# --- Property-style invariant ----------------------------------------------- #

@pytest.mark.parametrize("price", [1.0, 12.5, 50.0, 123.45, 999.99, 4000.0])
@pytest.mark.parametrize("equity", [10_000.0, 50_000.0, 250_000.0])
def test_notional_never_exceeds_cap_invariant(interceptor, price, equity):
    account = AccountState(total_equity=equity, cash_balance=equity, open_positions=0)
    cap = equity * RULES.max_position_pct
    if math.floor(min(cap, equity) / price) < 1:
        with pytest.raises(RiskViolation):
            interceptor.validate(_decision("BUY"), current_price=price, account_state=account)
    else:
        order = interceptor.validate(
            _decision("BUY"), current_price=price, account_state=account, proposed_quantity=10**9
        )
        assert order.notional <= cap + 1e-9
