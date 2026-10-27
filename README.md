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
- Pluggable strategy protocol plus an opt-in supplied-input spot-threshold demonstration
- Groww API boundary documented separately; no live order methods implemented
- Opt-in Groww-fed NIFTY ORB paper session in a separate dashboard tab
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

The dashboard starts in paper mode with an empty ledger. The Paper Trading and Groww sections are separate; historical-data requests occur only after an explicit user action and remain separate from simulated capital and the paper ledger. Use the API docs at `/docs` to inspect endpoints. The sample trade endpoint creates a **simulated** paper trade for testing; it does not submit any broker order.

### Live Paper ORB tab

The separate **Live Paper ORB** tab is an explicit, local-only paper session. Connect Groww first, choose an option expiry and strategy parameters, then start the session. AOTS subscribes to the NIFTY index feed, builds only tick-observed candles, applies the existing close-based opening-range breakout rule, resolves the selected NIFTY option from Groww's option chain and instrument master, and fills a local paper position at the first fresh option LTP received after the signal. It uses the existing `PaperBroker` and `RiskEngine`; stops, optional total-position profit targets, session square-off, and the risk gate do not call any broker order API.

The feed uses Groww's socket token and NATS WebSocket only for read-only market data. Socket credentials are kept in memory; the SDK helper that writes fixed `a.creds`/`b.creds` files is not used. The instrument master is downloaded only after starting the live-paper feature, with a bounded response size and timeout. Automated tests use fake feeds and do not call Groww. If the application restarts with an open live paper position, it requires an explicit Groww reconnect and recovery action; recovery monitors that position but does not resume new entries.

This is not a production execution simulator. LTP is not bid/ask, and the existing paper broker does not model spread, slippage, brokerage, or exchange charges. Tick-built candles cannot reconstruct a candle interval with no observations; missing bars are not synthesized. Session rollover is an Asia/Kolkata calendar date, not an exchange holiday/trading-session calendar. See [Groww integration](docs/GROWW_INTEGRATION.md) for operating limits.

## API quick reference

- `GET /api/status` — app mode, risk state, counts
- `GET /api/risk` — active risk limits
- `POST /api/risk/kill-switch` — turn the local entry kill switch on/off
- `GET /api/trades` — paper trade ledger
- `GET /api/positions` — open simulated positions
- `POST /api/paper/trade` — create a simulated trade
- `POST /api/paper/mark` — update simulated price/P&L
- `POST /api/paper/close/{trade_id}` — close a simulated trade
- `POST /api/strategy/evaluate` — evaluate supplied inputs without persisting or trading
- `POST /api/strategy/process` — persist a decision and risk-gate an explicit simulated entry
- `GET /api/strategy/decisions` — inspect recent persisted strategy outcomes
- `GET /api/live-paper/status` — inspect the explicit live-paper ORB session
- `POST /api/live-paper/start` — start a Groww-fed local paper session
- `POST /api/live-paper/stop` — disable new entries; an open paper position stays monitored
- `POST /api/live-paper/resume` — recover monitoring of a persisted open paper position
- `GET /api/live-paper/events` — inspect live-paper session events
- `GET /health` — health check

### Deterministic strategy demonstration

The optional `spot-threshold-demo` strategy evaluates one simple rule: **signal when the explicitly supplied underlying spot is greater than or equal to `AOTS_STRATEGY_SPOT_THRESHOLD`**. Set a positive threshold in `.env` or the process environment to enable it; if unset, evaluation fails closed as `REJECTED`. The demonstration accepts the configured underlying (default `NIFTY`) only.

Every evaluation request must supply an observation ID, underlying, finite positive spot, timezone-aware observation timestamp, candidate symbol, entry premium, stop premium, total quantity, lots, and optional positive total-position profit target in INR. The candidate prices and contract symbol are caller-supplied; the rule does not select an option, infer call/put type, calculate prices, or verify the source. `observed_at` is compared to the local evaluation clock and rejected if it is in the future or older than `AOTS_MARKET_DATA_MAX_AGE_SECONDS`. This prevents accepting missing timestamps, but cannot establish that user-supplied timestamps or prices are authentic. No live feed, candles, indicators, or instrument master is present.

Use `POST /api/strategy/evaluate` to get a structured `SIGNAL`, `NO_SIGNAL`, or `REJECTED` result. This endpoint is pure: it does not write decisions, trades, or audit events. Use `POST /api/strategy/process` only when you explicitly want to persist the result and attempt a paper entry. A qualifying candidate is sent through the existing authoritative risk validation; a risk failure is persisted as `RISK_REJECTED`, with no trade. A successful result is `TRADE_CREATED`; neither a signal nor a created simulated trade implies profitability. The existing BUY-only paper model, stop-first closure, mark sequencing, long-option P&L and total-INR take-profit behavior are unchanged.

Each process request must use a stable `observation_id`. SQLite serializes the idempotency check, risk decision, optional trade insertion, decision record, and audit events in one `BEGIN IMMEDIATE` transaction. Retrying an identical strategy/version/observation/input/configuration returns the original persisted decision and trade ID. Reusing the same observation ID with different inputs or configuration returns HTTP 409 without overwriting it. A storage error rolls back the attempted decision, trade and transaction-scoped audit events. `GET /api/strategy/decisions` returns the most recent 50 processed outcomes; evaluation-only outcomes are intentionally not in that history.

Set the strategy threshold locally, for example in PowerShell:

```powershell
$env:AOTS_STRATEGY_SPOT_THRESHOLD = "25000"
.\.venv\Scripts\python.exe -m uvicorn aots.api:app --host 127.0.0.1 --port 8000
```

In the dashboard, enter a unique observation ID and all supplied inputs, including an ISO-8601 timestamp with timezone (for example `2026-10-18T00:10:00+05:30`). **Evaluate only** does not persist or open a trade. **Process paper entry** records the outcome and, if the configured spot rule signals and all risk checks pass, opens a local paper position. Use `/api/strategy/decisions` or the dashboard’s recent-decision list to inspect processed signals, no-signals, stale/unconfigured rejections, and risk rejections.

This is a deterministic demonstration, not an autonomous trading strategy. It is paper-only, has no broker order placement or connected live market-data feed, and makes no claim of profitability. Do not treat manual or caller-supplied data as exchange-confirmed.

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

### Complete local paper-trading workflow (Windows PowerShell)

This workflow uses a separate SQLite file in the Windows temporary directory. Start the server from one PowerShell window and keep it open:

```powershell
$env:AOTS_DATABASE_PATH = Join-Path $env:TEMP ("aots-sprint5-" + [guid]::NewGuid().ToString() + ".db")
Write-Host "Paper database: $env:AOTS_DATABASE_PATH"
.\.venv\Scripts\python.exe -m uvicorn aots.api:app --reload
```

Open http://127.0.0.1:8000 for the dashboard and http://127.0.0.1:8000/docs for the API. The dashboard displays open unrealised and closed realised P&L separately, closure details and timestamps in Asia/Kolkata time. The option type is shown as unavailable because the trade model does not persist call/put contract classification. Marks are entered manually; there is no market-data feed or source timestamp, so freshness cannot be verified.

In a second PowerShell window, set the database path to the exact path printed by the server window (copy the whole path, including the `.db` suffix), then create a simulated paper trade:

```powershell
$env:AOTS_DATABASE_PATH = "PASTE THE PAPER DATABASE PATH PRINTED BY THE SERVER"
$base = "http://127.0.0.1:8000"
$tradeRequest = @{
  symbol = "NIFTY-DEMO-CE"
  idempotency_key = "sprint5-local-demo"
  side = "BUY"
  entry_price = 100
  stop_price = 95
  take_profit_profit_inr = 100
  quantity = 10
  lots = 1
  market_data_age_seconds = 0
}
$trade = Invoke-RestMethod "$base/api/paper/trade" -Method Post `
  -ContentType "application/json" -Body ($tradeRequest | ConvertTo-Json)
$tradeId = $trade.trade_id
```

The position is 10 total option units, not 10 multiplied by the lot count a second time. Submit a mark below target; the ₹50 unrealised profit should leave the trade open:

```powershell
$mark = @{ trade_id = $tradeId; mark_price = 105; observation_sequence = 1 }
Invoke-RestMethod "$base/api/paper/mark" -Method Post `
  -ContentType "application/json" -Body ($mark | ConvertTo-Json)
```

Confirm a duplicate/stale observation is rejected without changing the ledger, then submit the next sequence at the target. It should close at the accepted ₹110 mark with ₹100 realised profit and reason `SIMULATED_TAKE_PROFIT`:

```powershell
$before = (Invoke-RestMethod "$base/api/trades" |
  Where-Object trade_id -eq $tradeId | ConvertTo-Json -Compress)
$stale = @{ trade_id = $tradeId; mark_price = 120; observation_sequence = 1 }
try {
  Invoke-RestMethod "$base/api/paper/mark" -Method Post `
    -ContentType "application/json" -Body ($stale | ConvertTo-Json)
} catch {
  Write-Host "Expected stale-mark rejection: $($_.Exception.Message)"
}
$after = (Invoke-RestMethod "$base/api/trades" |
  Where-Object trade_id -eq $tradeId | ConvertTo-Json -Compress)
Write-Host "Ledger unchanged after rejected mark: $($before -eq $after)"

$targetMark = @{ trade_id = $tradeId; mark_price = 110; observation_sequence = 2 }
Invoke-RestMethod "$base/api/paper/mark" -Method Post `
  -ContentType "application/json" -Body ($targetMark | ConvertTo-Json)
Invoke-RestMethod "$base/api/trades" | ConvertTo-Json -Depth 5
Invoke-RestMethod "$base/api/risk" | ConvertTo-Json
Invoke-RestMethod "$base/api/events" | ConvertTo-Json -Depth 5
```

The dashboard's **Mark** action submits the next local sequence for a manually entered mark; **Close** remains available for manual paper closure. The API also permits an explicit sequence for testing. In the server window, stop and restart Uvicorn with Ctrl+C and the same `$env:AOTS_DATABASE_PATH`; refresh the dashboard or query `/api/trades` and `/api/events` to verify that the closed trade and audit history persisted. No broker order or live price monitoring is involved. Run `.\.venv\Scripts\pytest.exe -q` from the project root for the automated suite.

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

Groww adapter: read-only market data and account-insight functions.
Live order execution is not implemented in this starter.
```

## Market data provider (runtime credentials)

The dashboard's **Market Data Provider** panel lets an operator enter provider credentials at runtime. Paper trades stay simulated and are never sent to a broker; no order-placement method exists.

Endpoints:

- `GET /api/provider/status`
- `POST /api/provider/connect`
- `POST /api/provider/disconnect`
- `GET /api/provider/quote?symbol=NIFTY`
- `GET /api/provider/option-chain?underlying=NIFTY&expiry=YYYY-MM-DD`
- `GET /api/provider/account-insights`
- `GET /api/provider/historical/expiries?year=2024&month=1`
- `GET /api/provider/historical/contracts?expiry_date=2024-01-04`
- `POST /api/provider/historical/candles/check` — validate a bounded, read-only NIFTY option-candle response

Connect body (Groww; Kite is listed but disabled and rejected until its adapter exists):

- `{"provider":"groww","auth_method":"totp","api_key":"…","totp_secret":"…"}`
- `{"provider":"groww","auth_method":"approval","api_key":"…","api_secret":"…"}`
- `{"provider":"groww","auth_method":"access_token","access_token":"…"}`

Credential and token lifecycle:

- Credentials are held only in backend process memory (`aots/provider_session.py`), inside the active provider connection object. After successful authentication, only the access token remains in memory. Credentials and tokens are never written to SQLite, `.env`, configuration, audit events or logs, and are never returned by any endpoint. Validation errors on `/api/provider/*` omit submitted values.
- The browser clears the inputs as soon as the form is submitted and on disconnect; nothing is kept in browser storage, cookies or URLs.
- Each Connect first closes any existing session, so a failed attempt never leaves stale credentials. Disconnect and application shutdown close the session and drop all references. Repeated Connect/Disconnect is safe.
- Status states: `not_configured`, `credentials_in_memory`, `connecting`, `connected`, `failed`, `disconnected`. API-key authentication is verified by Groww's token exchange. A manually supplied token is format-checked locally and Groww validates it on the first provider request; a 401 then safely disconnects the session.
- Groww token exchange follows the official docs: `POST /v1/token/api/access` uses the supplied API key in the Authorization header. The approval flow sends `key_type=approval` with checksum `sha256(secret + epoch_seconds)` and timestamp. The TOTP flow sends `key_type=totp` with a generated RFC 6238 code from the supplied seed.
- Tokens generated through the TOTP or approval flow are treated as expiring at the next 06:00 IST boundary (per official docs). A manually supplied access token is kept in memory without guessing its issue time or expiry; Groww validates it on the first provider request. An HTTP 401 clears the session and requires reconnect.
- Read-only market data is normalized through the backend using official endpoints (`/v1/live-data/quote` and `/v1/option-chain/exchange/{exchange}/underlying/{underlying}` with `expiry_date=YYYY-MM-DD`). The application currently supports `NSE/NIFTY` for this runtime slice.
- Account insights use Groww's official Portfolio and Margin endpoints: `/v1/positions/user?segment=FNO`, `/v1/positions/user?segment=CASH`, `/v1/holdings/user`, and `/v1/margins/detail/user`. Groww documents `positions[].realised_pnl` but gives it no explicit daily time scope, so it is shown only as "reported realised P&L (scope unspecified)." Today's P&L is explicitly unavailable: AOTS does not infer it from that value, holdings, or balances, and this account-insights path does not retrieve per-position live marks.
- The Groww tab offers a manual historical-data capability check using Groww's documented `/v1/historical/expiries`, `/v1/historical/contracts`, and `/v1/historical/candles` endpoints. Groww documents FNO history from 2020 and describes option candle OHLC, volume and open interest in rupees. The diagnostic is limited to NIFTY option contracts, 5- or 15-minute intervals, and one calendar day per candle request. It does not create trades, store candles, alter paper trades/risk, or call any order API. Endpoint access depends on each account's Groww entitlement; a 403 is reported as access denied but does not identify the specific permission or subscription.
- Historical candle timestamps are documented without an offset. AOTS preserves their wall-clock values and does not claim their timezone convention or session completeness is verified. Invalid OHLC/OI values, duplicate timestamps and ordering/range concerns are surfaced as warnings. The check is a capability/data-quality diagnostic, not a historical options backtest.
- `clear_cash` is displayed as Groww-reported available cash. Other margin values (`collateral_available`, FNO option/future balances, and CNC/MIS balances) are shown separately and are not summed. Missing provider fields remain unavailable (`null`), distinct from numeric zero. Account insights are fetched only on the panel's manual refresh, not during normal dashboard refresh.
- Positions and holdings use only documented provider fields. A successful account snapshot carries a backend refresh timestamp; on a failed refresh, previously displayed data remains labelled stale with its prior timestamp. Auth failures expire the local session and require reconnection.
- Limitation: Python cannot securely erase immutable strings. Dropping references makes the values eligible for garbage collection, but copies may remain in process memory until then (and in the request body buffers of the web server).

## Groww integration boundary

See `docs/GROWW_INTEGRATION.md`. Groww's official API documentation describes authentication, market data, account/portfolio endpoints, historical data, order APIs, and WebSocket capabilities. AOTS uses only read-only endpoints and does not invoke live order endpoints. Access to specific endpoints depends on Groww account permissions and subscription; the historical-data diagnostic reports the user's observed response and does not assume an entitlement from documentation or plan labels.

## Historical NIFTY ORB research backtest

The `aots.backtest` package downloads Yahoo Finance's `^NSEI` 5-minute bars with `yfinance`, validates the response, saves a CSV cache plus a provenance/validation manifest, and runs the NIFTY 50 30-minute Opening Range Breakout simulation. The standalone report command and the dashboard's historical replay use the same chronological ORB executor. This remains research tooling: it is separate from the option-paper strategy, risk engine, paper broker and Groww account data, and cannot submit broker orders.

The baseline opening range uses the six bars timestamped 09:15–09:40 Asia/Kolkata. A breakout event is a close transition from inside/no breakout to strictly outside the frozen range, or a direct transition to the opposite side. Consecutive closes on the same side are not new events; price must close back inside before another breakout in that direction can occur. Each event is evaluated once, whether it passes optional indicator filters or entry constraints. Reports separate raw events, filter-passing and filter-rejected events, executable entry attempts, entry rejections, and completed trades, with rejection reasons. Entry uses the next bar's open. The stop is the opposite range boundary, target is 2R, new entries stop after 15:00, and a still-open position exits at the 15:15 candle open. Only one completed trade per session is allowed. A stop gap fills at the worse candle open; if one candle touches both stop and target, the conservative stop is applied first. Results are NIFTY index points only, with no fees, taxes, slippage, quantity, or multiplier applied.

Install the project dependencies as usual, then run a recent date window (the default is the recent 30 calendar days, with an end date exclusive):

```powershell
.\.venv\Scripts\python.exe -m aots.backtest
.\.venv\Scripts\python.exe -m aots.backtest --start 2026-09-20 --end 2026-10-20
```

Options include `--data-dir`, `--output-dir`, `--timeout`, `--retries`, `--offline`, `--max-cache-age-days`, `--allow-stale-cache`, `--session-calendar`, `--ema50`, and `--atr-max-ratio 2.0`. EMA and ATR options run as separate experiments against the baseline; EMA uses an SMA seed from 50 closes and ATR is an SMA of 14 completed true ranges. They are not automatically tuned. VWAP and breakout-volume filters are not supported for `^NSEI` because its reported index volume has not been validated as meaningful traded-instrument volume; requesting either filter fails closed. Yahoo intraday history is limited to the most recent 60 calendar days; `yfinance` documents this limit and exposes `timeout` on `download()`. Its documentation describes timezone handling but does not establish whether each 5-minute label marks the candle's start or end. This implementation assumes start labels (09:15 through 15:25 IST), consistent with observed complete sessions, but that convention is not authoritatively verified. See the [yfinance download reference](https://ranaroussi.github.io/yfinance/reference/api/yfinance.download.html). yfinance is not affiliated with or endorsed by Yahoo; observe [Yahoo's terms](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html).

Online mode always attempts a download and never falls back to a cache. `--offline` requires an exact matching request, a successful validation bound to both cached CSV checksums and the retrieval ID, and matching instrument, row count, and timestamp range. Offline reuse rejects malformed, future-dated, incompatible, or over-age cache metadata. The default maximum age is seven calendar days; it is a configurable research freshness policy, not a Yahoo, exchange, or regulatory guarantee. Set another positive value with `--max-cache-age-days`. To knowingly use an older cache, specify both `--offline` and `--allow-stale-cache`; the override, age, and stale status are recorded in quality metadata and the report. Online mode continues to download rather than silently trusting an existing cache.

### Dashboard historical replay

The **Historical NIFTY ORB replay** panel starts a separate, in-memory replay using the same validated Yahoo/cache pipeline and `BacktestRunner` as the batch report. Choose an exclusive-end date range of at most 60 calendar days; the API validates the same OHLC/session quality gate before processing. Online mode attempts a fresh Yahoo download and does not fall back to stale data. Enable offline mode to require an exact validated cache match; the stale-cache override is available only when offline mode is selected. Optional EMA(50) and opening-range/ATR filters match the research runner.

Replay candles are advanced one at a time in chronological order. A breakout event is finalized with the following candle's open only when that next candle is available to the runner; entries and exits are emitted to the API event stream and the dashboard polls progress, open simulated index position, and results. Open-position unrealized points use the most recently processed candle close. Up to three replays can run concurrently, with a bounded in-process history of 20 sessions and 500 retained events per session. Start and stop are supported; pause/resume and durable replay recovery are not. Stopping prevents later candles from being processed after the current candle boundary. A process restart discards replay session state. Results show gross underlying-index points only, not option premiums, rupee P&L, or the separate AOTS paper ledger. No replay request creates a database trade or calls a broker.

Endpoints: `POST /api/replay/start`, `GET /api/replay`, `GET /api/replay/{session_id}`, `GET /api/replay/{session_id}/events?after=0`, `GET /api/replay/{session_id}/results`, and `POST /api/replay/{session_id}/stop`.

For an auditable date classification, `--session-calendar path.json` accepts a manually maintained JSON calendar, for example:

```json
{
  "source": "URL or description of the manually checked exchange notices",
  "holidays": ["2026-10-19"],
  "special_sessions": ["2026-10-20"],
  "regular_sessions": ["2026-10-21"]
}
```

Each date list is optional; dates must be unique across lists and use `YYYY-MM-DD`. The supplied file is hashed and its classifications are included in the report. This is user-provided evidence, not an automatically verified NSE calendar. An in-range date explicitly listed under `regular_sessions` but absent from the data is a known coverage gap and fails validation. Other weekdays with no returned bars remain unresolved between holiday, special/exceptional session, and provider gap; they are not silently treated as holidays. Cross-check dates and Muhurat/exceptional sessions against the [official NSE holiday list](https://www.nseindia.com/resources/exchange-communication-holidays) and exchange notices. Special sessions are excluded because the regular-session ORB schedule does not apply.

Incomplete or invalid sessions are excluded and recorded; duplicate/out-of-order timestamps, off-session rows, calendar-file errors, or no complete sessions fail the gate and prevent the strategy from running. Successful runs write `orb_backtest_report.md`, `orb_trades.csv`, `orb_signals.csv`, `orb_data_quality.json`, and `orb_metrics.json` under `reports/backtest` by default. A failed acquisition, cache-integrity check, or quality gate writes an explicit no-results report, marks metrics as `backtest_run: false`, and replaces generated signals/trades with empty files so an older successful result is not mistaken for the current run. Metrics include gross/net underlying points (equal only under the explicit zero-cost assumption), R multiples, win/loss statistics, profit factor when defined, close-to-close cumulative points drawdown, and session/direction breakdowns. The verdict is `inconclusive` unless the configured minimum sample and chronological out-of-sample criteria are met; even a `promising` result is not a profitability claim.

**This is not an options backtest.** `^NSEI` has no historical option premiums. The reports explicitly state that option P&L was not simulated; a separate licensed source with historical expiries/contracts, strike mapping, option prices or bid/ask quotes, lot sizes, and costs is required. No option premium, Greeks, or returns are inferred from index points.

## Historical NIFTY options ORB backtest (Groww read-only data)

The **Backtesting** tab runs a separate research simulation of the opening-range breakout on real Groww option premiums. The opening range defaults to the first 60 minutes (configurable from 15 to 60). It needs an active Groww connection (Groww tab) and uses only the read-only historical expiry, contract and candle endpoints. It sends no orders, does not use the account balance, and never touches the paper ledger, risk state or the Yahoo replay. Results live in memory only (10 runs kept, one active at a time), are restored in the dashboard after a page reload, and are discarded when the server restarts.

The options backtest interval selector supports 1-, 5-, and 10-minute candles and applies the selected interval to both index decisions and option fills/exits. The opening-range length must be divisible by that interval; the square-off time must lie on its candle grid. Choose one exit-level mode for both initial stop-loss and target: percentage of entry premium (the default; existing behavior), or fixed premium points in ₹. In fixed-point mode, a ₹5 stop with a ₹100 entry sets the initial stop to ₹95, and a ₹5 sell trigger sets the target to ₹105. A fixed stop distance must be smaller than the entry premium so the stop remains positive. The **Additional exit strategy** selector chooses None, ATR trailing stop (default), or NIFTY VWAP; the initial stop, target, and time exit remain active in all choices. ATR uses Wilder ATR(14) of option candles by default with a 2× multiplier. Its stop is the greater of the initial stop and the highest premium reached so far minus ATR × multiplier. It only ratchets upward and updates after each candle closes. Pre-entry option candles can warm up ATR; until ATR is available, the initial stop remains active. A gap in candles restarts ATR warm-up. VWAP is computed from the nearest exact NIFTY futures contract listed on or after the session date (independent of option expiry), using its session cumulative volume-weighted typical price, `(high + low + close) / 3`. For a CE it signals an exit when a completed futures candle closes below futures VWAP; for a PE, when it closes above futures VWAP. The exit is simulated at the next option candle open to avoid using a price before the signal is known. VWAP mode requires complete futures candles with volume; sessions with a missing futures contract, volume, or candles are excluded with a diagnostic rather than using an approximate VWAP. ORB entries continue to use NIFTY CASH index candles. If a candle touches a stop and target, the simulation assumes stop-first because OHLC data cannot establish intrabar ordering. Each trade reports the selected exit strategy, input values, computed `stop_premium` / `target_premium`, and applicable ATR/VWAP exit details. Each trade reports `highest_premium_between_entry_and_exit` and its time: the entry fill or the highest high from fully completed holding candles strictly after entry and before the exit candle. Ambiguous entry-candle pre-fill prices and exit-candle highs are excluded because their intrabar order relative to the simulated exit is unknown.

The **Peak-premium trailing stop** exit can also be selected. It trails the highest eligible option-candle high by a configurable fixed premium distance (default ₹5), with optional activation after a minimum favourable move. It is a long-premium rule and is identical for CE and PE. An optional fixed-point initial stop overrides the selected initial stop distance for this mode; if blank, the configured percentage/fixed-point stop applies. Because minute OHLC cannot establish high/low ordering, entry-candle OHLC is ignored after the open fill, and a high that raises the trail only affects the next candle. A pre-existing stop is checked against the candle open/low first, with gap-through fills at the open; if stop and target are both touched, the existing conservative stop-first rule applies. MFE/MAE and the peak are reported for all exit modes; ambiguous entry/exit candle extremes are excluded from excursion calculations. Existing brokerage, tax, exchange, and slippage inputs are applied to trailing-stop exits as they are to other exits. Bid/ask spread and market impact are not modelled. Session-end exit defaults to the configured square-off candle open; if disabled and no stop/target exit occurs in the available session data, the position is unresolved and excluded from P&L (overnight carry is not simulated).

Each trade shows realized gross/net P&L and a separate **hypothetical** gross/net P&L at its reported highest premium. The hypothetical applies the same quantity and configured cost model; it is not an executable quote or realized result. False-breakout reporting uses an explicit Yes/No field and a separate timestamp when applicable.

The **Day-wise P&L** application tab reports one row per processed session with trade count, wins, losses, win rate, gross P&L, and net P&L for the selected primary exit strategy. Sessions with no executed trades are included with zero P&L and an unavailable (N/A) win rate. Paired comparison alternatives do not get counted as additional trades.

To run a paired comparison, select **NIFTY VWAP close** or **Peak-premium trailing stop**, enable **Paired VWAP/trailing comparison**, and optionally enter extra comma-separated trailing distances (for example `3, 7.5, 10`). The selected trailing distance is always included. Every variant uses the same signal, contract, fill, quantity, cost model, and shared non-overlapping entry schedule; the next entry waits until all variants have exited. The report provides paired trade rows and per-variant net P&L, win rate, profit factor, drawdown, average profit/loss, consecutive losses, favourable excursion, peak giveback and exit reasons. Results are a historical comparison only; distances are not auto-selected, and no profitability is implied.

False-breakout analysis is reported per trade and summarized in the Backtesting results. A trade is marked when a completed NIFTY candle closes back inside the opening-range high/low after entry and no later than the simulated exit time; the report includes that close time. This is a descriptive price-action rule, not proof that a breakout was false or a predictive signal.

The separate **Trade Loss Analysis** tab generates a report only for completed primary trades whose realized net P&L is negative. It reconstructs the ORB range, signal, entry/exit, option and NIFTY CASH candle timeline, stop-state events, excursion/giveback measurements, data-quality observations, and evidence-labeled findings with test-oriented recommendations. Use the date, instrument, side, exit-reason and minimum-loss filters; reports can be sorted and exported as filtered CSV. Recurring-pattern summaries include sample sizes and loser-versus-winner comparisons, so they are descriptive and do not establish causality or statistical significance. The forensic report does not change strategy rules or simulate alternate exits. Its timestamps retain provider semantics (not independently verified), execution/fill assessment is model-only, and historical Greeks, IV, bid/ask, market depth, broker fills, and charting are unavailable rather than inferred. Reports follow the existing in-memory run retention (10 runs) and are not preserved across server restarts.

The detailed rule list below describes legacy ATR defaults; the current **Additional exit strategy** selector controls whether ATR is enabled, VWAP is used instead, or neither additional exit is used. Initial SL, target, and time exit apply in every mode.

For VWAP exits, Groww's NIFTY CASH index candles do not provide volume, so AOTS finds the nearest exact NIFTY futures contract listed on or after the session date (independent of the option expiry) and calculates session VWAP from its completed volume-bearing candles. CE exits are triggered by a futures close below futures VWAP; PE exits by a futures close above it. ORB entries continue to use NIFTY CASH index candles, and exits are simulated at the next option candle open. If no exact listed future or complete futures candles with volume are available, the session is excluded with a diagnostic instead of using a volume-free approximation.

Rules (also returned with every result): signals use NIFTY index candles (`NSE-NIFTY`, CASH segment); the opening range is the high/low of the first 60 minutes by default (configurable 15-60); a later close crossing outside the range triggers a CE (up) or PE (down) buy, with an optional CE-only/PE-only mode. Repeated closes beyond the same boundary are one breakout event; a new same-side event requires a close back inside the range, while a direct close beyond the opposite boundary is a new event. The entry is the **open of the option candle that starts when the signal candle closes** - never the signal candle's own price. The ATM strike is the signal close rounded to the strike step (optionally OTM by N steps). The expiry is the nearest Groww-listed expiry on or after the session date plus the minimum days to expiry, and the contract is the exact symbol for that expiry, strike and CE/PE; no other contract is ever substituted. Groww's contracts endpoint can return a mixed list containing the expiry's `FUT` symbol; AOTS validates that format, excludes futures from the options-only list, and never sends a future to the options candle resolver. Groww's contracts endpoint can omit contracts that do have candles (observed live: NIFTY 27Oct26 22500 PE was missing while its CE and its candles were available). When the exact symbol is not in the list, AOTS requests that same symbol's candles; if they come back and pass every validation, the trade is used and flagged `not in the Groww contracts list`. Otherwise the signal is skipped with a distinct reason: `option_no_data` (no candles returned), `contract_candles_unavailable` (Groww rejected the candle request, so it could not be verified, with a listing diagnostic) or `option_data_invalid`. 'Not listed by the contracts API' is never treated as 'candles unavailable', and provider errors (auth, denied, rate limit, 5xx) still fail the run. The initial stop/target use either percentage-of-entry-premium levels (default) or fixed premium-point distances in ₹; e.g. entry ₹100 with stop ₹5 and sell trigger ₹5 yields initial stop ₹95 and target ₹105. A fixed stop must be less than entry premium. ATR trailing stop behavior applies only when selected: it uses Wilder ATR from option candles (default period 14 and multiplier 2), is the greater of the initial stop and the highest premium reached since entry minus ATR × multiplier, only ratchets upward, and updates after each completed candle. Pre-entry candles may initialize ATR; until enough consecutive candles are available, the initial stop remains in force. VWAP behavior applies only when selected: it uses the nearest exact NIFTY futures contract listed on or after the session date and requires complete futures candles with volume; CE exits follow a futures close below futures VWAP and PE exits a close above, simulated at the next option candle open. In all modes the position exits at whichever applies first: target, initial stop, selected additional exit, or configured time exit (default 15:15). Stop gaps fill at the open, and if one candle touches both a stop and target the simulation assumes stop-first. Entries after the cutoff (default 15:00) are skipped; trades per session are capped (default 1). Timestamp convention: Groww's docs and the installed `growwapi` 1.5.0 client do not establish whether a stamp is a candle start or end. AOTS excludes the final 15:30 row for both index and option data by its explicit session boundary, regardless of OHLC or volume, and records a warning. It does not shift timestamps or use that row in signals or fills. Other timestamp/grid validation remains strict. Premiums are never forward-filled or estimated: a missing entry candle skips the signal, a missing exit candle leaves the trade unresolved and out of P&L, and both are reported.

ORB signal handling follows the existing NIFTY ORB event model: repeated closes beyond the same opening-range boundary are one breakout event. A fresh same-side event requires a close back inside the range first; a direct close through the opposite boundary is a new event. Re-entry is evaluated only after the prior option trade exits. The daily trade limit is configurable above 3 (API hard limit 1,000); the UI has no maximum of 3. This corrects repeated same-breakout entries and does not guarantee a higher win rate. Option stop/target values and ATR are based on option premium and are not automatically equivalent to the underlying-index stop and 2R target in the Yahoo index ORB.

P&L: gross = (exit - entry premium) x quantity, quantity = lots x lot size. Charges (brokerage, STT, exchange, stamp duty, GST) and slippage are user-entered, itemised, and all default to zero; with no costs configured the net figure equals gross and is labelled as not cost-modelled. Max drawdown is measured on the closed-trade equity curve; profit factor is `null` when there are no net losses.

Limitations to read before trusting a result: **lot size is user-supplied** (Groww's historical endpoints return none) and applied to the entire run; candle timestamps carry no timezone and are assumed to be candle-start in exchange-local time (verified only as sitting on the 09:15-15:25 grid); whether the account is entitled to `NSE-NIFTY` CASH candles is unverified; tick-size rounding, spreads and liquidity are not modelled; weekdays with no data (possibly holidays) and sessions with invalid or incomplete data are excluded and counted, which can bias results; this calendar-date approach is not an exchange calendar. Automated tests use mocked data only, so they verify the rules, not the quality of real Groww data - validate a run against your own records before relying on it. Provider failures (401/403/429/5xx/malformed) fail the whole run with a category and are never shown as zero trades.

Endpoints: `POST /api/options-backtest/runs`, `GET /api/options-backtest/runs`, `GET /api/options-backtest/runs/{run_id}`, `POST /api/options-backtest/runs/{run_id}/stop`. Windows are at most 31 calendar days. The end date may be today only at or after 15:30 Asia/Kolkata; future dates and today's date before that time are rejected. This cutoff gives the exchange session time to close, but does not guarantee Groww has finished publishing all candles; the existing data-quality checks and diagnostics still apply. Groww requests are paced by 0.2 seconds.

## Indian retail algo compliance

See `docs/COMPLIANCE_CHECKLIST_INDIA.md`. This is an engineering checklist, not legal advice. SEBI/exchange requirements and broker implementation procedures change; verify the latest official circulars, exchange notices, and Groww's current API terms before enabling any live execution.

## Production gaps (not yet implemented)

This starter is not production-ready. Before real-money use, implement and independently test: official instrument-master refresh; live market-data subscriptions with source timestamps; exchange calendar; broker-side order/position reconciliation; broker-compatible order-state machine; order and fill persistence; margin checks; exchange/broker-approved algo registration and tagging; secure secret storage; operational alerting; independent watchdog; backups; deployment hardening; and incident/recovery drills. Keep live execution disabled until all requirements are signed off.
