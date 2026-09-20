# AI Swing Trading System

A modular, **safety-first** scaffold for an autonomous multi-agent swing-trading
pipeline. It separates concerns into four isolated layers, with a hardcoded risk
interceptor sitting between the LLM's suggestion and any order that reaches a
broker.

> ⚠️ **Not financial advice. Educational scaffold only.** It ships defaulting to
> **dry-run** and, even when "live", targets **Alpaca paper trading only**. Do not
> point it at a live brokerage endpoint or risk real capital.

---

## Architecture

```
Ingest (L1) ─▶ Analyze (L2) ─▶ Risk Intercept / Validate (L3) ─▶ Execute (L4)
 yfinance       Claude LLM        hardcoded Python (no AI)          Alpaca paper
```

| Layer | File | Role | AI? |
|-------|------|------|-----|
| **1. Ingestion** | `src/ingestion/data_fetcher.py` | Pull price action (4h candles) + compute RSI(14), SMA50, SMA200 | No |
| **2. Analyst** | `src/analysis/analyst_agent.py` | Claude reads the metrics, returns a strict JSON trade signal | **Yes** |
| **3. Risk interceptor** | `src/risk/guardrails.py` | Enforce 2% max position, append 3% stop / 6% target, or kill the trade | **No — deterministic** |
| **4. Executor** | `src/execution/order_executor.py` | Build the broker payload; dry-run by default | No |

**The safety guarantee:** Layer 3 is 100% hardcoded Python. It makes no model
calls and takes no AI-driven decisions. Every order is capped at 2% of equity and
carries a mandatory stop-loss/take-profit, or it is rejected with a hard error.

### Directory layout

```
stock-analysing/
├── config/
│   ├── settings.py           # env config + immutable-rules loader
│   └── trading_rules.json    # hardcoded, immutable risk parameters
├── src/
│   ├── ingestion/data_fetcher.py    # Layer 1
│   ├── analysis/analyst_agent.py    # Layer 2
│   ├── risk/guardrails.py           # Layer 3  (critical safety)
│   └── execution/order_executor.py  # Layer 4
├── tests/test_guardrails.py  # proves the safety controls hold
├── main.py                   # orchestrator
├── requirements.txt
└── .env.example
```

---

## Setup

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env      # Windows: copy .env.example .env
# then edit .env
```

### `.env` variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `ANTHROPIC_API_KEY` | for Layer 2 | — | Claude API key ([console](https://console.anthropic.com/)) |
| `ANTHROPIC_MODEL` | no | `claude-sonnet-5` | Analyst model (see note below) |
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | only for `--live` | — | **Paper** trading keys |
| `ALPACA_BASE_URL` | no | `https://paper-api.alpaca.markets` | Paper endpoint (live endpoints are refused) |
| `DRY_RUN` | no | `true` | Print payloads instead of sending |
| `SIMULATED_TOTAL_EQUITY` | no | `100000` | Equity the risk layer sizes against when no broker keys are set |
| `SIMULATED_CASH_BALANCE` | no | `100000` | Cash used for the funding check in simulation |

> **Model note:** the original spec requested *Claude 3.5 Sonnet*, which is now
> deprecated. This scaffold defaults to **`claude-sonnet-5`** (its current, cheaper,
> more capable successor). Override with `ANTHROPIC_MODEL` if you need a specific
> model.

---

## Running a safe simulation

**Dry-run (default — no orders sent, no real money, prints the exact payload):**

```bash
python main.py --ticker AAPL
```

You can run the ingestion + risk layers with **no Alpaca account at all** — the
risk layer falls back to the `SIMULATED_*` equity values. Only Layer 2 needs an
`ANTHROPIC_API_KEY`.

**Propose a size and watch the risk layer downsize it to the 2% cap:**

```bash
python main.py --ticker MSFT --qty 10000
```

**Paper trading (still no real money — hits Alpaca's paper API):**

```bash
# requires ALPACA_API_KEY / ALPACA_SECRET_KEY for a PAPER account
python main.py --ticker TSLA --live
```

The executor **refuses to submit** if `ALPACA_BASE_URL` is not a paper endpoint or
if credentials are missing — there is no code path to a live brokerage here.

---

## Tests

The guardrails are covered by unit tests that require no network and no LLM:

```bash
pytest -q
```

They assert that positions never exceed the 2% cap, oversized proposals are
downsized, stop-loss/take-profit are appended correctly for both long and short,
HOLD produces no order, and unsafe inputs raise a hard `RiskViolation`.

---

## Safe-deployment playbook

1. **Stay in dry-run** until you have read every logged payload and agree with it.
2. **Paper only.** Keep `ALPACA_BASE_URL` on the paper endpoint. The executor
   blocks non-paper URLs by design — do not remove that check.
3. **Treat `trading_rules.json` as immutable.** Never let the model or any runtime
   process rewrite it. Change limits only via reviewed commits.
4. **The risk layer is the boundary.** Keep Layer 3 free of any LLM/AI calls.
   All position sizing and stops must remain deterministic Python.
5. **Add monitoring & kill-switches** before considering anything beyond paper:
   position reconciliation, daily loss limits, and a manual halt. These are *not*
   included here.
6. **Understand the risk.** Automated trading can lose money quickly. This is a
   learning scaffold, not a production trading system, and comes with no warranty.
