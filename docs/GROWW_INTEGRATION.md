# Groww read-only integration

## Current behavior

The dashboard's **Groww** section reuses the in-memory connection/session and account-insights flows, and provides a manually triggered historical-data capability check. Runtime credentials are sent only to the backend connect request, cleared from the browser form, held in memory by the provider session, and never persisted or returned. AOTS uses no Groww order-placement, modification, or cancellation endpoint.

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
