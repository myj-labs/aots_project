# Groww integration boundary

## What this starter does

- Identifies Groww as the planned broker.
- Defines `BrokerReadOnly` and `GrowwReadOnlyAdapter` interfaces.
- Does not create a live SDK client, authenticate, stream market data, or place/cancel orders.
- Does not store credentials in source code.

## Official documentation to review

- Groww Trading API overview: https://groww.in/trade-api/docs
- Python SDK overview: https://groww.in/trade-api/docs/python-sdk
- Option chain / Greeks: https://groww.in/trade-api/docs/curl/live-data
- Orders: https://groww.in/trade-api/docs/curl/orders

The official docs describe F&O option-chain access and order management. Availability is not equivalent to permission for every account or strategy. Confirm API subscription, account eligibility, current authentication/session requirements, product types, market-data limits, order limits, and retail algo workflow with Groww.

## Safe implementation sequence

1. Implement read-only authentication and option-chain access with explicit timeouts, retry/backoff, rate-limit handling, response schema validation, and redacted logs.
2. Maintain an exchange instrument master with expiry, strike, option type, lot size, tick size, and contract status. Never hardcode current lot sizes or expiries.
3. Add live market data only after reconnect and stale-data tests pass.
4. Reconcile broker orders, trades, and positions before considering any execution module.
5. Treat broker-side order state as authoritative after a timeout. Do not blindly retry order-creation requests; use idempotency/order-reference support where documented and reconcile first.
6. Add execution only as a separately reviewed module after regulatory/broker approvals. Keep a separate kill switch, audited approvals, and explicit deployment gate.

## Important limitations

TOTP/token automation is intentionally not implemented. Groww authentication options and daily session behavior can change; use the current official documentation. Do not put tokens or TOTP seeds into source control, dashboard JavaScript, logs, or support tickets.
