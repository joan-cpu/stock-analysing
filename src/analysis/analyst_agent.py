"""Layer 2 - The Analyst Agent.

A thin, well-guarded interface over the Anthropic (Claude) API. The agent reads
the Layer 1 :class:`MarketSnapshot` and returns a strictly-typed
:class:`AnalystDecision`.

Design notes
------------
* The system prompt forces Claude to behave as a disciplined *technical swing
  trader* and to answer with a single JSON object - no markdown, no prose.
* The response is defensively parsed (markdown fences stripped if present) and
  validated with pydantic, so a malformed answer fails loudly instead of leaking
  garbage into the risk layer.
* This layer proposes a trade *idea only*. It has NO authority over position
  size, stops, or whether the order is actually sent - that is Layer 3's job.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError

from config.settings import Settings, get_settings
from src.ingestion.data_fetcher import MarketSnapshot

logger = logging.getLogger(__name__)

Action = Literal["BUY", "SELL", "HOLD"]

SYSTEM_PROMPT = """\
You are a disciplined technical swing trader. You trade multi-day to multi-week \
moves based strictly on price action and technical indicators. You are not an \
investment adviser and you do not give financial advice to humans - you emit a \
single machine-readable trade signal for a downstream automated risk system.

Rules you MUST follow:
- Reason ONLY from the technical data provided (RSI, SMA50, SMA200, trend regime,
  recent 4h candles, price relative to SMA50).
- Prefer BUY when the regime is bullish (SMA50 >= SMA200) and momentum supports a
  continuation or a pullback entry; prefer SELL when the regime is bearish and
  momentum confirms weakness; otherwise HOLD.
- Do NOT invent data you were not given. Do NOT size the position or set stops -
  a separate hardcoded risk layer does that.
- target_price is your technical price objective for the move (a positive number).
  For HOLD, set target_price to the current price.

Output format (CRITICAL):
- Respond with EXACTLY ONE JSON object and nothing else.
- No markdown, no code fences, no commentary before or after.
- Keys, in this exact shape:
  {
    "action": "BUY" | "SELL" | "HOLD",
    "ticker": "<symbol>",
    "target_price": <number>,
    "reasoning": "<one or two concise sentences citing the indicators>"
  }
"""

USER_TEMPLATE = """\
Analyse the following market snapshot and return your trade signal as a single \
JSON object per your instructions.

MARKET_SNAPSHOT:
{snapshot_json}
"""


class AnalystDecision(BaseModel):
    """Validated trade idea emitted by the analyst agent."""

    action: Action
    ticker: str
    target_price: float = Field(gt=0)
    reasoning: str

    @classmethod
    def from_model_text(cls, text: str, expected_ticker: str) -> "AnalystDecision":
        """Parse and validate the raw model text into a decision."""
        payload = _extract_json_object(text)
        decision = cls.model_validate(payload)
        # Normalise / defend the ticker so a hallucinated symbol can't slip through.
        if decision.ticker.strip().upper() != expected_ticker.strip().upper():
            logger.warning(
                "Analyst returned ticker %r; overriding with requested %r.",
                decision.ticker, expected_ticker,
            )
            decision = decision.model_copy(update={"ticker": expected_ticker.upper()})
        else:
            decision = decision.model_copy(update={"ticker": expected_ticker.upper()})
        return decision


class AnalystError(RuntimeError):
    """Raised when the analyst cannot produce a valid decision."""


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def _extract_json_object(text: str) -> dict:
    """Best-effort extraction of a single JSON object from model output."""
    cleaned = _FENCE_RE.sub("", text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Fall back to the first {...} span if the model added stray characters.
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError as exc:
                raise AnalystError(f"Could not parse JSON from analyst output: {exc}") from exc
        raise AnalystError("Analyst output contained no JSON object.")


class AnalystAgent:
    """Wraps the Anthropic client to turn a snapshot into a trade idea."""

    def __init__(self, settings: Optional[Settings] = None, client=None) -> None:
        self.settings = settings or get_settings()
        self._client = client  # allow dependency injection for tests

    def _get_client(self):
        if self._client is not None:
            return self._client
        if not self.settings.has_anthropic:
            raise AnalystError(
                "ANTHROPIC_API_KEY is not set. Add it to your .env to run the analyst."
            )
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - environment guard
            raise AnalystError(
                "anthropic SDK not installed. Run `pip install -r requirements.txt`."
            ) from exc
        self._client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        return self._client

    def analyze(self, snapshot: MarketSnapshot) -> AnalystDecision:
        """Run the analyst on a market snapshot and return a validated decision."""
        client = self._get_client()
        user_content = USER_TEMPLATE.format(snapshot_json=snapshot.to_analyst_json())

        logger.info("Requesting analyst decision for %s (model=%s).",
                    snapshot.ticker, self.settings.anthropic_model)
        try:
            response = client.messages.create(
                model=self.settings.anthropic_model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
        except Exception as exc:  # noqa: BLE001 - surface any SDK/API failure
            raise AnalystError(f"Anthropic API call failed: {exc}") from exc

        text = _first_text_block(response)
        if not text:
            raise AnalystError("Analyst returned no text content.")

        try:
            decision = AnalystDecision.from_model_text(text, snapshot.ticker)
        except ValidationError as exc:
            raise AnalystError(f"Analyst output failed schema validation: {exc}") from exc

        logger.info("Analyst decision: %s %s @ target %.2f",
                    decision.action, decision.ticker, decision.target_price)
        return decision


def _first_text_block(response) -> str:
    """Extract the first text block from an Anthropic response (skips thinking)."""
    content = getattr(response, "content", None)
    if not content:
        return ""
    for block in content:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


__all__ = ["Action", "AnalystDecision", "AnalystAgent", "AnalystError", "SYSTEM_PROMPT"]
