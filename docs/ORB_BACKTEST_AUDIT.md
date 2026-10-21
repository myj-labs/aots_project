# Sprint 12 ORB Backtest Audit

## Scope and baseline

This audit uses the existing cached Yahoo Finance response only. No replacement data was downloaded, no strategy parameters were changed, and the ORB remains research-only and separate from paper trading.

- Branch: `sprint12_orbTest`
- HEAD at audit start: `cc218dbc5f579882a14be1d444e438ce79efcc7b` (`cc218db`)
- Existing worktree at audit start: Sprint 12 changes in `.gitignore`, `README.md`, `docs/ARCHITECTURE.md`, `pyproject.toml`, `aots/backtest/`, and `tests/test_backtest.py`; preserved.
- Baseline test run before audit edits: `173 passed, 1 warning` (existing Starlette/httpx deprecation).
- Request: `^NSEI`, 5-minute bars, 2026-09-21 inclusive through 2026-10-21 exclusive, Asia/Kolkata.
- Cache: 1,500 rows, 20 observed dates, 18 eligible sessions, 2 rejected sessions. Retrieval timestamp in the manifest: `2026-10-20T20:23:08.521129+00:00`.
- Both normalized-cache and original-response SHA-256 values match the manifest. Offline loading and validation reproduced the same input and results; no live Yahoo request was made.

## Findings and correction

### Breakout events were being overcounted

Before correction, the engine created a signal row for every candle close outside the opening range, including repeated consecutive closes on the same side. In the baseline CSV, 802 rows were labeled signals, but only 17 were accepted for entry; 591 rows were rejected because a position was already open. A single uninterrupted outside-range stretch could therefore add dozens of purported signals without a new breakout.

The count also depended on the filter variant. Position occupancy and skipped exit candles altered which repeated close rows were emitted, so the ATR variant showed 816 rows while baseline showed 802. These were not comparable raw-event counts. The prior rows combined repeated outside closes, filter failures, position/capacity rejections, and entry timing failures under one “signal” count.

The event definition is now explicit and independent of filter or position state: one raw event occurs when a completed close transitions from inside/no-breakout to strictly above or below the frozen opening range, or directly changes to the opposite side. Consecutive closes on the same side do not create another event. A close back inside re-arms a later breakout in the same direction. Each event is evaluated once; filters and entry constraints can reject it. An event on a candle that also exits a position is recorded but not entered because intrabar ordering is unknown. Existing one-position-at-a-time, one-completed-trade-per-session, next-candle-open, stop, target, and time-exit rules remain in place.

Signals now record whether indicator filters passed and whether a simulated entry was opened. Metrics separate raw events, filter-passing/rejected events, entry attempts, entry-rejected events, and completed trades. The signal CSV retains the combined rejection reason for each event; reports include per-reason counts for each configuration.

### Data-quality gate and excluded rows

The four invalid OHLC rows in the cached original Yahoo CSV are all null OHLC values:

| Timestamp (Asia/Kolkata) | Session | Source row (CSV line) |
|---|---|---:|
| 2026-09-21 15:20 | 2026-09-21 | 75 |
| 2026-09-21 15:25 | 2026-09-21 | 76 |
| 2026-10-15 11:45 | 2026-10-15 | 1307 |
| 2026-10-15 11:50 | 2026-10-15 | 1308 |

The affected dates are excluded as whole sessions before the engine computes EMA/ATR, opening ranges, events, entries, exits, or P&L. A regression test verifies the eligible session alone reaches strategy calculations. Thus “quality passed” means the remaining eligible subset passed; it does not mean every downloaded row was valid. The 18 eligible sessions each had the expected 75 timestamps. The two invalid dates are distinct from the weekdays with no provider rows: 2026-10-02 and 2026-10-20.

The cached timestamps convert to Asia/Kolkata and span 2026-09-21 09:15 through 2026-10-19 15:25. The data and manifest contain 1,500 rows, with no duplicate timestamps, off-session rows, or missing expected candle timestamps. The cache treats labels as candle starts, matching the expected 09:15–15:25 labels, but this is not authoritative proof of Yahoo's timestamp convention. No official NSE calendar was successfully verified during the audit; the two no-row weekdays therefore remain unresolved as exchange holidays versus provider gaps. Special sessions also remain unmodeled.

## Results on identical cached input

### Before and after event correction

| Configuration | Prior reported rows | Prior accepted entries | Prior trades | Prior gross points | Corrected raw events | Filter rejected | Filter passing | Entry attempts | Entry rejected | Corrected trades | Corrected gross points |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Baseline | 802 | 17 | 17 | 686.4473 | 46 | 0 | 46 | 17 | 29 | 17 | 686.4473 |
| EMA(50) | 801 | 17 | 17 | 802.9961 | 46 | 5 | 41 | 16 | 25 | 16 | 820.7461 |
| OR width / ATR(14) <= 2 | 816 | 2 | 2 | -21.6523 | 46 | 45 | 1 | 1 | 0 | 1 | 141.3984 |

“Entry attempt” here means a filter-passing event that passed entry constraints and opened a simulated position; blocked events are counted as entry rejections. A filter-rejected event can also have an entry-condition reason, but it is counted as filter-rejected rather than as a filter-passing entry rejection.

The 46-event count is identical across variants because event generation uses the same valid sessions and candle closes before variant filters or position state are applied. The ATR result changed from two trades to one because the prior repeated-close logic could re-enter during a continuous outside-range stretch; the transition rule permits only a new event after re-arming. Baseline trades and their P&L are unchanged. Filter variants are descriptive experiments, not optimized recommendations.

### Rejection reasons after correction

Counts below are components of per-event reasons; an event can have more than one reason.

| Configuration | Rejection reason | Count |
|---|---|---:|
| Baseline | another position is already open | 17 |
| Baseline | position exited on this candle | 4 |
| Baseline | entry deferred because intrabar ordering is unknown | 4 |
| Baseline | maximum completed trades per session reached | 12 |
| Baseline | next candle opens after the entry cutoff | 3 |
| EMA(50) | EMA alignment rejected (bullish) | 3 |
| EMA(50) | EMA alignment rejected (bearish) | 1 |
| EMA(50) | EMA unavailable during warmup | 1 |
| EMA(50) | another position is already open | 17 |
| EMA(50) | position exited on this candle / intrabar entry deferred | 4 each |
| EMA(50) | maximum completed trades per session reached | 11 |
| EMA(50) | next candle opens after the entry cutoff | 3 |
| OR width / ATR(14) <= 2 | opening-range width exceeded ATR ratio | 45 |
| OR width / ATR(14) <= 2 | another position is already open | 1 |
| OR width / ATR(14) <= 2 | next candle opens after the entry cutoff | 3 |

The full detailed component counts and combined reasons per raw event are in `reports/backtest/orb_metrics.json` and `reports/backtest/orb_signals.csv` (local generated outputs, ignored by Git).

## Ledger and execution reconciliation

The regenerated baseline trade ledger independently sums to **+686.447265625** points:

- 17 trades: 11 wins, 6 losses, 0 breakeven.
- Long: 9 trades, **-32.8984375** points.
- Short: 8 trades, **+719.345703125** points.
- Gross profit factor: 2.41069679698.
- Trade-close drawdown, starting equity/peak at zero: 325.052734375 points.
- Sum of session P&L and sum of directional P&L both reconcile to the trade ledger total.
- Two repeated executions against the same offline cache produced identical signals, trades, and metrics for baseline, EMA, and ATR variants.

The single best trade was a 2026-10-08 SHORT, +242.69921875 points. Removing it leaves **+443.748046875** points across 16 trades; it contributed approximately 35.36% of baseline gross points. This is a sensitivity diagnostic only, not statistical evidence or a replacement strategy. No long-only or short-only recommendation is made.

Execution checks retained or extended in tests cover opening-range and completed-candle timing, next-candle entry, future-bar invariance of EMA/ATR, one-position/session constraints, stop/target gaps and same-candle stop-first handling, long/short signs, drawdown, metric reconciliation, and deterministic reruns. The full research result remains underlying points, not option P&L; fees, slippage, multipliers, and actual option contracts are absent.

## Changes and validation

- Added close-transition breakout event detection, explicit event/entry metrics and reason counts.
- Updated the report and README to define event/re-entry semantics and distinguish raw events from filter and execution outcomes.
- Added regression tests for transition/re-arm behavior, filter-invariant raw events, invalid-session exclusion, open-position rejection, and no future-bar influence on indicators.
- Focused suite: `pytest -q tests/test_backtest.py` — **20 passed**.
- Full suite after changes: `pytest -q` — **178 passed, 1 warning** (existing Starlette/httpx deprecation).
- No commit, push, merge, or PR action was performed.

## Remaining limitations

- Only 18 eligible sessions and 17 baseline trades; no credible statistical inference or profitability claim.
- Underlying index points are not NIFTY options returns.
- No official verification of Yahoo's candle-label convention or NSE holiday/session calendar; 2026-10-02 and 2026-10-20 no-row dates remain unresolved in this artifact audit.
- Yahoo data may be revised, incomplete, or subject to source terms; this audit proves reproducibility from the local checksum-matched cache, not correctness of the source data.
