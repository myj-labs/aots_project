# Architecture and design notes

## Runtime modules

- `aots.api`: FastAPI dashboard/API boundary
- `aots.config`: validated environment configuration; paper-only guard
- `aots.risk`: deterministic entry risk checks and circuit breakers
- `aots.paper`: local simulated broker, P&L marks, simulated stop behavior
- `aots.strategy`: pluggable protocol, safe no-op, and deterministic supplied-input threshold demonstration
- `aots.strategy_service`: pure decision evaluation and explicit risk-gated paper processing
- `aots.brokers.base`: read-only broker contract
- `aots.brokers.groww`: placeholder read-only adapter boundary
- `aots.database`: SQLite persistence for trades, app state and audit events
- `aots.backtest`: yfinance acquisition/cache, OHLC quality gate, shared chronological underlying-only ORB executor, in-memory replay service, and research reports; replay remains separate from paper trading

## P&L semantics

For this starter, long-option P&L is `(mark_or_exit_price - entry_price) * quantity`. Daily realised P&L includes only trades whose `closed_at` date, converted to Asia/Kolkata, matches the current calendar date. Unrealised P&L from all currently open paper positions is included. Fees, taxes, slippage, contract multiplier semantics and fill-level accounting are not yet modeled.

## State and restart behavior

SQLite stores trades, events and circuit-breaker state. The active session date is persisted in `app_state`; when the Asia/Kolkata calendar date changes, the consecutive-loss count and daily pause reset while the kill switch, trade ledger and event history are preserved. This is calendar-date rollover, not an exchange holiday or trading-session calendar. A process restart does not restore a strategy because no strategy runs by default.

Paper entry requests require an idempotency key and a finite market-data-age value. SQLite serializes session rollover, risk validation, duplicate-key lookup, trade creation and the opening event. Mark and close transitions condition on the trade still being open, and their ledger, risk-state and audit-event writes commit as one transaction. A persistence exception rolls back the state transition; the API does not return a completed trade.

Each mark/close transaction reads the latest committed trade row after acquiring SQLite's write lock, then computes and records the transition before committing. Concurrent marks are serialized in database-lock acquisition order; this serializes state mutation but does not determine which market observation is fresher.

Mark requests require a positive, caller-supplied `observation_sequence` greater than the trade's persisted `mark_sequence`. Missing or malformed sequences return HTTP 422; older and duplicate values return HTTP 409 and do not generate a successful-mark audit event. Sequence validation, mark/P&L/stop calculation, trade update and audit/risk changes are in the same write transaction. A stop is evaluated only for an accepted sequence. The SQLite migration adds `mark_sequence INTEGER NOT NULL DEFAULT 0` without removing existing rows; the first accepted update to a migrated trade must have a positive sequence. The API itself does not generate or verify market-source chronology; a source-generated monotonic per-trade sequence is required to make chronology claims. Receipt or database lock order is not market-data freshness.

Paper entries can optionally persist `take_profit_profit_inr`, a positive target for total trade P&L in rupees. P&L uses `(mark - entry) * quantity`, with `quantity` representing total option units across all lots; `lots` is not multiplied a second time. This starter has no authoritative lot-size master, so callers supply the correct total quantity (for example, 10 lots × 65 units = quantity 650). On an accepted mark, P&L is calculated and rounded to paise; the existing stop condition (`mark_price <= stop_price`) is checked first, followed by closure when calculated profit reaches or exceeds the configured target. The trade closes at the observed mark price, records reason `SIMULATED_TAKE_PROFIT`, and writes trade/risk/audit changes in the same transaction. No fill at an implied threshold premium or intrabar price path is inferred. Trades without a target retain stop and manual-close behavior. Additive nullable `take_profit_profit_inr` and `close_reason` columns preserve legacy records. A legacy `take_profit_price` column is left intact and is not copied into `take_profit_profit_inr`, avoiding reinterpretation of premium values as total profit.

Market-data age is supplied by the caller and is not independently verified: there is no connected feed or source timestamp in the current architecture. Trading costs are not represented in the trade schema and are not deducted from P&L.

## Strategy demonstration

`SpotThresholdStrategy` implements the existing `Strategy.evaluate(MarketSnapshot)` contract. Its sole rule is to signal when supplied spot is at or above configured `AOTS_STRATEGY_SPOT_THRESHOLD`. `StrategyService.evaluate` is side-effect free. The explicit process route persists a structured outcome, reuses `RiskEngine.validate_entry`, and conditionally creates a paper trade. Input observation timestamps must include a timezone and pass the configured maximum-age check; caller-supplied timestamps and prices remain unverified.

The additive `strategy_decisions` table is keyed by `(strategy_id, observation_id)` and stores a canonical input/configuration fingerprint and the complete outcome. `BEGIN IMMEDIATE` serializes lookup, fingerprint comparison, risk validation, trade insertion, audit events and decision insertion. Identical retries return the original outcome; a changed payload for the same observation conflicts. These records are strategy-decision history only and do not duplicate trade or risk state.

## Historical ORB replay

`POST /api/replay/start` runs the same `BacktestRunner` used by `run_backtest`, after the existing acquisition, cache-integrity, and OHLC/session validation pipeline passes. The worker advances one validated bar at a time; the runner computes indicators from bars already seen and resolves next-open entries only when the next bar is supplied. The existing ORB transition, exit, stop/target, time-exit, and index-point accounting rules are shared rather than reimplemented for replay.

Replay sessions, bounded event histories, progress, and results are held only in the current process. The service allows at most three concurrent sessions, retains up to 20 sessions, and keeps up to 500 events per session. Status and event polling do not block bar execution; processing runs in a worker thread. Stop takes effect at the next bar-processing boundary; pause/resume and persistence across server restarts are not implemented.

Replay results are research-only NIFTY index points under the batch engine's zero-cost assumptions. While a replay position is open, its unrealized points are marked to the most recently processed candle close. They are not option P&L or rupee capital results. The replay service does not use the paper broker, `RiskEngine`, SQLite trade/event ledger, Groww provider, or Groww account balance. The UI displays replay outputs in a dedicated section and keeps them separate from paper trades and account insights. Historical Yahoo candle-label convention remains an unverified assumption as documented in the backtest section.

## API security

The starter is intended for local development. It does not include user authentication, CSRF protection, role-based access, or hardened internet-facing deployment. Do not expose the dashboard to the public internet. Bind to localhost for local use or add an authenticated, network-restricted deployment front end before remote access.
