# Indian retail algo trading — pre-live compliance checklist

**Status: NOT CLEARED FOR LIVE TRADING.** This checklist is for engineering planning, not legal advice. It is not an assertion that this application meets SEBI or exchange requirements.

As of the project build date (16 October 2026), SEBI's framework for safer retail participation in algorithmic trading and exchange implementation standards are in effect. Exchange notices and broker procedures may be updated; check the latest versions directly.

## Official starting points

- SEBI circular on safer participation of retail investors in algorithmic trading (4 February 2025): https://www.sebi.gov.in/legal/circulars/feb-2025/safer-participation-of-retail-investors-in-algorithmic-trading_91613.html
- SEBI implementation timeline extension (30 September 2025): https://www.sebi.gov.in/sebi_data/attachdocs/sep-2025/1759232056254.pdf
- NSE algorithm trading / retail API information: https://www.nseindia.com/static/trade/platform-services-non-neat-decision-support-tools-algorithm-trading
- NSE empanelled algo providers and current notices: https://www.nseindia.com/static/trade/empanelled-algo-providers-exchange
- Groww Trading API documentation: https://groww.in/trade-api/docs

## Mandatory go/no-go checks before live deployment

- [ ] Obtain written confirmation from Groww of current eligibility and permitted use of its Trading API for the intended NIFTY options strategy.
- [ ] Confirm the account's API subscription, authentication requirements, order types and any broker-specific restrictions.
- [ ] Confirm the applicable SEBI and NSE framework, circulars, implementation standards, exchange registration/approval workflow, and any strategy classification that applies.
- [ ] Confirm the broker's required algo tagging, identifiers, approvals, static-IP or other infrastructure conditions, rate limits, order-rate controls, and audit/reporting obligations where applicable.
- [ ] Confirm whether the proposed client-built strategy is treated as client-direct API, broker-provided algo, vendor algo, or another category under current rules.
- [ ] Confirm required exchange registration or approval before transmitting any automated orders; do not assume a low order rate removes all obligations.
- [ ] Validate current instrument master, lot size, tick size, expiry calendar, contract eligibility, and market timings from authoritative sources.
- [ ] Implement a production-grade order state machine, idempotency, partial-fill handling, cancellation confirmation, broker reconciliation, and duplicate-order protection.
- [ ] Validate margin and available funds before order submission, including multi-leg exposure where relevant.
- [ ] Complete backtests with realistic slippage, brokerage, STT, GST, exchange charges and stamp duty; then paper-trade and conduct failure-injection drills.
- [ ] Implement secure secret management, least privilege, access controls, audit logs, alerting, backups, clock synchronization, and incident response.
- [ ] Test loss-limit breach, rejected orders, no fill, partial fill, stale data, disconnect, reconnect, process restart, broker API outage, and emergency manual intervention.
- [ ] Obtain legal/compliance review and explicit operator sign-off before enabling any live execution.

## Engineering controls already in this starter

- Paper-only mode is enforced in configuration.
- Setting `AOTS_LIVE_TRADING_ENABLED=true` causes startup validation to fail.
- No broker order-placement method is implemented.
- Entry risk checks include loss limits, loss streak, kill switch, position count, lots and stale data.
- Local audit events are recorded.

## Not implemented / not certified

This repository does not implement or certify exchange registration, broker approvals, static-IP requirements, algo tagging, order-rate compliance, official exchange data feeds, live reconciliation, or live risk controls. It should not be described as SEBI-compliant or production-ready.
