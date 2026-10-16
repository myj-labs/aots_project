# Architecture and design notes

## Runtime modules

- `aots.api`: FastAPI dashboard/API boundary
- `aots.config`: validated environment configuration; paper-only guard
- `aots.risk`: deterministic entry risk checks and circuit breakers
- `aots.paper`: local simulated broker, P&L marks, simulated stop behavior
- `aots.strategy`: pluggable protocol and safe no-op strategy
- `aots.brokers.base`: read-only broker contract
- `aots.brokers.groww`: placeholder read-only adapter boundary
- `aots.database`: SQLite persistence for trades, app state and audit events

## P&L semantics

For this starter, long-option P&L is `(mark_or_exit_price - entry_price) * quantity`. Daily realised P&L includes only trades whose `closed_at` date, converted to Asia/Kolkata, matches the current calendar date. Unrealised P&L from all currently open paper positions is included. Fees, taxes, slippage, contract multiplier semantics and fill-level accounting are not yet modeled.

## State and restart behavior

SQLite stores trades, events and circuit-breaker state. The active session date is persisted in `app_state`; when the Asia/Kolkata calendar date changes, the consecutive-loss count and daily pause reset while the kill switch, trade ledger and event history are preserved. This is calendar-date rollover, not an exchange holiday or trading-session calendar. A process restart does not restore a strategy because no strategy runs by default.

## API security

The starter is intended for local development. It does not include user authentication, CSRF protection, role-based access, or hardened internet-facing deployment. Do not expose the dashboard to the public internet. Bind to localhost for local use or add an authenticated, network-restricted deployment front end before remote access.
