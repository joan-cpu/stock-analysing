"""Layer 1 - Data ingestion.

Fetches historical price action from Yahoo Finance (``yfinance``) and computes
a small set of swing-trading technical metrics:

* RSI (14)              - momentum oscillator
* SMA 50 / SMA 200      - trend regime (golden / death cross)
* 4-hour price action   - recent intraday candles (resampled from 1h bars)

The output is a validated :class:`MarketSnapshot` pydantic model, which serialises
cleanly to JSON for the analyst agent (Layer 2).

Note on intervals: Yahoo Finance does not expose a native 4h bar, so the recent
price action is resampled from 1h bars. The 50/200-day SMAs and RSI are computed
on *daily* closes, which is the conventional basis for those swing indicators.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

import pandas as pd
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# yfinance is imported lazily inside the fetch so that unit tests and the risk
# layer can import this module without a network-capable environment.


class Candle(BaseModel):
    """A single OHLCV candle."""

    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class MarketSnapshot(BaseModel):
    """Structured market data + technical metrics for one ticker."""

    ticker: str
    as_of: str = Field(description="UTC ISO-8601 timestamp of the snapshot")
    current_price: float
    rsi_14: Optional[float] = None
    sma_50: Optional[float] = None
    sma_200: Optional[float] = None
    trend_regime: str = "unknown"
    price_vs_sma50_pct: Optional[float] = None
    recent_4h_candles: List[Candle] = Field(default_factory=list)

    def to_analyst_json(self) -> str:
        """Compact JSON payload handed to the LLM analyst."""
        return self.model_dump_json(indent=2)


class DataFetchError(RuntimeError):
    """Raised when market data cannot be retrieved or is unusable."""


def _compute_rsi(close: pd.Series, period: int = 14) -> Optional[float]:
    """Wilder's RSI on a close-price series. Returns the latest value."""
    if len(close) <= period:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    latest = rsi.iloc[-1]
    return None if pd.isna(latest) else round(float(latest), 2)


def _latest_sma(close: pd.Series, window: int) -> Optional[float]:
    """Simple moving average over ``window`` closes; None if insufficient data."""
    if len(close) < window:
        return None
    return round(float(close.rolling(window=window).mean().iloc[-1]), 4)


def _resample_4h(intraday: pd.DataFrame) -> pd.DataFrame:
    """Resample 1h OHLCV bars into 4h bars."""
    agg = {
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }
    return intraday.resample("4h").agg(agg).dropna(how="any")


def fetch_market_snapshot(
    ticker: str,
    *,
    daily_period: str = "1y",
    intraday_period: str = "60d",
    recent_candles: int = 12,
) -> MarketSnapshot:
    """Fetch price data for ``ticker`` and return a :class:`MarketSnapshot`.

    Parameters
    ----------
    ticker:
        Symbol, e.g. ``"AAPL"``.
    daily_period:
        Look-back for daily bars used by SMA/RSI (needs >=200 sessions for SMA200).
    intraday_period:
        Look-back for 1h bars that are resampled to 4h price action.
    recent_candles:
        How many trailing 4h candles to include in the snapshot.
    """
    symbol = ticker.strip().upper()
    if not symbol:
        raise DataFetchError("Empty ticker symbol.")

    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover - environment guard
        raise DataFetchError(
            "yfinance is not installed. Run `pip install -r requirements.txt`."
        ) from exc

    tkr = yf.Ticker(symbol)

    # --- Daily bars: SMA + RSI ---
    daily = tkr.history(period=daily_period, interval="1d")
    if daily is None or daily.empty:
        raise DataFetchError(f"No daily price data returned for '{symbol}'.")
    daily_close = daily["Close"].dropna()

    sma_50 = _latest_sma(daily_close, 50)
    sma_200 = _latest_sma(daily_close, 200)
    rsi_14 = _compute_rsi(daily_close, 14)

    # --- Intraday bars resampled to 4h: recent price action ---
    candles: List[Candle] = []
    current_price: Optional[float] = None
    try:
        intraday = tkr.history(period=intraday_period, interval="1h")
        if intraday is not None and not intraday.empty:
            bars_4h = _resample_4h(intraday).tail(recent_candles)
            for ts, row in bars_4h.iterrows():
                candles.append(
                    Candle(
                        timestamp=pd.Timestamp(ts).isoformat(),
                        open=round(float(row["Open"]), 4),
                        high=round(float(row["High"]), 4),
                        low=round(float(row["Low"]), 4),
                        close=round(float(row["Close"]), 4),
                        volume=float(row["Volume"]),
                    )
                )
            if not bars_4h.empty:
                current_price = round(float(bars_4h["Close"].iloc[-1]), 4)
    except Exception as exc:  # noqa: BLE001 - intraday is best-effort
        logger.warning("Intraday fetch for %s failed (%s); using daily close.", symbol, exc)

    if current_price is None:
        current_price = round(float(daily_close.iloc[-1]), 4)

    # --- Trend regime ---
    trend_regime = "unknown"
    price_vs_sma50_pct: Optional[float] = None
    if sma_50 is not None and sma_200 is not None:
        trend_regime = "bullish" if sma_50 >= sma_200 else "bearish"
    if sma_50:
        price_vs_sma50_pct = round((current_price - sma_50) / sma_50 * 100, 2)

    snapshot = MarketSnapshot(
        ticker=symbol,
        as_of=datetime.now(timezone.utc).isoformat(),
        current_price=current_price,
        rsi_14=rsi_14,
        sma_50=sma_50,
        sma_200=sma_200,
        trend_regime=trend_regime,
        price_vs_sma50_pct=price_vs_sma50_pct,
        recent_4h_candles=candles,
    )
    logger.info(
        "Snapshot %s: price=%.2f rsi=%s sma50=%s sma200=%s regime=%s",
        symbol, current_price, rsi_14, sma_50, sma_200, trend_regime,
    )
    return snapshot


__all__ = ["Candle", "MarketSnapshot", "DataFetchError", "fetch_market_snapshot"]
