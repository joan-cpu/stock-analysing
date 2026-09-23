# AI Swing Trading System (India / Dhan)

A modular, **safety-first** scaffold for an autonomous multi-agent swing-trading
pipeline for **Indian equities (NSE/BSE)**. It separates concerns into four
isolated layers, with a hardcoded risk interceptor sitting between the LLM's
suggestion and any order that reaches the broker.

> ⚠️ **Not financial advice. Educational scaffold only.** It defaults to
> **dry-run**, and when it does send orders it targets the **Dhan sandbox**
> (mock money) unless you deliberately switch to live. Automated trading can
> lose money quickly.

---

## Architecture

```
Ingest (L1) ─▶ Analyze (L2) ─▶ Risk Intercept / Validate (L3) ─▶ Execute (L4)
 yfinance       Claude LLM        hardcoded Python (no AI)          Dhan (sandbox)
 real prices                      2% cap · 3% SL · 6% TP            mock by default
```

| Layer | File | Role | AI? |
|-------|------|------|-----|
| **1. Ingestion** | `src/ingestion/data_fetcher.py` | Real prices (Yahoo Finance, `.NS`/`.BO`) + RSI, SMA50, SMA200 | No |
| **2. Analyst** | `src/analysis/analyst_agent.py` | Claude reads the metrics, returns a strict JSON trade signal | **Yes** |
| **3. Risk interceptor** | `src/risk/guardrails.py` | Enforce 2% max position, append 3% stop / 6% target, or kill the trade | **No — deterministic** |
| **4. Executor** | `src/execution/order_executor.py` | Build the Dhan payload; dry-run by default, sandbox before live | No |

Supporting modules: `src/execution/dhan_client.py` (Dhan REST calls) and
`src/execution/instruments.py` (resolves a symbol to Dhan's `securityId`).

**The safety guarantee:** Layer 3 is 100% hardcoded Python. It makes no model
calls. Every order is capped at 2% of equity and carries a mandatory
stop-loss/take-profit, or it is rejected with a hard error.

### Why Dhan sandbox?

Dhan is one of the few Indian brokers with a **mock API** ([developer.dhanhq.co](https://developer.dhanhq.co)):
you get ₹10,00,000 of virtual capital (resets daily) and orders are simulated,
never routed to the exchange. That lets the **whole pipeline run end-to-end —
including the real order API call — with zero money at risk.** Note the sandbox
fills every order at ₹100 and has no live quotes, so it validates *plumbing*, not
strategy P&L (that's why prices/analysis still come from Yahoo Finance).

---

## Setup

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate   |   macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # Windows: copy .env.example .env   — then edit
```

### `.env` variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `ANTHROPIC_API_KEY` | for Layer 2 | — | Claude API key |
| `ANTHROPIC_MODEL` | no | `claude-sonnet-5` | Analyst model |
| `DHAN_CLIENT_ID` / `DHAN_ACCESS_TOKEN` | to submit orders | — | Dhan credentials (sandbox token works) |
| `DHAN_ENV` | no | `sandbox` | `sandbox` (mock) or `live` (real money) |
| `DHAN_ENABLE_LIVE_TRADING` | no | `false` | Second lock; must be `true` for a live order |
| `DEFAULT_EXCHANGE` | no | `NSE` | `NSE` or `BSE` |
| `PRODUCT_TYPE` | no | `CNC` | `CNC` (delivery/swing), `INTRADAY`, `BO`, ... |
| `DRY_RUN` | no | `true` | Print the payload instead of sending |
| `SIMULATED_TOTAL_EQUITY` / `SIMULATED_CASH_BALANCE` | no | `1000000` | Equity used when no Dhan creds are set |

---

## Running

**Dry-run (default — no order sent, prints payload + risk plan):**

```bash
python main.py --symbol RELIANCE
```

Works with **no Dhan account at all** — analysis uses real Yahoo Finance prices
and the risk layer falls back to the `SIMULATED_*` equity. Only Layer 2 needs an
`ANTHROPIC_API_KEY`.

**Propose a size and watch the risk layer cap it to 2%:**

```bash
python main.py --symbol TCS --qty 100000
```

**Submit to the Dhan sandbox (mock money, real API round-trip):**

```bash
# needs DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN (sandbox token) and DHAN_ENV=sandbox
python main.py --symbol INFY --submit
```

**Going live (real money) — deliberately gated:** set `DHAN_ENV=live` **and**
`DHAN_ENABLE_LIVE_TRADING=true`, then `--submit`. If either is missing the
executor refuses. Test everything in sandbox first.

---

## Tests

```bash
pytest -q
```

No network, no LLM. They assert positions never exceed the 2% cap, oversized
proposals are downsized, stop-loss/take-profit are appended correctly, HOLD
produces no order, and unsafe inputs raise a hard `RiskViolation`.

---

## Notes specific to the Indian market

- **CNC (delivery) can't be shorted overnight**, so `allow_short_selling` is
  `false` by default — a `SELL` signal is rejected by the risk layer. To trade
  shorts, use an intraday product and enable shorting in `trading_rules.json`.
- **SL/TP for CNC** are provided as a *risk plan* (reference/stop/target prices)
  printed with every order. Placing them automatically as GTT/exit orders is a
  documented extension point. For the `BO` product they attach to the order
  directly (`boStopLossValue` / `boProfitValue`), but `BO` is intraday only.
- **Dhan needs a `securityId`**, resolved automatically from Dhan's public scrip
  master (cached under `data/`, refreshed daily).

## Safe-deployment playbook

1. **Stay in dry-run** until you've read the payloads and agree with them.
2. **Sandbox next.** Submit to Dhan sandbox and confirm the round-trip before
   ever touching live.
3. **Treat `trading_rules.json` as immutable.** Change limits only via reviewed
   commits; never let the model rewrite it.
4. **The risk layer is the boundary.** Keep Layer 3 free of any AI calls.
5. **Add monitoring & kill-switches** before live: position reconciliation,
   daily loss limits, a manual halt. These are *not* included here.
6. **Understand the risk.** This is a learning scaffold, not a production trading
   system, and comes with no warranty.
