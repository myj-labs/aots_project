# Groww read-only integration

## Current behavior

The dashboard's **Groww** section reuses the in-memory connection/session and account-insights flows, and provides a manually triggered historical-data capability check. The separate **Live Paper ORB** tab can explicitly start a read-only NIFTY/index and option market-data feed. Runtime credentials are sent only to the backend connect request, cleared from the browser form, held in memory by the provider session, and never persisted or returned. AOTS uses no Groww order-placement, modification, or cancellation endpoint.

## Live Paper ORB

The Live Paper ORB tab requires a connected Groww session and a user-initiated start. It obtains the NIFTY index exchange token from Groww's public instrument master (the NIFTY cash-index token is the string `NIFTY`), subscribes to index observations, and builds interval candles from source-timestamped feed messages. It requires a complete opening range, then uses the historical engine's `compute_opening_range`, `find_breakout`, and `target_strike` rules. After a signal it requests the selected expiry's Groww option chain, resolves the exact option's numeric FNO token, and subscribes to its LTP. Only a fresh option observation timestamped after the signal can become the local paper fill.

All simulated entries go through `PaperBroker.open_trade`, which invokes the existing `RiskEngine` atomically. Subsequent fresh option LTPs update the same paper ledger and can trigger its configured stop or optional total-position take-profit. The configured square-off time follows the project's 15:15 Asia/Kolkata convention by default. Stopping disables new entries; an open position remains monitored until it closes. If the application/provider session stops while a position is open, the SQLite trade remains open and the UI requires explicit recovery after Groww reconnect. Recovery subscribes to the existing contract for exit monitoring only and does not resume new signals.

The live feed adapter uses a bounded/timeout Groww socket-token request and in-memory NATS JWT/signature callbacks. NATS challenge signatures are Ed25519-signed and base64-encoded before being passed to the NATS client. It calls the canonical socket-token URL without a trailing slash; Groww redirects the SDK's trailing-slash URL with HTTP 307, and AOTS does not follow redirects for authenticated requests. It intentionally avoids the installed Groww SDK convenience feed because that implementation writes socket credentials to fixed `a.creds` and `b.creds` files and does not expose a public shutdown method. Groww protobuf definitions and topic builders are used only for decoding/read-only subscriptions. No feed is connected during app startup or automated tests.

The **Groww log** tab also includes Live feed diagnostics. This bounded in-memory log records startup stages, socket-token HTTP status, and safe exception class/message details for NATS connection failures. Credential-like text is redacted; raw provider response bodies, credentials, and tokens are not recorded. Diagnostics are lost when the process restarts and can be cleared independently from the historical response log.

Data and execution limits:

- Tick-built index bars have no synthesized volume. An interval with no ticks remains missing, and a missing opening-range bar blocks the day's ORB signals.
- The first fresh option LTP received after the signal is the simulated fill. It is not a guaranteed exchange fill or bid/ask execution.
- The current paper ledger does not apply spread, brokerage, slippage, STT, or exchange costs to these live paper trades.
- The existing paper broker manages the configured premium stop and optional total-position profit target using observed LTPs; this is not an intrabar stop execution simulator.
- Calendar-date handling uses Asia/Kolkata and is not an exchange holiday or trading-session calendar.
- Automated tests inject fake market-data feeds and never contact Groww. Do not enable a real feed solely as a test.

The historical diagnostic calls Groww's documented read-only endpoints:

- `GET /v1/historical/expiries` for NSE/NIFTY with year and month.
- `GET /v1/historical/contracts` for NSE/NIFTY and an expiry date.
- `GET /v1/historical/candles` for a Groww option symbol, the FNO segment, a time range, and a candle interval.

The official [Historical Data](https://groww.in/trade-api/docs/curl/historical-data) documentation describes FNO data from 2020 and shows historical option contracts, CE/PE symbols, rupee OHLC candles, volume, and open interest. It does not guarantee any particular account's access. A 403 response is presented as access denied; the response does not establish whether the cause is subscription, account permission, or another provider policy. Do not treat an entitlement as confirmed until the user's own connection returns data.

The dashboard requires explicit actions to load expiries, load contracts, and test candles. The candle request is bounded by AOTS to one day and uses the documented 5-minute and 15-minute examples. It validates the response shape and reports candle count, timestamps, duplicate/order/range issues, and invalid OHLC/OI rows. It does not persist the response, create historical trades, update simulated capital/P&L, or touch paper-trade/risk state.

Groww's documented candle timestamps have no timezone offset. AOTS preserves them as timezone-unspecified wall-clock values; the exchange-local interpretation and session completeness are not verified. Invalid candle data is diagnostic output only and must not be treated as validated backtest input.

## Relevant official documentation

- Groww Trading API overview: https://groww.in/trade-api/docs
- Historical data and FNO backtesting workflow: https://groww.in/trade-api/docs/curl/historical-data
- Backtesting API description: https://groww.in/trade-api/docs/curl/backtesting
- Instruments: https://groww.in/trade-api/docs/curl/instruments
- Option chain / live data: https://groww.in/trade-api/docs/curl/live-data
- Orders (not used by AOTS): https://groww.in/trade-api/docs/curl/orders

Groww API availability, account eligibility, subscriptions, permissions, and rate limits can vary. Verify the current official terms and the user's actual entitlements. AOTS does not infer access from documentation or from current option-chain access.

## Safety boundaries

- Groww account balance and positions remain read-only and are displayed separately from AOTS simulated capital and paper P&L.
- Historical data checks run only after an explicit user action and do not use the Yahoo Finance ORB pipeline.
- No result from this diagnostic is a historical options backtest or an options P&L result.
- Do not expose the app on the public internet; current app security is intended for local development.
- No broker order endpoint, order data flow, or order-state reconciliation is part of this capability check.
