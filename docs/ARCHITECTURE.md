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

For this starter, long-option P&L is `(mark_or_exit_price - entry_price) * quantity`. Fees, taxes, slippage, contract multiplier semantics and fill-level accounting are not yet modeled. This is sufficient only for exercising the app workflow and risk gate.

## State and restart behavior

SQLite stores trades and circuit-breaker state. The consecutive-loss count and paused-for-day flag currently persist until manually reset in the database; daily session rollover/reset is not automated. That is intentionally conservative but needs a market-calendar-aware session manager before serious testing. A process restart does not restore a strategy because no strategy runs by default.

## API security

The starter is intended for local development. It does not include user authentication, CSRF protection, role-based access, or hardened internet-facing deployment. Do not expose the dashboard to the public internet. Bind to localhost for local use or add an authenticated, network-restricted deployment front end before remote access.
