# AOTS — Automated Options Trading System (paper-first starter)

A runnable starter for a NIFTY-options trading system with a FastAPI web dashboard, configurable risk controls, a pluggable strategy interface, SQLite persistence, and a paper broker.

> **Safety boundary:** this project does not send orders to Groww or any other broker. `AOTS_TRADING_MODE=paper` is the only supported mode. `AOTS_LIVE_TRADING_ENABLED` is deliberately rejected if set to `true`. Do not use this starter as a production trading system without independent engineering, broker approval, and compliance review.

## Features

- Web dashboard at `http://127.0.0.1:8000/`
- Status, risk configuration, simulated trades and positions APIs
- Paper-only order/position simulation
- Daily loss limit (2% of configured capital), per-trade risk (0.5%), 3-loss circuit breaker
- Max open positions, max lots per order, stale-market-data guard
- Kill switch blocks new entries
- SQLite event/trade persistence
- Asia/Kolkata calendar-date risk rollover (not an exchange holiday calendar)
- Pluggable strategy protocol; no trading strategy is enabled by default
- Groww API boundary documented separately; no live order methods implemented
- Automated tests

## Requirements

Python 3.11+ (Python 3.12 recommended).

## Quick start

### macOS / Linux

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
uvicorn aots.api:app --reload
```

### Windows PowerShell

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
uvicorn aots.api:app --reload
```

Open http://127.0.0.1:8000. Run tests with `pytest`.

The dashboard starts in paper mode with an empty ledger. Use the API docs at `/docs` to inspect endpoints. The sample trade endpoint creates a **simulated** paper trade for testing; it does not submit any broker order.

## API quick reference

- `GET /api/status` — app mode, risk state, counts
- `GET /api/risk` — active risk limits
- `POST /api/risk/kill-switch` — turn the local entry kill switch on/off
- `GET /api/trades` — paper trade ledger
- `GET /api/positions` — open simulated positions
- `POST /api/paper/trade` — create a simulated trade
- `POST /api/paper/mark` — update simulated price/P&L
- `POST /api/paper/close/{trade_id}` — close a simulated trade
- `GET /health` — health check

### Example simulated trade

```bash
curl -X POST http://127.0.0.1:8000/api/paper/trade \
  -H 'Content-Type: application/json' \
  -d '{
    "symbol": "NIFTY26DEC25000CE",
    "idempotency_key": "replace-with-a-unique-request-key",
    "side": "BUY",
    "entry_price": 100,
    "stop_price": 95,
    "quantity": 25,
    "lots": 1,
    "market_data_age_seconds": 0
  }'
```

This example uses an illustrative quantity only. Use current exchange contract specifications and lot sizes when building a real instrument master. It does not select a real contract or generate a signal.
Paper-entry requests require a unique `idempotency_key` and an explicit finite `market_data_age_seconds`. Empty keys and keys with surrounding whitespace are rejected (keys are not trimmed). Reuse the same key when retrying an order; different order terms with a reused key are rejected. The reported age is caller-supplied because this application has no connected market-data feed.

Paper mark requests require a positive `observation_sequence` that strictly increases per trade. Missing or malformed sequences return HTTP 422; a duplicate or lower sequence is rejected with HTTP 409 without recording an applied-mark event. The check, accepted mark, simulated-stop behavior, P&L, and audit events are applied atomically. The dashboard supplies the next local sequence for manual simulated marks. Since the application has no connected feed, callers must supply source-generated monotonic sequences to represent source observation order; server receipt order does not establish freshness. Existing SQLite trades are retained and migrated with `mark_sequence=0`.

Paper entries may optionally include `take_profit_profit_inr`, a positive rupee-denominated total P&L target. P&L uses `(mark - entry) * quantity`, where `quantity` is the total option units across all lots; `lots` is not multiplied in again. For 10 lots of 65 units, submit `quantity=650, lots=10`. At an entry premium of ₹100, a ₹1,500 target is reached at or above a mark of ₹102.31 (₹1,501.50 profit). The trade closes at the accepted observed mark and records `SIMULATED_TAKE_PROFIT`; it does not assume a fill at a different price. This starter does not maintain an instrument lot-size master, so callers must supply the correct total quantity. The existing stop rule remains `mark_price <= stop_price` and is checked first. Trades without a target retain their existing stop/manual-close behavior. Existing databases are migrated additively with a nullable target and closure-reason column. A legacy `take_profit_price` premium column is preserved as-is but is not copied to the rupee target; the new target remains disabled for those records unless explicitly set.

### Dashboard manual smoke check

Automated tests verify that the dashboard and its sequence-aware script are served, but do not automate browser interaction. To exercise the UI against an isolated temporary ledger from the project root in Windows PowerShell:

```powershell
$env:AOTS_DATABASE_PATH = Join-Path $env:TEMP ("aots-sprint4-" + [guid]::NewGuid().ToString() + ".db")
.\.venv\Scripts\python.exe -m uvicorn aots.api:app --reload
```

Open http://127.0.0.1:8000 and create a trade with entry `100`, stop `95`, total quantity `65`, and take-profit profit target `650`. Mark it at `105` and confirm it remains open with unrealised P&L of ₹325; mark it at `110` and confirm it closes at the observed mark with ₹650 realised P&L and a “Take-profit reached” status. Create a second trade with take-profit blank, mark it at `90`, and confirm the existing simulated stop closes it with -₹650 realised P&L. Reload the page and confirm both closed trades remain in the ledger. These are manual paper-only checks; no broker order is sent.

## Risk model notes

- The daily-loss limit is calculated against the configured starting capital and current-date realised + open-position unrealised P&L tracked by this local paper ledger. Trade closing dates use Asia/Kolkata.
- Consecutive losses and the daily pause reset on Asia/Kolkata calendar-date changes. This is not an exchange holiday or trading-session calendar; the kill switch and trade/event history persist across rollover.
- Risk per trade is calculated as `(entry - stop) * quantity` for BUY positions. This starter is long-option oriented and rejects short trades; expand the model carefully before supporting multi-leg or short-option strategies.
- An entry is rejected if stop risk exceeds the configured per-trade allowance, lot count exceeds the limit, the kill switch is on, market data is stale, daily loss is breached, the loss streak is paused, or position count is at its limit.
- Entry risk checks, duplicate-key handling, trade insertion and the opening audit event are serialized in one SQLite transaction. Mark/close state transitions and their risk/audit updates also commit atomically.
- Risk limits do not guarantee a maximum loss in real markets. Gaps, slippage, liquidity, rejected exits and outages can exceed stop estimates.
- A software kill switch cannot guarantee square-off when the broker/network is unavailable. Live systems need broker-side protection where available, out-of-band alerting, and a tested incident procedure.

## Architecture

```text
Dashboard / API
      |
Application service
      |---------------------|
Risk engine            Strategy interface
      |                     |
Paper broker <----- Market-data abstraction
      |
SQLite ledger / audit events

Future Groww adapter: read-only market-data/account functions first.
Live execution is not implemented in this starter.
```

## Groww integration boundary

See `docs/GROWW_INTEGRATION.md`. Groww's official API documentation describes authentication, option-chain/Greeks endpoints, order APIs, and WebSocket capabilities. This project intentionally only defines an adapter contract and does not invoke the live order endpoints. API permissions, token handling, product availability, rate limits, supported order types, and account-specific eligibility must be verified against current official documentation and broker support before any integration.

## Indian retail algo compliance

See `docs/COMPLIANCE_CHECKLIST_INDIA.md`. This is an engineering checklist, not legal advice. SEBI/exchange requirements and broker implementation procedures change; verify the latest official circulars, exchange notices, and Groww's current API terms before enabling any live execution.

## Production gaps (not yet implemented)

This starter is not production-ready. Before real-money use, implement and independently test: official instrument-master refresh; live market-data subscriptions with source timestamps; exchange calendar; broker-side order/position reconciliation; broker-compatible order-state machine; order and fill persistence; margin checks; exchange/broker-approved algo registration and tagging; secure secret storage; operational alerting; independent watchdog; backups; deployment hardening; and incident/recovery drills. Keep live execution disabled until all requirements are signed off.
