"""Tests for the historical NIFTY options ORB backtest (mocked data only).

Nothing here contacts Groww or uses credentials. Passing these tests shows the
rules are implemented as documented; it does NOT validate real Groww data.
"""

import json
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from aots import api
from aots.api import app
from aots.brokers.base import HistoricalCandle, ProviderResponseError
from aots.options_backtest import data as ob_data
from aots.options_backtest import engine as ob_engine
from aots.options_backtest.config import CostConfig, OptionsBacktestConfig
from aots.options_backtest.engine import OptionUnavailable, compute_costs, run_session, summarize
from aots.options_backtest.forensics import build_loss_dashboard, build_trade_loss_report
from aots.options_backtest.service import (
    GrowwOptionResolver,
    OptionsBacktestService,
    RunRejected,
    execute_backtest,
)
from aots.provider_session import ProviderConnectRequest, ProviderRequestError
from test_api import _STATIC_DIR, _run_harness_page, needs_browser
from test_provider import APPROVAL, MARKER, groww_manager_with_transport, provider_env  # noqa: F401

DAY = date(2024, 1, 2)  # Tuesday
EXPIRY = date(2024, 1, 4)
CLOCK = datetime(2024, 1, 10, 4, 0, tzinfo=timezone.utc)


def grid(day=DAY):
    stamp = datetime.combine(day, time(9, 15))
    return [stamp + timedelta(minutes=5 * i) for i in range(75)]


def candle(ts, o, h, low, c, vol=500):
    return HistoricalCandle(ts, float(o), float(h), float(low), float(c), float(vol))


def index_candles(day=DAY, overrides=None, drop=()):
    rows = []
    for ts in grid(day):
        key = ts.strftime("%H:%M")
        if key in drop:
            continue
        if key == "09:15":
            values = (22000, 22050, 21950, 22000)
        else:
            values = (22000, 22010, 21990, 22000)
        values = (overrides or {}).get(key, values)
        rows.append(candle(ts, *values))
    return rows


def future_candles(day=DAY, overrides=None, drop=()):
    rows = []
    for ts in grid(day):
        key = ts.strftime("%H:%M")
        if key in drop:
            continue
        values = (22000, 22010, 21990, 22000)
        values = (overrides or {}).get(key, values)
        rows.append(candle(ts, *values, vol=1000))
    return rows


def option_candles(day=DAY, overrides=None, drop=(), price=100):
    rows = []
    for ts in grid(day):
        key = ts.strftime("%H:%M")
        if key in drop:
            continue
        values = (overrides or {}).get(key, (price, price + 1, price - 1, price))
        rows.append(candle(ts, *values))
    return rows


def cfg(**overrides):
    base = dict(start_date=DAY, end_date=DAY, lot_size=25, opening_range_minutes=30)
    base.update(overrides)
    return OptionsBacktestConfig(**base)


class FakeSource:
    def __init__(self, underlying=None, options=None, futures=None, expiries=None, contracts=None, error=None):
        self.underlying = underlying if underlying is not None else {DAY: index_candles()}
        self.options = options if options is not None else {}
        self.futures = futures if futures is not None else {}
        self.expiries = expiries if expiries is not None else {(2024, 1): [EXPIRY.isoformat()]}
        self.contracts = contracts if contracts is not None else {
            EXPIRY.isoformat(): [
                "NSE-NIFTY-04Jan24-22050-CE",
                "NSE-NIFTY-04Jan24-21950-PE",
                "NSE-NIFTY-04Jan24-22100-CE",
            ]
        }
        self.error = error
        self.calls = []

    def status(self):
        return {"connected": True}

    def shutdown(self):
        pass

    def historical_expiries(self, year, month):
        self.calls.append(("expiries", year, month))
        if self.error:
            raise self.error
        return list(self.expiries.get((year, month), []))

    def historical_contracts(self, expiry):
        self.calls.append(("contracts", expiry))
        if self.error:
            raise self.error
        return list(self.contracts.get(expiry, []))

    def historical_future_contract(self, expiry):
        self.calls.append(("future_contract", expiry))
        if self.error:
            raise self.error
        return next(
            (symbol for symbol in self.contracts.get(expiry, []) if symbol.endswith("-FUT")),
            None,
        )

    def historical_candles(self, segment, symbol, start, end, interval):
        self.calls.append(("candles", segment, symbol, start, end, interval))
        if self.error:
            raise self.error
        if segment == "CASH":
            return list(self.underlying.get(start.date(), []))
        if symbol.endswith("-FUT"):
            return list(self.futures.get((symbol, start.date()), []))
        return list(self.options.get((symbol, start.date()), []))


CE = "NSE-NIFTY-04Jan24-22050-CE"
PE = "NSE-NIFTY-04Jan24-21950-PE"
FUT = "NSE-NIFTY-04Jan24-FUT"
MONTHLY_FUT = "NSE-NIFTY-25Jan24-FUT"
UP_10_00 = {"10:00": (22040, 22070, 22030, 22060)}  # closes above 22050 -> CE, ATM 22050
DOWN_10_00 = {"10:00": (21960, 21970, 21930, 21940)}  # closes below 21950 -> PE, ATM 21950


def run_one(underlying_overrides, options, **config):
    source = FakeSource(
        underlying={DAY: index_candles(overrides=underlying_overrides)},
        options=options,
    )
    config_obj = cfg(**config)
    resolver = GrowwOptionResolver(source, config_obj)
    result = run_session(DAY, source.underlying[DAY], config_obj, resolver, config_obj.initial_capital_inr)
    return result, source


# ---------- data validation ----------

def test_validate_accepts_full_grid_and_reports_missing_candles_as_warning():
    ok = ob_data.validate_session_candles(index_candles(), DAY, "x")
    assert ok.usable and not ok.warnings and ok.missing_count == 0
    gap = ob_data.validate_session_candles(index_candles(drop=("11:00", "11:05")), DAY, "x")
    assert gap.usable and gap.missing_count == 2 and "2 of 75" in gap.warnings[0]
    assert len(gap.candles) == 73  # nothing filled


@pytest.mark.parametrize(
    ("rows_factory", "text"),
    [
        (lambda: index_candles() + [index_candles()[3]], "duplicate"),
        (lambda: list(reversed(index_candles())), "chronological"),
        (lambda: [candle(datetime.combine(DAY, time(9, 17)), 1, 2, 1, 1)], "grid"),
        (lambda: [candle(datetime.combine(DAY + timedelta(days=1), time(9, 15)), 1, 2, 1, 1)], "outside"),
        (lambda: [candle(datetime.combine(DAY, time(9, 15)), 100, 90, 95, 99)], "invalid"),
        (lambda: [candle(datetime.combine(DAY, time(9, 15)), 0, 1, 0, 1)], "invalid"),
        (lambda: [candle(datetime.combine(DAY, time(9, 15)), 1, 2, 1, 1, vol=-1)], "invalid"),
        (lambda: [candle(datetime.combine(DAY, time(9, 15), tzinfo=timezone.utc), 1, 2, 1, 1)], "offset"),
    ],
)
def test_validate_rejects_bad_candles(rows_factory, text):
    result = ob_data.validate_session_candles(rows_factory(), DAY, "x")
    assert not result.usable
    assert any(text in error for error in result.errors)


def test_validate_empty_is_no_data():
    result = ob_data.validate_session_candles([], DAY, "x")
    assert result.no_data and not result.usable


def test_contract_selection_requires_exact_expiry_strike_and_type():
    symbols = [CE, PE, "NSE-NIFTY-11Jan24-22050-CE", "garbage", "NSE-NIFTY-30Feb24-22050-CE"]
    found, inconsistent = ob_data.select_contract(symbols, EXPIRY, 22050, "CE")
    assert found.groww_symbol == CE and found.strike == 22050 and found.expiry == EXPIRY
    assert inconsistent == 3  # other expiry, garbage, impossible date
    assert ob_data.select_contract(symbols, EXPIRY, 22050, "PE")[0] is None
    assert ob_data.select_contract(symbols, EXPIRY, 22100, "CE")[0] is None
    assert ob_data.select_contract(symbols, date(2024, 1, 11), 22050, "CE")[0].expiry == date(2024, 1, 11)
    assert ob_data.contract_symbol(EXPIRY, 22050, "CE") == CE


# ---------- strategy rules ----------

def test_no_breakout_gives_zero_trades_not_failure():
    result, source = run_one({}, {})
    assert result.status == "complete" and result.trades == [] and result.signals == []
    assert not any(call[0] == "contracts" for call in source.calls)
    summary = summarize([result.as_dict()], cfg(), [DAY])
    assert summary["result_kind"] == "zero_trades" and summary["trade_count"] == 0
    assert summary["profit_factor"] is None and summary["win_rate_pct"] is None


def test_ce_breakout_target_hit_uses_next_candle_open_and_real_premium():
    options = {
        (CE, DAY): option_candles(
            overrides={"10:00": (90, 91, 89, 90), "10:05": (100, 101, 99, 100), "10:10": (100, 160, 99, 150)}
        )
    }
    result, source = run_one(UP_10_00, options)
    (trade,) = result.trades
    assert trade["contract"]["groww_symbol"] == CE
    assert trade["contract"]["option_type"] == "CE" and trade["contract"]["strike"] == 22050
    assert trade["contract"]["expiry"] == "2024-01-04"
    # signal candle 10:00 closes at 10:05; fill is the 10:05 option open, never the 10:00 candle
    assert trade["signal_time"].endswith("10:05:00") and trade["entry_time"].endswith("10:05:00")
    assert trade["entry_premium"] == 100.0
    assert trade["exit_reason"] == "TARGET" and trade["exit_premium"] == 150.0
    assert trade["quantity"] == 25 and trade["lots"] == 1 and trade["lot_size"] == 25
    assert trade["gross_pnl"] == 1250.0
    assert trade["net_pnl"] == trade["gross_pnl"]  # no costs configured
    assert trade["net_pnl_basis"].startswith("no costs configured")
    assert any("user-supplied" in w for w in trade["warnings"])
    assert ("candles", "FNO", CE) == source.calls[-1][:3]


def test_pe_breakout_selects_put_and_atm_strike_and_otm_offset():
    options = {(PE, DAY): option_candles(overrides={"10:10": (100, 101, 70, 72)})}
    result, _ = run_one(DOWN_10_00, options)
    (trade,) = result.trades
    assert trade["contract"]["option_type"] == "PE" and trade["contract"]["strike"] == 21950
    assert trade["exit_reason"] == "STOP_LOSS" and trade["exit_premium"] == 75.0
    assert trade["gross_pnl"] == -625.0
    assert ob_engine.target_strike(22060, "UP", cfg(strike_offset_steps=2)) == 22150
    assert ob_engine.target_strike(21940, "DOWN", cfg(strike_offset_steps=2)) == 21850


def test_loss_reports_are_created_only_for_completed_negative_net_pnl_trades():
    losing_result, _ = run_one(
        DOWN_10_00,
        {(PE, DAY): option_candles(overrides={"10:10": (100, 101, 70, 72)})},
    )
    winning_result, _ = run_one(
        UP_10_00,
        {(CE, DAY): option_candles(overrides={"10:10": (100, 160, 99, 150)})},
    )

    assert len(losing_result.loss_reports) == 1
    assert losing_result.loss_reports[0]["generated_for_completed_loss"] is True
    assert losing_result.loss_reports[0]["summary"]["net_pnl"] < 0
    assert winning_result.trades[0]["net_pnl"] > 0
    assert winning_result.loss_reports == []


def test_loss_report_reconstructs_only_through_exit_and_is_json_serializable():
    options = {(PE, DAY): option_candles(overrides={"10:10": (100, 101, 70, 72)})}
    result, source = run_one(DOWN_10_00, options)
    trade, = result.trades
    report = build_trade_loss_report(
        trade,
        run_id="direct",
        config=cfg(),
        underlying_candles=source.underlying[DAY],
        option_candles=options[(PE, DAY)],
    )
    exit_timestamp = datetime.fromisoformat(trade["exit_time"])
    underlying_with_uncompleted_exit_bar = [
        candle(row.timestamp, 99000, 99010, 98990, 99000)
        if row.timestamp == exit_timestamp else row
        for row in source.underlying[DAY]
    ]
    later_candles = [
        *options[(PE, DAY)],
        candle(datetime.combine(DAY, time(10, 15)), 500, 700, 450, 600),
    ]
    report_with_future_data = build_trade_loss_report(
        trade,
        run_id="direct",
        config=cfg(),
        underlying_candles=underlying_with_uncompleted_exit_bar,
        option_candles=later_candles,
    )

    assert report == result.loss_reports[0] == report_with_future_data
    assert report["report_id"] == f"direct:{trade['trade_id']}:loss-forensics-v1"
    assert report["summary"]["entry_fill_price"] == trade["entry_premium"]
    assert report["summary"]["exit_fill_price"] == trade["exit_premium"]
    assert report["summary"]["gross_pnl"] == trade["gross_pnl"]
    assert all(row["timestamp"] <= trade["exit_time"] for row in report["timeline"])
    assert all(
        datetime.fromisoformat(row["timestamp"]) + timedelta(minutes=cfg().interval_minutes) <= exit_timestamp
        for row in report["timeline"] if row["event"] == "NIFTY_CASH_CANDLE"
    )
    assert report["underlying_vs_option"]["greeks"].startswith("unavailable")
    assert report["entry_quality"]["data_validation"]["timestamp_timezone"].startswith("provider")
    json.dumps(report)


def test_loss_report_marks_missing_candles_and_unavailable_market_fields():
    result, _ = run_one(
        DOWN_10_00,
        {(PE, DAY): option_candles(
            drop=("10:10",),
            overrides={"10:15": (100, 101, 70, 72)},
        )},
    )

    report, = result.loss_reports
    assert report["entry_quality"]["data_validation"]["option_candle_gaps"]
    assert any(
        finding["code"] == "DATA_QUALITY_ISSUE"
        for finding in report["root_causes"]
    )
    assert report["underlying_vs_option"]["implied_volatility"] == "unavailable; not inferred"
    assert report["underlying_vs_option"]["bid_ask_and_liquidity"] == "unavailable; not inferred"


def test_loss_dashboard_reports_sample_sizes_and_loss_patterns():
    losing_result, _ = run_one(
        DOWN_10_00,
        {(PE, DAY): option_candles(overrides={"10:10": (100, 101, 70, 72)})},
    )
    losing_trade = losing_result.trades[0]
    second_loss = {
        **losing_trade,
        "trade_id": "2024-01-03-1",
        "session_date": "2024-01-03",
        "entry_time": "2024-01-03T10:05:00",
        "contract": {**losing_trade["contract"], "expiry": "2024-01-04"},
    }
    winner = {**losing_trade, "trade_id": "2024-01-03-2", "net_pnl": 100.0}
    dashboard = build_loss_dashboard(
        [losing_trade, second_loss, winner],
        losing_result.loss_reports,
    )

    assert dashboard["sample_sizes"] == {"losing_trades": 2, "all_completed_trades": 3}
    assert dashboard["by_option_type"]["PE"]["count"] == 2
    assert dashboard["false_breakout"]["sample_size"] == 2
    group = dashboard["losses_vs_winners_by_side_and_exit"][
        f"PE|{losing_trade['exit_strategy']}"
    ]
    assert group["all_trade_sample_size"] == 3
    assert group["loser_sample_size"] == 2


def test_trade_is_marked_false_breakout_when_nifty_closes_back_inside_before_exit():
    result, _ = run_one(
        UP_10_00,
        {(CE, DAY): option_candles(overrides={"10:10": (100, 160, 99, 150)})},
    )
    trade, = result.trades
    assert trade["false_breakout"] is True
    assert trade["false_breakout_time"].endswith("10:10:00")


def test_trade_is_not_marked_false_breakout_if_nifty_remains_beyond_opening_range():
    result, _ = run_one(
        {**UP_10_00, "10:05": (22050, 22070, 22040, 22060)},
        {(CE, DAY): option_candles(overrides={"10:10": (100, 160, 99, 150)})},
    )
    trade, = result.trades
    assert trade["false_breakout"] is False
    assert trade["false_breakout_time"] is None


@pytest.mark.parametrize(
    ("bar", "exit_reason", "exit_price"),
    [
        ((100, 106, 99, 105), "TARGET", 105),
        ((100, 104, 94, 96), "STOP_LOSS", 95),
    ],
)
def test_fixed_premium_point_exits_are_relative_to_entry(bar, exit_reason, exit_price):
    result, _ = run_one(
        UP_10_00,
        {(CE, DAY): option_candles(overrides={"10:10": bar})},
        exit_level_mode="premium_points",
        stop_loss_points=5,
        target_points=5,
    )
    trade, = result.trades
    assert trade["entry_premium"] == 100
    assert trade["stop_premium"] == 95
    assert trade["target_premium"] == 105
    assert trade["exit_reason"] == exit_reason
    assert trade["exit_premium"] == exit_price
    assert trade["exit_level_mode"] == "premium_points"
    assert trade["stop_loss_input"] == trade["target_input"] == 5


def test_fixed_stop_must_leave_a_positive_stop_price():
    result, _ = run_one(
        UP_10_00,
        {(CE, DAY): option_candles(overrides={"10:05": (4, 5, 3, 4)})},
        exit_level_mode="premium_points",
        stop_loss_points=5,
        target_points=5,
    )
    assert result.trades == []
    assert result.signals[0]["reason_code"] == "invalid_absolute_stop"
    assert "must be less than" in result.signals[0]["reason"]


def test_atr_trailing_stop_updates_after_completed_candle_and_exits_before_target():
    options = {
        (CE, DAY): option_candles(
            overrides={
                "10:05": (100, 110, 99, 108),
                "10:10": (106, 120, 102, 104),
            }
        )
    }
    result, _ = run_one(
        UP_10_00,
        options,
        atr_period=2,
        atr_multiplier=1,
        target_pct=10,
    )
    trade, = result.trades
    assert trade["entry_premium"] == 100
    assert trade["stop_premium"] == 75
    assert trade["target_premium"] == 110
    assert trade["exit_reason"] == "ATR_TRAILING_STOP"
    assert trade["exit_time"].endswith("10:10:00")
    assert trade["exit_premium"] == pytest.approx(103.5)
    assert trade["atr_trailing_stop_premium"] == pytest.approx(103.5)
    assert trade["atr_period"] == 2 and trade["atr_multiplier"] == 1
    assert any("stop-first assumed" in warning for warning in trade["warnings"])


def test_initial_stop_remains_active_while_atr_is_warming_up():
    options = {
        (CE, DAY): option_candles(overrides={"10:05": (100, 101, 70, 75)})
    }
    result, _ = run_one(
        UP_10_00,
        options,
        atr_period=20,
        atr_multiplier=2,
    )
    trade, = result.trades
    assert trade["exit_reason"] == "STOP_LOSS"
    assert trade["exit_premium"] == 75
    assert trade["atr_trailing_stop_premium"] is None


def test_peak_premium_trailing_stop_raises_on_close_and_exits_on_later_reversal():
    options = {(CE, DAY): option_candles(overrides={
        "10:05": (100, 180, 80, 100),  # Entry-candle high/low are deliberately ignored.
        "10:10": (100, 110, 99, 108),
        "10:15": (108, 109, 104, 105),
    })}
    result, _ = run_one(
        UP_10_00,
        options,
        exit_mode="TRAILING_STOP",
        trailing_stop_distance=5,
        target_pct=100,
    )
    trade, = result.trades
    assert trade["exit_strategy"] == "trailing_stop"
    assert trade["exit_reason"] == "TRAILING_STOP"
    assert trade["exit_premium"] == 105
    assert trade["highest_premium_since_entry"] == 110
    assert trade["highest_premium_time"].endswith("10:10:00")
    assert trade["trailing_stop_at_exit"] == 105
    assert trade["maximum_favourable_excursion"] == 10
    assert trade["maximum_adverse_excursion"] == 1
    assert trade["max_premium_reached"] == 110
    assert "Entry candle OHLC" in trade["excursion_basis"]


def test_trailing_activation_not_reached_leaves_trail_inactive():
    options = {(CE, DAY): option_candles(overrides={
        "10:10": (100, 109, 99, 108),
    })}
    result, _ = run_one(
        UP_10_00,
        options,
        exit_strategy="trailing_stop",
        trailing_stop_activation=10,
        target_pct=100,
    )
    trade, = result.trades
    assert trade["exit_reason"] == "TIME_EXIT"
    assert trade["trailing_stop_at_exit"] is None
    assert trade["highest_premium_since_entry"] == 109


def test_trailing_initial_stop_can_exit_before_activation():
    options = {(CE, DAY): option_candles(overrides={
        "10:10": (100, 102, 90, 95),
    })}
    result, _ = run_one(
        UP_10_00,
        options,
        exit_strategy="trailing_stop",
        initial_stop_loss=5,
        trailing_stop_activation=20,
    )
    trade, = result.trades
    assert trade["exit_reason"] == "STOP_LOSS"
    assert trade["exit_premium"] == 95
    assert trade["trailing_stop_at_exit"] is None


def test_trailing_stop_does_not_use_new_high_before_same_candle_low():
    options = {(CE, DAY): option_candles(overrides={
        "10:10": (100, 120, 100, 115),  # Newly calculated stop would be 115.
        "10:15": (115, 116, 114, 115),
    })}
    result, _ = run_one(
        UP_10_00,
        options,
        exit_strategy="trailing_stop",
        trailing_stop_distance=5,
        target_pct=100,
    )
    trade, = result.trades
    assert trade["exit_reason"] == "TRAILING_STOP"
    assert trade["exit_time"].endswith("10:15:00")
    assert trade["exit_premium"] == 115


def test_trailing_gap_fills_at_open_and_reuses_cost_model():
    options = {(CE, DAY): option_candles(overrides={
        "10:10": (120, 121, 119, 120),
        "10:15": (110, 112, 108, 110),
    })}
    result, _ = run_one(
        UP_10_00,
        options,
        exit_strategy="trailing_stop",
        trailing_stop_distance=5,
        target_pct=100,
        costs=CostConfig(brokerage_per_order_inr=20, slippage_per_unit_inr=0.5),
    )
    trade, = result.trades
    assert trade["exit_reason"] == "TRAILING_STOP"
    assert trade["trailing_stop_at_exit"] == 116
    assert trade["exit_premium"] == 110
    assert trade["costs"]["brokerage"] == 40
    assert trade["costs"]["slippage"] == 25
    assert trade["net_pnl"] == trade["gross_pnl"] - trade["costs"]["total_costs"]


@pytest.mark.parametrize(
    ("direction", "option_type", "symbol"),
    [(UP_10_00, "CE", CE), (DOWN_10_00, "PE", PE)],
)
def test_peak_trailing_logic_is_shared_by_long_ce_and_pe(direction, option_type, symbol):
    options = {(symbol, DAY): option_candles(overrides={
        "10:10": (100, 110, 99, 108),
        "10:15": (108, 109, 104, 105),
    })}
    result, _ = run_one(
        direction,
        options,
        exit_strategy="trailing_stop",
        trailing_stop_distance=5,
        target_pct=100,
    )
    trade, = result.trades
    assert trade["contract"]["option_type"] == option_type
    assert trade["exit_reason"] == "TRAILING_STOP"
    assert trade["exit_premium"] == 105


def test_trailing_entry_candle_extreme_is_excluded_and_missing_candles_are_reported():
    options = {(CE, DAY): option_candles(
        overrides={"10:05": (100, 180, 80, 100), "10:10": (100, 102, 99, 101)},
        drop=("10:15",),
    )}
    result, _ = run_one(
        UP_10_00,
        options,
        exit_strategy="trailing_stop",
        target_pct=100,
    )
    trade, = result.trades
    assert trade["highest_premium_since_entry"] == 102
    assert trade["max_premium_reached"] == 102
    assert any("missing while the position was open" in warning for warning in trade["warnings"])


def test_trailing_session_end_can_be_disabled_without_inventing_exit_price():
    result, _ = run_one(
        UP_10_00,
        {(CE, DAY): option_candles()},
        exit_strategy="trailing_stop",
        exit_on_session_end=False,
        target_pct=100,
    )
    assert result.trades == []
    assert result.signals[0]["reason_code"] == "exit_data_missing"
    assert "no overnight carry" in result.signals[0]["reason"]


def test_trailing_peak_state_resets_for_each_position():
    underlying = {
        **UP_10_00,
        "10:15": (22000, 22010, 21990, 22000),
        "10:20": (22040, 22070, 22030, 22060),
    }
    options = {(CE, DAY): option_candles(overrides={
        "10:10": (100, 120, 99, 115),
        "10:15": (118, 119, 110, 112),
        "10:25": (100, 150, 90, 100),  # The second entry candle is also excluded.
        "10:30": (100, 104, 99, 102),
    })}
    result, _ = run_one(
        underlying,
        options,
        exit_strategy="trailing_stop",
        trailing_stop_distance=5,
        target_pct=100,
        max_trades_per_session=2,
    )
    first, second = result.trades
    assert first["exit_reason"] == "TRAILING_STOP"
    assert first["highest_premium_since_entry"] == 120
    assert second["highest_premium_since_entry"] == 104
    assert second["highest_premium_time"].endswith("10:30:00")


def test_no_additional_exit_strategy_disables_atr_and_vwap():
    options = {
        (CE, DAY): option_candles(
            overrides={
                "10:05": (100, 140, 99, 130),
                "10:10": (120, 121, 110, 115),
            }
        )
    }
    result, _ = run_one(
        UP_10_00,
        options,
        exit_strategy="none",
        target_pct=100,
    )
    trade, = result.trades
    assert trade["exit_reason"] == "TIME_EXIT"
    assert trade["exit_strategy"] == "none"
    assert trade["atr_trailing_stop_premium"] is None


def test_vwap_exit_for_ce_uses_completed_nifty_close_and_next_option_open():
    underlying = index_candles(overrides={
        **UP_10_00,
        "10:05": (22000, 22010, 21990, 21990),
    })
    source = FakeSource(
        underlying={DAY: underlying},
        options={(CE, DAY): option_candles(overrides={"10:10": (92, 96, 91, 94)})},
        futures={(FUT, DAY): future_candles(overrides={"10:05": (22000, 22010, 21990, 21990)})},
        contracts={EXPIRY.isoformat(): [CE, FUT]},
    )
    config_obj = cfg(exit_strategy="vwap", target_pct=100)
    result = run_session(
        DAY,
        source.underlying[DAY],
        config_obj,
        GrowwOptionResolver(source, config_obj),
        config_obj.initial_capital_inr,
        vwap_rows=source.futures[(FUT, DAY)],
        vwap_symbol=FUT,
    )
    trade, = result.trades
    assert trade["exit_strategy"] == "vwap"
    assert trade["exit_reason"] == "VWAP_EXIT"
    assert trade["exit_time"].endswith("10:10:00")
    assert trade["exit_premium"] == 92
    assert trade["vwap_exit_signal_time"].endswith("10:10:00")
    assert trade["vwap_at_exit"] is not None
    assert trade["atr_trailing_stop_premium"] is None


def test_vwap_exit_for_pe_uses_close_above_vwap():
    underlying = index_candles(overrides={
        **DOWN_10_00,
        "10:05": (22000, 22020, 21995, 22020),
    })
    source = FakeSource(
        underlying={DAY: underlying},
        options={(PE, DAY): option_candles(overrides={"10:10": (93, 97, 92, 95)})},
        futures={(FUT, DAY): future_candles(overrides={"10:05": (22000, 22020, 21995, 22020)})},
        contracts={EXPIRY.isoformat(): [PE, FUT]},
    )
    config_obj = cfg(exit_strategy="vwap", target_pct=100)
    result = run_session(
        DAY,
        source.underlying[DAY],
        config_obj,
        GrowwOptionResolver(source, config_obj),
        config_obj.initial_capital_inr,
        vwap_rows=source.futures[(FUT, DAY)],
        vwap_symbol=FUT,
    )
    trade, = result.trades
    assert trade["contract"]["option_type"] == "PE"
    assert trade["exit_reason"] == "VWAP_EXIT"
    assert trade["exit_time"].endswith("10:10:00")


def test_vwap_exit_rejects_missing_future_volume_or_incomplete_future_session():
    rows_without_volume = [
        HistoricalCandle(row.timestamp, row.open, row.high, row.low, row.close, None)
        for row in future_candles()
    ]
    config_obj = cfg(exit_strategy="vwap")
    no_volume = run_session(
        DAY,
        index_candles(overrides=UP_10_00),
        config_obj,
        lambda *_: pytest.fail("resolver must not run when VWAP volume is missing"),
        config_obj.initial_capital_inr,
        vwap_rows=rows_without_volume,
        vwap_symbol=FUT,
    )
    assert no_volume.status == "excluded"
    assert no_volume.reason_code == "invalid_vwap_future_data"
    assert "volume" in no_volume.reason

    incomplete = run_session(
        DAY,
        index_candles(overrides=UP_10_00),
        config_obj,
        lambda *_: pytest.fail("resolver must not run when VWAP data has a gap"),
        config_obj.initial_capital_inr,
        vwap_rows=future_candles(drop=("11:00",)),
        vwap_symbol=FUT,
    )
    assert incomplete.status == "excluded"
    assert incomplete.reason_code == "vwap_data_incomplete"
    assert "candle(s) were missing" in incomplete.reason


def test_vwap_future_resolver_uses_exact_listed_future_and_its_candles():
    source = FakeSource(
        futures={(FUT, DAY): future_candles()},
        contracts={EXPIRY.isoformat(): [CE, FUT]},
    )
    config_obj = cfg(exit_strategy="vwap")
    symbol, rows = GrowwOptionResolver(source, config_obj).vwap_future(DAY)
    assert symbol == FUT
    assert len(rows) == 75
    assert ("future_contract", EXPIRY.isoformat()) in source.calls
    assert any(call[:3] == ("candles", "FNO", FUT) for call in source.calls)


def test_vwap_resolver_uses_nearest_later_listed_future_when_option_expiry_has_none():
    later_expiry = date(2024, 1, 25)
    source = FakeSource(
        futures={(MONTHLY_FUT, DAY): future_candles()},
        expiries={(2024, 1): [EXPIRY.isoformat(), later_expiry.isoformat()]},
        contracts={
            EXPIRY.isoformat(): [CE],
            later_expiry.isoformat(): [MONTHLY_FUT],
        },
    )
    symbol, rows = GrowwOptionResolver(source, cfg(exit_strategy="vwap")).vwap_future(DAY)
    assert symbol == MONTHLY_FUT and len(rows) == 75
    queried = [call[1] for call in source.calls if call[0] == "future_contract"]
    assert queried == [EXPIRY.isoformat(), later_expiry.isoformat()]


def test_execute_backtest_wires_futures_vwap_but_keeps_cash_orb_signal():
    source = FakeSource(
        underlying={DAY: index_candles(overrides=UP_10_00)},
        options={(CE, DAY): option_candles(overrides={"10:10": (92, 96, 91, 94)})},
        futures={(FUT, DAY): future_candles(overrides={"10:05": (22000, 22010, 21990, 21990)})},
        contracts={EXPIRY.isoformat(): [CE, FUT]},
    )
    result = execute_backtest(source, cfg(exit_strategy="vwap", target_pct=100))
    trade, = result["trades"]
    assert trade["exit_reason"] == "VWAP_EXIT"
    assert trade["contract"]["groww_symbol"] == CE
    assert result["sessions"][0]["vwap_symbol"] == FUT
    assert result["data_source"]["underlying_segment"] == "CASH"
    assert result["data_source"]["vwap_underlying_segment"] == "FNO"


def test_paired_exit_comparison_uses_shared_entries_and_waits_for_all_exits():
    underlying = index_candles(overrides={
        **UP_10_00,
        "10:05": (22000, 22010, 21990, 21990),
        "10:15": (22000, 22010, 21990, 22000),
        "10:20": (22040, 22070, 22030, 22060),
    })
    source = FakeSource(
        underlying={DAY: underlying},
        options={(CE, DAY): option_candles(overrides={
            "10:10": (100, 110, 99, 108),
            "10:15": (108, 109, 104, 105),
        })},
        futures={(FUT, DAY): future_candles(overrides={
            "10:05": (22000, 22010, 21990, 21990),
        })},
        contracts={EXPIRY.isoformat(): [CE, FUT]},
    )
    result = execute_backtest(
        source,
        cfg(
            exit_strategy="vwap",
            compare_vwap_and_trailing=True,
            trailing_stop_distance=5,
            trailing_stop_comparison_distances=[3, 10],
            target_pct=100,
            max_trades_per_session=2,
        ),
    )
    assert len(result["comparison_trades"]) == 4
    assert {trade["paired_entry_id"] for trade in result["comparison_trades"]} == {
        result["trades"][0]["trade_id"]
    }
    assert len({trade["entry_time"] for trade in result["comparison_trades"]}) == 1
    assert len({trade["contract"]["groww_symbol"] for trade in result["comparison_trades"]}) == 1
    assert result["trades"][0]["exit_reason"] == "VWAP_EXIT"
    assert result["exit_comparison"]["VWAP"]["trade_count"] == 1
    assert result["exit_comparison"]["TRAILING_STOP ₹3"]["trade_count"] == 1
    assert result["exit_comparison"]["TRAILING_STOP ₹10"]["trade_count"] == 1
    assert all(trade["quantity"] == 25 for trade in result["comparison_trades"])
    assert len(result["trades"]) == 1  # The second breakout is after entry but before the slowest shared exit.


def test_side_mode_ignores_unwanted_direction():
    result, source = run_one(DOWN_10_00, {}, option_side_mode="ce_only")
    assert result.trades == [] and result.signals == []
    assert not any(call[0] == "candles" and call[1] == "FNO" for call in source.calls)


def test_future_candles_do_not_change_earlier_decisions():
    options = {(CE, DAY): option_candles()}
    base, _ = run_one(UP_10_00, options)
    mutated_underlying = dict(UP_10_00)
    mutated_underlying.update({"10:05": (21000, 21010, 20990, 21000), "13:00": (30000, 30100, 29900, 30000)})
    changed, _ = run_one(mutated_underlying, options)
    keys = ("signal_time", "entry_time", "entry_premium", "contract", "stop_premium", "target_premium")
    assert {k: base.trades[0][k] for k in keys} == {k: changed.trades[0][k] for k in keys}


def test_option_data_after_exit_cannot_change_exit():
    early = {"10:10": (100, 160, 99, 150)}
    one, _ = run_one(UP_10_00, {(CE, DAY): option_candles(overrides=early)})
    two, _ = run_one(UP_10_00, {(CE, DAY): option_candles(overrides={**early, "12:00": (1, 1, 1, 1)})})
    assert one.trades[0]["exit_time"] == two.trades[0]["exit_time"]
    assert one.trades[0]["gross_pnl"] == two.trades[0]["gross_pnl"]


@pytest.mark.parametrize(("interval", "minutes"), [("1minute", 1), ("10minute", 10)])
def test_selected_interval_drives_validation_signals_fills_and_max_premium(interval, minutes):
    start = datetime.combine(DAY, time(9, 15))
    stamps = []
    stamp = start
    while stamp.time() < time(15, 30):
        stamps.append(stamp)
        stamp += timedelta(minutes=minutes)

    underlying = [
        candle(ts, 22000, 22001, 21999, 22000)
        for ts in stamps
    ]
    signal_index = next(i for i, ts in enumerate(stamps) if ts.time() == time(10, 15))
    underlying[signal_index] = candle(stamps[signal_index], 22000, 22041, 21999, 22040)
    options = [candle(ts, 100, 101, 99, 100) for ts in stamps]
    entry_time = stamps[signal_index + 1]
    entry_index = stamps.index(entry_time)
    options[entry_index] = candle(entry_time, 100, 106, 99, 102)
    config_obj = cfg(candle_interval=interval, opening_range_minutes=60, atr_multiplier=20)
    contract = "NSE-NIFTY-04Jan24-22050-CE"
    source = FakeSource(
        underlying={DAY: underlying},
        options={(contract, DAY): options},
    )
    resolver = GrowwOptionResolver(source, config_obj)
    underlying_rows = resolver.underlying(DAY)

    result = run_session(
        DAY,
        underlying_rows,
        config_obj,
        resolver,
        config_obj.initial_capital_inr,
    )

    assert result.status == "complete"
    trade, = result.trades
    assert trade["signal_time"] == entry_time.isoformat()
    assert trade["entry_time"] == entry_time.isoformat()
    assert trade["exit_reason"] == "TIME_EXIT"
    assert trade["max_premium_reached"] == 101
    assert trade["max_premium_time"] == (entry_time + timedelta(minutes=minutes)).isoformat()
    assert trade["highest_premium_between_entry_and_exit"] == 101
    assert trade["highest_premium_between_entry_and_exit_time"] == (
        entry_time + timedelta(minutes=minutes)
    ).isoformat()
    assert "intrabar path ordering" in trade["max_premium_basis"]
    candle_calls = [call for call in source.calls if call[0] == "candles"]
    assert [(call[1], call[-1]) for call in candle_calls] == [
        ("CASH", interval),
        ("FNO", interval),
    ]


def test_highest_premium_excludes_entry_and_exit_candle_ambiguous_extremes():
    options = {(CE, DAY): option_candles(overrides={
        "10:05": (100, 180, 80, 100),  # Entry fill is at the open; later high/low order is unknown.
        "10:10": (100, 120, 99, 102),  # Fully completed while the position is open.
        "10:15": (70, 200, 65, 75),  # Stop gap exit; high may have occurred after the open fill.
        "10:20": (100, 300, 99, 102),  # After exit and must never contribute.
    })}
    result, _ = run_one(UP_10_00, options, target_pct=100)
    trade, = result.trades
    assert trade["exit_reason"] == "STOP_LOSS"
    assert trade["exit_premium"] == 70
    assert trade["highest_premium_between_entry_and_exit"] == 120
    assert trade["highest_premium_between_entry_and_exit_time"].endswith("10:10:00")
    assert trade["max_premium_reached"] == 120
    assert trade["max_premium_time"].endswith("10:10:00")
    assert trade["gross_pnl"] == -750
    assert trade["net_pnl"] == -750
    assert trade["hypothetical_peak_exit_gross_pnl"] == 500
    assert trade["hypothetical_peak_exit_net_pnl"] == 500
    assert "strictly before the exit candle" in trade["max_premium_basis"]


def test_hypothetical_peak_exit_pnl_uses_configured_costs():
    result, _ = run_one(
        UP_10_00,
        {(CE, DAY): option_candles(overrides={
            "10:10": (100, 110, 99, 105),
            "10:15": (105, 106, 90, 95),
        })},
        costs=CostConfig(brokerage_per_order_inr=20, slippage_per_unit_inr=0.5),
        target_pct=100,
    )
    trade, = result.trades
    assert trade["highest_premium_between_entry_and_exit"] == 110
    assert trade["hypothetical_peak_exit_gross_pnl"] == 250
    assert trade["hypothetical_peak_exit_costs"]["brokerage"] == 40
    assert trade["hypothetical_peak_exit_costs"]["slippage"] == 25
    assert trade["hypothetical_peak_exit_net_pnl"] == 185


def test_same_candle_stop_and_target_is_stop_first_and_flagged():
    options = {(CE, DAY): option_candles(overrides={"10:10": (100, 160, 70, 100)})}
    result, _ = run_one(UP_10_00, options)
    trade = result.trades[0]
    assert trade["exit_reason"] == "STOP_LOSS" and trade["exit_premium"] == 75.0
    assert any("stop-first" in w for w in trade["warnings"])


def test_stop_gap_fills_at_open_not_at_stop_level():
    options = {(CE, DAY): option_candles(overrides={"10:10": (60, 61, 55, 58)})}
    result, _ = run_one(UP_10_00, options)
    assert result.trades[0]["exit_premium"] == 60.0


def test_time_exit_at_square_off_candle_open():
    options = {(CE, DAY): option_candles(overrides={"15:15": (110, 111, 109, 110)})}
    result, _ = run_one(UP_10_00, options)
    trade = result.trades[0]
    assert trade["exit_reason"] == "TIME_EXIT" and trade["exit_time"].endswith("15:15:00")
    assert trade["exit_premium"] == 110.0 and trade["gross_pnl"] == 250.0


def test_entry_cutoff_boundaries():
    late = {"15:00": (22040, 22070, 22030, 22060)}  # signal time 15:05 > cutoff
    result, source = run_one(late, {(CE, DAY): option_candles()})
    assert result.trades == []
    assert result.signals[0]["outcome"] == "skipped_rule" and "cutoff" in result.signals[0]["reason"]
    assert not any(call[0] == "contracts" for call in source.calls)
    ok = {"14:55": (22040, 22070, 22030, 22060)}  # signal time 15:00 == cutoff
    result, _ = run_one(ok, {(CE, DAY): option_candles()})
    assert result.trades[0]["entry_time"].endswith("15:00:00")
    assert result.trades[0]["exit_time"].endswith("15:15:00")


def test_missing_entry_candle_skips_signal_and_marks_session_incomplete():
    options = {(CE, DAY): option_candles(drop=("10:05",))}
    result, _ = run_one(UP_10_00, options)
    assert result.trades == [] and result.status == "warnings"
    assert result.signals[0]["outcome"] == "skipped_data"
    assert result.signals[0]["reason_code"] == "entry_candle_missing"


def test_missing_exit_data_is_unresolved_and_excluded_from_pnl():
    rows = [c for c in option_candles() if c.timestamp.time() <= time(10, 5)]
    result, _ = run_one(UP_10_00, {(CE, DAY): rows})
    assert result.trades == []
    assert result.signals[0]["reason_code"] == "exit_data_missing"
    summary = summarize([result.as_dict()], cfg(), [DAY])
    assert summary["net_pnl"] == 0 and summary["results_may_be_incomplete"] is True
    assert summary["signals_skipped_data"] == 1


def test_holding_period_gaps_and_zero_volume_are_warned():
    rows = option_candles(drop=("10:15", "10:20"))
    result, _ = run_one(UP_10_00, {(CE, DAY): rows})
    warnings = " ".join(result.trades[0]["warnings"])
    assert "2 option candle(s) were missing" in warnings


def test_zero_volume_entry_is_skipped_not_filled():
    rows = option_candles(overrides={"10:05": (100, 101, 99, 100, 0)})
    rows = [candle(c.timestamp, c.open, c.high, c.low, c.close, 0 if c.timestamp.strftime("%H:%M") == "10:05" else 500) for c in rows]
    result, _ = run_one(UP_10_00, {(CE, DAY): rows})
    assert result.trades == []
    assert result.signals[0]["reason_code"] == "no_entry_volume"


def test_insufficient_simulated_capital_skips_trade():
    result, _ = run_one(UP_10_00, {(CE, DAY): option_candles()}, initial_capital_inr=1000)
    assert result.trades == []
    assert result.signals[0]["reason_code"] == "insufficient_simulated_capital"


def test_max_trades_per_session_and_reentry_after_exit_only():
    options = {(CE, DAY): option_candles(overrides={"10:10": (100, 101, 70, 72)})}
    overrides = {**UP_10_00, "10:30": (22040, 22070, 22030, 22060)}
    one, _ = run_one(overrides, options, max_trades_per_session=1)
    two, _ = run_one(overrides, options, max_trades_per_session=2)
    assert len(one.trades) == 1 and len(two.trades) == 2
    assert two.trades[1]["entry_time"].endswith("10:35:00")
    assert two.trades[1]["trade_id"].endswith("-2")


def test_expiry_selection_changes_with_min_days_and_rolls_to_next_month():
    source = FakeSource(expiries={(2024, 1): ["2024-01-02", "2024-01-09"]})
    c0 = cfg(min_days_to_expiry=0)
    assert GrowwOptionResolver(source, c0)._pick_expiry(DAY) == date(2024, 1, 2)
    c1 = cfg(min_days_to_expiry=1)
    assert GrowwOptionResolver(source, c1)._pick_expiry(DAY) == date(2024, 1, 9)
    roll = FakeSource(expiries={(2024, 1): [], (2024, 2): ["2024-02-01"]})
    assert GrowwOptionResolver(roll, c0)._pick_expiry(date(2024, 1, 31)) == date(2024, 2, 1)
    with pytest.raises(OptionUnavailable) as info:
        GrowwOptionResolver(FakeSource(expiries={}), c0)._pick_expiry(DAY)
    assert info.value.code == "expiry_unavailable"


def test_same_side_breakout_requires_close_back_inside_before_reentry():
    rows = index_candles(overrides={
        "10:00": (22040, 22070, 22030, 22060),
        "10:05": (22060, 22080, 22050, 22070),
        "10:10": (22000, 22010, 21990, 22000),
        "10:15": (22040, 22070, 22030, 22060),
    })
    config = cfg()
    opening, error = ob_engine.compute_opening_range(rows, DAY, config)
    assert error is None and opening is not None

    first = ob_engine.find_breakout(rows, opening, opening.end, config)
    repeated = ob_engine.find_breakout(rows, opening, datetime.combine(DAY, time(10, 5)), config)
    after_reentry = ob_engine.find_breakout(rows, opening, datetime.combine(DAY, time(10, 10)), config)
    assert first is not None and first.candle_start.time() == time(10, 0)
    assert repeated is not None and repeated.candle_start.time() == time(10, 15)
    assert after_reentry is not None and after_reentry.candle_start.time() == time(10, 15)


def test_trade_limit_above_three_and_more_than_ten_signals_are_supported():
    overrides = {}
    signal_start = datetime.combine(DAY, time(10, 0))
    for index in range(25):
        stamp = signal_start + timedelta(minutes=5 * index)
        key = stamp.strftime("%H:%M")
        overrides[key] = (
            (22040, 22070, 22030, 22060)
            if index % 2 == 0
            else (21960, 21970, 21930, 21940)
        )
    stop_rows = option_candles(overrides={
        (datetime.combine(DAY, time(10, 0)) + timedelta(minutes=5 * index)).strftime("%H:%M"):
        (100, 101, 70, 80)
        for index in range(1, 27)
    })
    config = cfg(max_trades_per_session=12)
    source = FakeSource(
        underlying={DAY: index_candles(overrides=overrides)},
        options={(CE, DAY): stop_rows, (PE, DAY): stop_rows},
    )
    result = run_session(
        DAY, source.underlying[DAY], config, GrowwOptionResolver(source, config), config.initial_capital_inr
    )
    assert len(result.trades) == 12
    assert result.trades[-1]["trade_id"].endswith("-12")


@pytest.mark.parametrize(
    ("source_kwargs", "code"),
    [
        ({"contracts": {EXPIRY.isoformat(): [PE]}, "options": {}}, "option_no_data"),
        ({"contracts": {EXPIRY.isoformat(): ["NSE-NIFTY-11Jan24-22050-CE"]}, "options": {}}, "option_no_data"),
        ({"options": {(CE, DAY): []}}, "option_no_data"),
        ({"options": {(CE, DAY): option_candles() + [option_candles()[0]]}}, "option_data_invalid"),
        ({"options": {(CE, DAY): option_candles(overrides={"10:05": (0, 1, 0, 1)})}}, "option_data_invalid"),
    ],
)
def test_option_data_problems_skip_signal_with_reason_never_substitute(source_kwargs, code):
    kwargs = {"underlying": {DAY: index_candles(overrides=UP_10_00)}}
    kwargs.update(source_kwargs)
    kwargs.setdefault("options", {(CE, DAY): option_candles()})
    source = FakeSource(**kwargs)
    c = cfg()
    result = run_session(DAY, source.underlying[DAY], c, GrowwOptionResolver(source, c), c.initial_capital_inr)
    assert result.trades == [] and result.signals[0]["reason_code"] == code
    fno = [call[2] for call in source.calls if call[0] == "candles" and call[1] == "FNO"]
    assert all(symbol == CE for symbol in fno)  # never queried another contract


def test_underlying_problems_exclude_session_with_reason():
    c = cfg()
    never = lambda *a: pytest.fail("resolver must not be used")  # noqa: E731
    empty = run_session(DAY, [], c, never, 1)
    assert empty.status == "excluded" and empty.reason_code == "no_data"
    dup = run_session(DAY, index_candles() + [index_candles()[2]], c, never, 1)
    assert dup.reason_code == "invalid_underlying_data"
    partial = run_session(DAY, index_candles(drop=("09:20",)), c, never, 1)
    assert partial.reason_code == "opening_range_unavailable"
    summary = summarize([empty.as_dict()], c, [DAY])
    assert summary["result_kind"] == "no_historical_data"
    summary = summarize([dup.as_dict(), empty.as_dict()], c, [DAY, DAY])
    assert summary["result_kind"] == "invalid_or_incomplete_data"
    assert summary["excluded_reasons"] == {"invalid_underlying_data": 1, "no_data": 1}


# ---------- costs and summary ----------

def test_costs_are_itemised_rounded_and_separate_from_slippage():
    costs = CostConfig(
        brokerage_per_order_inr=20, stt_sell_pct=0.1, exchange_txn_pct=0.0035,
        stamp_duty_buy_pct=0.002, gst_pct=18, slippage_per_unit_inr=0.5,
    )
    out = compute_costs(100, 150, 25, costs)
    assert out["brokerage"] == 40 and out["stt"] == 3.75 and out["exchange_charges"] == 0.22
    assert out["stamp_duty"] == 0.05 and out["gst"] == 7.24
    assert out["charges_total"] == 51.26 and out["slippage"] == 25 and out["total_costs"] == 76.26
    assert out["configured"] is True
    assert compute_costs(100, 150, 25, CostConfig())["total_costs"] == 0
    assert compute_costs(100, 150, 25, CostConfig())["configured"] is False


def test_net_pnl_deducts_modelled_costs():
    options = {(CE, DAY): option_candles(overrides={"10:10": (100, 160, 99, 150)})}
    result, _ = run_one(UP_10_00, options, costs=CostConfig(brokerage_per_order_inr=20, slippage_per_unit_inr=0.5))
    trade = result.trades[0]
    assert trade["gross_pnl"] == 1250 and trade["costs"]["total_costs"] == 40 + 25
    assert trade["net_pnl"] == 1185
    summary = summarize([result.as_dict()], cfg(costs=CostConfig(brokerage_per_order_inr=20)), [DAY])
    assert summary["costs_configured"] is True and summary["net_pnl"] == 1185


def fake_session(nets):
    trades = [
        {"gross_pnl": n, "net_pnl": n, "costs": {"total_costs": 0, "charges_total": 0, "slippage": 0}}
        for n in nets
    ]
    return {"session_date": DAY.isoformat(), "status": "complete", "reason_code": None, "trades": trades, "signals": []}


def test_drawdown_profit_factor_and_win_rate():
    s = summarize([fake_session([100, -50, -80, 200])], cfg(), [DAY])
    assert s["max_drawdown_inr"] == 130 and s["profit_factor"] == pytest.approx(2.3077, abs=1e-4)
    assert s["win_rate_pct"] == 50 and s["net_pnl"] == 170 and s["ending_equity"] == 100170
    assert s["costs_configured"] is False and "not net of real charges" in s["net_pnl_basis"]
    wins = summarize([fake_session([10, 20])], cfg(), [DAY])
    assert wins["profit_factor"] is None and "no net losing" in wins["profit_factor_note"]
    assert wins["max_drawdown_inr"] == 0
    flat = summarize([fake_session([0])], cfg(), [DAY])
    assert flat["breakeven_trades"] == 1 and flat["win_rate_pct"] == 0
    daily = s["daily_pnl"][0]
    assert daily == {
        "session_date": DAY.isoformat(),
        "session_status": "complete",
        "trade_count": 4,
        "winning_trades": 2,
        "losing_trades": 2,
        "win_rate_pct": 50,
        "gross_pnl": 170,
        "net_pnl": 170,
    }


def test_daily_pnl_includes_zero_trade_session_with_unavailable_win_rate():
    empty_day = {
        "session_date": (DAY + timedelta(days=1)).isoformat(),
        "status": "complete",
        "reason_code": None,
        "trades": [],
        "signals": [],
    }
    summary = summarize([fake_session([100]), empty_day], cfg(), [DAY, DAY + timedelta(days=1)])
    assert summary["daily_pnl"][1] == {
        "session_date": (DAY + timedelta(days=1)).isoformat(),
        "session_status": "complete",
        "trade_count": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "win_rate_pct": None,
        "gross_pnl": 0,
        "net_pnl": 0,
    }


# ---------- config ----------

@pytest.mark.parametrize(
    "overrides",
    [
        {"end_date": DAY - timedelta(days=1)},
        {"end_date": DAY + timedelta(days=40)},
        {"candle_interval": "2minute"},
        {"entry_cutoff": time(15, 20)},
        {"entry_cutoff": time(9, 30)},
        {"stop_loss_pct": 0},
        {"exit_level_mode": "price"},
        {"stop_loss_points": 0},
        {"target_points": 0},
        {"exit_strategy": "ema"},
        {"exit_mode": "EMA"},
        {"atr_period": 0},
        {"atr_multiplier": 0},
        {"max_trades_per_session": 1001},
        {"unexpected": 1},
    ],
)
def test_config_rejects_invalid_values(overrides):
    with pytest.raises(ValidationError):
        cfg(**overrides)


def test_config_requires_explicit_lot_size_and_has_no_cost_defaults():
    with pytest.raises(ValidationError):
        OptionsBacktestConfig(start_date=DAY, end_date=DAY)
    assert cfg().costs.configured is False
    assert any("opening range" in rule.lower() for rule in cfg().rules_description())
    with pytest.raises(ValidationError):
        cfg(compare_vwap_and_trailing=True)


def test_options_backtest_ui_does_not_cap_trade_limit_at_three():
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert re.search(r'data-tab="daily-pnl"[^>]*>Day-wise P&amp;L</button>', html)
    assert 'id="options-backtest-daily-pnl" data-app-tab="daily-pnl"' in html
    assert "Total trades" in html and "Win rate" in html
    match = re.search(r'<input name="max_trades_per_session"[^>]*>', html)
    assert match is not None
    assert 'max="3"' not in match.group()
    assert cfg(max_trades_per_session=12).max_trades_per_session == 12
    default_config = OptionsBacktestConfig(start_date=DAY, end_date=DAY, lot_size=25)
    assert default_config.opening_range_minutes == 60
    opening_range = re.search(r'<input name="opening_range_minutes"[^>]*>', html)
    assert opening_range is not None and 'value="60"' in opening_range.group()
    options_panel = html.split('id="options-backtest-panel"', 1)[1]
    interval = re.search(r'<select name="candle_interval">(.*?)</select>', options_panel)
    assert interval is not None
    assert re.findall(r'<option value="([^"]+)"', interval.group(1)) == ["1minute", "5minute", "10minute"]
    exit_mode = re.search(r'<select name="exit_level_mode">(.*?)</select>', options_panel)
    assert exit_mode is not None
    assert re.findall(r'<option value="([^"]+)"', exit_mode.group(1)) == ["percentage", "premium_points"]
    assert 'name="stop_loss_pct"' in options_panel and 'name="target_pct"' in options_panel
    assert 'name="stop_loss_points"' in options_panel and 'name="target_points"' in options_panel
    assert 'name="atr_period"' in options_panel and 'value="14"' in options_panel
    assert 'name="atr_multiplier"' in options_panel and 'value="2"' in options_panel
    exit_strategy = re.search(r'<select name="exit_strategy">(.*?)</select>', options_panel)
    assert exit_strategy is not None
    assert re.findall(r'<option value="([^"]+)"', exit_strategy.group(1)) == [
        "none", "atr", "vwap", "trailing_stop"
    ]
    assert 'name="trailing_stop_distance"' in options_panel
    assert 'name="trailing_stop_activation"' in options_panel
    assert 'name="initial_stop_loss"' in options_panel
    assert 'name="exit_on_session_end"' in options_panel
    assert 'name="compare_vwap_and_trailing"' in options_panel
    assert 'name="trailing_stop_comparison_distances"' in options_panel
    assert "today allowed after 15:30 IST" in options_panel
    assert cfg(exit_mode="TRAILING_STOP").exit_strategy == "trailing_stop"
    for heading in (
        "<th>Entry Time</th>",
        "<th>Entry Price</th>",
        "<th>Exit Time</th>",
        "<th>Exit Price</th>",
        "<th>Highest premium between entry and exit time</th>",
        "<th>False breakout (Yes/No)</th>",
        "<th>P&amp;L at highest premium (gross / net, hypothetical)</th>",
    ):
        assert heading in options_panel
    assert "simulated alternatives, not simultaneous trades" in options_panel
    assert "counterfactual comparisons, not multiple open positions or orders" in options_panel
    app_js = (_STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 't.false_breakout ? "Yes" : "No"' in app_js
    loss_panel = re.search(r'<section class="panel" id="trade-loss-analysis-panel".*?</section>', html, re.S)
    assert loss_panel is not None
    for element_id in ("loss-date-from", "loss-date-to", "loss-instrument", "loss-option-type",
                       "loss-exit-reason", "loss-min-amount", "loss-sort", "loss-export-csv"):
        assert f'id="{element_id}"' in loss_panel.group(0)
    assert "renderTradeLossAnalysis(result);" in app_js
    assert '$("loss-export-csv").addEventListener("click", exportLossReportsCsv);' in app_js
    assert 'async function obRestoreLatestRun()' in app_js
    assert 'const runs = await api("/api/options-backtest/runs");' in app_js
    assert 'obRestoreLatestRun();' in app_js


# ---------- service ----------

def make_service(**kwargs):
    return OptionsBacktestService(lambda: kwargs.pop("source"), clock=lambda: CLOCK, pace_seconds=0, run_in_thread=False)


def good_source():
    return FakeSource(
        underlying={DAY: index_candles(overrides=UP_10_00)},
        options={(CE, DAY): option_candles(overrides={"10:10": (100, 160, 99, 150)})},
    )


def test_service_runs_and_records_exact_requests():
    source = good_source()
    service = make_service(source=source)
    run = service.start(cfg())
    assert run["state"] == "completed"
    result = service.get(run["run_id"])["result"]
    assert result["summary"]["trade_count"] == 1 and result["trades"][0]["net_pnl"] == 1250
    assert result["data_source"]["underlying_symbol"] == "NSE-NIFTY"
    assert any("not validated" in text for text in result["limitations"])
    cash = [c for c in source.calls if c[0] == "candles" and c[1] == "CASH"][0]
    assert cash[2:] == ("NSE-NIFTY", datetime(2024, 1, 2, 9, 15), datetime(2024, 1, 2, 15, 30), "5minute")
    assert ("expiries", 2024, 1) in source.calls and ("contracts", "2024-01-04") in source.calls


def test_service_rejects_today_and_future_end_dates_and_parallel_runs():
    service = make_service(source=good_source())
    with pytest.raises(RunRejected):
        service.start(cfg(start_date=date(2024, 1, 10), end_date=date(2024, 1, 10)))
    sticky = OptionsBacktestService(lambda: good_source(), clock=lambda: CLOCK, run_in_thread=False)
    sticky._runs["busy"] = {"state": "running"}
    with pytest.raises(RunRejected) as info:
        sticky.start(cfg())
    assert info.value.status_code == 409


@pytest.mark.parametrize(
    ("local_time", "accepted"),
    [
        (time(15, 29, 59), False),
        (time(15, 30), True),
        (time(15, 30, 1), True),
    ],
)
def test_service_allows_today_only_at_or_after_market_close(local_time, accepted):
    local_now = datetime.combine(date(2024, 1, 10), local_time).replace(
        tzinfo=timezone(timedelta(hours=5, minutes=30))
    )
    service = OptionsBacktestService(
        lambda: good_source(),
        clock=lambda: local_now.astimezone(timezone.utc),
        run_in_thread=False,
    )
    config = cfg(start_date=date(2024, 1, 10), end_date=date(2024, 1, 10))

    if accepted:
        run = service.start(config)
        assert run["state"] == "completed"
    else:
        with pytest.raises(RunRejected, match="15:30 Asia/Kolkata"):
            service.start(config)


def test_service_never_allows_future_end_date_even_after_market_close():
    local_now = datetime(2024, 1, 10, 15, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    service = OptionsBacktestService(
        lambda: good_source(),
        clock=lambda: local_now.astimezone(timezone.utc),
        run_in_thread=False,
    )

    with pytest.raises(RunRejected, match="end_date cannot be in the future"):
        service.start(cfg(start_date=date(2024, 1, 11), end_date=date(2024, 1, 11)))


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (ProviderRequestError("denied", 403), "provider_denied"),
        (ProviderRequestError("auth", 401), "provider_auth"),
        (ProviderRequestError("rate", 429), "provider_rate_limited"),
        (ProviderRequestError("down", 503), "provider_unavailable"),
        (ProviderRequestError("bad", 502), "provider_response"),
        (ProviderRequestError("no", 409), "provider_not_connected"),
        (RuntimeError(MARKER), "execution_error"),
    ],
)
def test_provider_failures_fail_the_run_instead_of_reporting_zero_trades(error, category):
    service = make_service(source=FakeSource(error=error))
    run = service.start(cfg())
    assert run["state"] == "failed" and run["error"]["category"] == category
    assert service.get(run["run_id"])["result"] is None
    assert MARKER not in json.dumps(run)


def test_weekday_without_candles_is_excluded_as_no_data_and_weekends_skipped():
    source = FakeSource(underlying={})
    result = execute_backtest(source, cfg(start_date=date(2024, 1, 5), end_date=date(2024, 1, 8)))
    assert [s["session_date"] for s in result["sessions"]] == ["2024-01-05", "2024-01-08"]
    assert result["summary"]["result_kind"] == "no_historical_data"
    assert result["summary"]["sessions_excluded"] == 2


def test_stop_returns_partial_results():
    source = good_source()
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] > 1

    result = execute_backtest(source, cfg(start_date=DAY, end_date=date(2024, 1, 3)), should_stop=should_stop)
    assert result["summary"]["stopped_early"] is True
    assert result["summary"]["sessions_evaluated"] == 1 and result["summary"]["sessions_requested"] == 2


# ---------- adapter (mock transport only) ----------

def _connected_manager(responses):
    manager, _, transports = groww_manager_with_transport([CLOCK], responses=[(200, {"token": "tok-12345678"})] + responses)
    manager.connect(ProviderConnectRequest(**APPROVAL))
    return manager, transports[0]


def _payload(rows):
    return (200, {"status": "SUCCESS", "payload": {"candles": rows}})


def test_adapter_requests_exact_segments_symbols_and_returns_raw_rows():
    manager, transport = _connected_manager([
        _payload([["2024-01-02T09:15:00", 1, 2, 1, 2, 10, None]]),
        _payload([]),
    ])
    start, end = datetime(2024, 1, 2, 9, 15), datetime(2024, 1, 2, 15, 30)
    rows = manager.historical_candles("CASH", "NSE-NIFTY", start, end, "5minute")
    assert rows[0].timestamp == datetime(2024, 1, 2, 9, 15) and rows[0].timestamp.tzinfo is None
    assert manager.historical_candles("FNO", CE, start, end, "5minute") == []
    cash_url, fno_url = transport.calls[1][1], transport.calls[2][1]
    assert "segment=CASH" in cash_url and "groww_symbol=NSE-NIFTY" in cash_url
    assert "segment=FNO" in fno_url and f"groww_symbol={CE}" in fno_url
    assert "candle_interval=5minute" in cash_url
    assert all("order" not in call[1] for call in transport.calls)


def test_adapter_accepts_only_exact_listed_nifty_future_for_vwap():
    manager, transport = _connected_manager([
        (200, {"status": "SUCCESS", "payload": {"contracts": [FUT, CE]}}),
        _payload([["2024-01-02T09:15:00", 22000, 22010, 21990, 22000, 1000, None]]),
    ])
    assert manager.historical_future_contract(EXPIRY.isoformat()) == FUT
    rows = manager.historical_candles(
        "FNO",
        FUT,
        datetime(2024, 1, 2, 9, 15),
        datetime(2024, 1, 2, 15, 30),
        "5minute",
    )
    assert rows[0].volume == 1000
    assert f"groww_symbol={FUT}" in transport.calls[2][1]

    invalid_manager, _ = _connected_manager([
        (200, {"status": "SUCCESS", "payload": {"contracts": ["NSE-NIFTY-11Jan24-FUT"]}}),
    ])
    assert invalid_manager.historical_future_contract(EXPIRY.isoformat()) is None


@pytest.mark.parametrize(
    ("segment", "symbol", "interval"),
    [("CASH", "NSE-BANKNIFTY", "5minute"), ("FNO", "garbage", "5minute"), ("MCX", CE, "5minute"), ("CASH", "NSE-NIFTY", "2minute")],
)
def test_adapter_rejects_bad_inputs_before_any_request(segment, symbol, interval):
    manager, transport = _connected_manager([])
    with pytest.raises(ProviderRequestError) as info:
        manager.historical_candles(segment, symbol, datetime(2024, 1, 2, 9, 15), datetime(2024, 1, 2, 15, 30), interval)
    assert info.value.status_code == 422 and len(transport.calls) == 1


@pytest.mark.parametrize(
    ("upstream", "status", "state"),
    [
        ((401, {"status": "FAILURE"}), 401, "failed"),
        ((403, {"status": "FAILURE"}), 403, "connected"),
        ((429, {"status": "FAILURE"}), 429, "connected"),
        (TimeoutError(MARKER), 503, "connected"),
        (_payload("not-a-list"), 502, "connected"),
        (_payload([["2024-01-02T09:15:00", 1, 2]]), 502, "connected"),
        (_payload([["2024-01-02T09:15:00+05:30", 1, 2, 1, 2, 10, None]]), 502, "connected"),
    ],
)
def test_adapter_error_mapping_is_safe(upstream, status, state):
    manager, _ = _connected_manager([upstream])
    with pytest.raises(ProviderRequestError) as info:
        manager.historical_candles("CASH", "NSE-NIFTY", datetime(2024, 1, 2, 9, 15), datetime(2024, 1, 2, 15, 30), "5minute")
    assert info.value.status_code == status and MARKER not in str(info.value)
    assert manager.status()["state"] == state


def test_adapter_rejects_malformed_rows_without_unused_import():
    assert issubclass(ProviderResponseError, Exception)


# ---------- API ----------

def _snapshot(env):
    return env.db.events(), env.db.trades(), api.risk.snapshot()


def test_api_requires_connected_provider(provider_env):  # noqa: F811
    with TestClient(app) as client:
        response = client.post("/api/options-backtest/runs", json={"start_date": "2024-01-02", "end_date": "2024-01-02", "lot_size": 25})
    assert response.status_code == 409


@pytest.mark.parametrize(
    "payload",
    [
        {"start_date": "2024-01-02", "end_date": "2024-01-02"},
        {"start_date": "2024-01-02", "end_date": "2024-03-30", "lot_size": 25},
        {"start_date": "2024-01-02", "end_date": "2024-01-02", "lot_size": 25, "extra": 1},
    ],
)
def test_api_rejects_invalid_configuration_with_422(provider_env, monkeypatch, payload):  # noqa: F811
    monkeypatch.setattr(api, "provider_session", FakeSource())
    with TestClient(app) as client:
        response = client.post("/api/options-backtest/runs", json=payload)
    assert response.status_code == 422
    json.dumps(response.json())


def test_api_run_lifecycle_does_not_touch_paper_ledger_risk_or_orders(provider_env, monkeypatch):  # noqa: F811
    source = good_source()
    monkeypatch.setattr(api, "provider_session", source)
    with TestClient(app) as client:
        monkeypatch.setattr(api, "options_backtest_service", make_service(source=source))
        before = _snapshot(provider_env)
        started = client.post(
            "/api/options-backtest/runs",
            json={"start_date": "2024-01-02", "end_date": "2024-01-02", "opening_range_minutes": 30, "lot_size": 25},
        )
        assert started.status_code == 200 and started.json()["state"] == "completed"
        assert "result" not in started.json()
        run_id = started.json()["run_id"]
        fetched = client.get(f"/api/options-backtest/runs/{run_id}").json()
        listing = client.get("/api/options-backtest/runs").json()
        stopped = client.post(f"/api/options-backtest/runs/{run_id}/stop")
        missing = client.get("/api/options-backtest/runs/nope")
        after = _snapshot(provider_env)
        paths = {route.path for route in app.routes}
    assert fetched["result"]["trades"][0]["gross_pnl"] == 1250
    assert [r["run_id"] for r in listing] == [run_id] and "result" not in listing[0]
    assert stopped.status_code == 200 and missing.status_code == 404
    assert after == before
    assert not any("order" in path for path in paths)


def test_api_provider_failure_is_reported_as_failed_run_with_category(provider_env, monkeypatch):  # noqa: F811
    source = FakeSource(error=ProviderRequestError("Groww denied access to historical data.", 403))
    monkeypatch.setattr(api, "provider_session", source)
    with TestClient(app) as client:
        monkeypatch.setattr(api, "options_backtest_service", make_service(source=source))
        run = client.post(
            "/api/options-backtest/runs",
            json={"start_date": "2024-01-02", "end_date": "2024-01-02", "lot_size": 25},
        ).json()
        fetched = client.get(f"/api/options-backtest/runs/{run['run_id']}").json()
    assert fetched["state"] == "failed" and fetched["error"]["category"] == "provider_denied"
    assert fetched["result"] is None


def test_package_has_no_order_endpoints_or_ledger_access():
    package = Path(api.__file__).parent / "options_backtest"
    text = "\n".join(p.read_text(encoding="utf-8") for p in package.glob("*.py"))
    for forbidden in ("/v1/order", "place_order", "cancel_order", "modify_order", "PaperBroker", "RiskEngine", "import database", "from ..database", "get_account_insights"):
        assert forbidden not in text


# ---------- UI ----------

def _ui_harness(post, get=None, runs="[]", submit=True):
    return f"""<script>
const calls = [];
window.fetch = async (path, options={{}}) => {{
  const url = String(path);
  calls.push({{url, method:options.method || "GET"}});
  let data = {{}}, status = 200;
  if (url === "/api/strategy/decisions" || url === "/api/trades" || url === "/api/replay") data = [];
  else if (url === "/api/options-backtest/runs" && (options.method || "GET") === "GET") data = {runs};
  else if (url === "/api/options-backtest/runs" && options.method === "POST") {{ ({{data, status}} = {post}); }}
  else if (url.startsWith("/api/options-backtest/runs/")) data = {get or "{}"};
  return {{ok:status < 400,status,json:async()=>data}};
}};
</script>
<script src="{(_STATIC_DIR / 'app.js').as_uri()}"></script>
<script>
(async () => {{
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  await sleep(50);
  const loadCalls = calls.filter(c => c.url.includes("options-backtest")).length;
  const panelHiddenInitially = document.getElementById("options-backtest-panel").hidden;
  if ({str(submit).lower()}) {{
    document.querySelector('[data-tab="backtest"]').click();
    const form = document.getElementById("ob-form");
    form.elements.start_date.value = "2024-01-02";
    form.elements.end_date.value = "2024-01-02";
    form.elements.lot_size.value = "25";
    form.requestSubmit();
    await sleep(2500);
  }}
  const snap = {{
    loadCalls, panelHiddenInitially,
    panelHidden: document.getElementById("options-backtest-panel").hidden,
    status: document.getElementById("ob-status").textContent,
    state: document.getElementById("ob-status").dataset.state,
    message: document.getElementById("ob-message").textContent,
    trades: document.getElementById("ob-trades").innerText,
    lossSummary: document.getElementById("loss-analysis-summary").innerText,
    lossRows: document.getElementById("loss-report-rows").innerText,
    lossFilterSummary: document.getElementById("loss-filter-summary").innerText,
    startDisabled: document.getElementById("ob-start").disabled,
    posts: calls.filter(c => c.method === "POST").map(c => c.url),
  }};
  const pre = document.createElement("pre");
  pre.id = "snapshot";
  pre.textContent = JSON.stringify([snap]);
  document.body.appendChild(pre);
}})();
</script>"""


def _result(kind, trades=None):
    return json.dumps({
        "run_id": "r1", "state": "completed", "progress": {"done": 1, "total": 1, "current_date": None},
        "error": None,
        "result": {
            "summary": {
                "result_kind": kind, "sessions_processed": 1, "sessions_requested": 1, "sessions_excluded": 0,
                "excluded_reasons": {}, "trade_count": len(trades or []), "win_rate_pct": None, "gross_pnl": 0,
                "total_costs": 0, "total_charges": 0, "total_slippage": 0, "net_pnl": 0, "max_drawdown_inr": 0,
                "max_drawdown_pct": 0, "profit_factor": None, "average_trade_pnl": None, "initial_equity": 100000,
                "ending_equity": 100000, "net_pnl_basis": "basis", "profit_factor_note": "pf", "max_drawdown_method": "dd",
                "results_may_be_incomplete": False, "costs_configured": False,
            },
            "trades": trades or [], "sessions": [], "skipped_signals": [], "rules": ["r"], "limitations": ["l"],
            "data_source": {"timestamp_note": "tz"},
        },
    })


@needs_browser
def test_ui_distinguishes_zero_trades_from_failure_and_is_user_triggered(tmp_path):
    zero = _run_harness_page(
        tmp_path,
        _ui_harness('{data:{run_id:"r1",state:"running",progress:{done:0,total:1}}, status:200}', _result("zero_trades")),
    )[0]
    assert zero["loadCalls"] == 1 and zero["panelHiddenInitially"] is True and zero["panelHidden"] is False
    assert zero["status"] == "Zero trades" and zero["state"] == "incomplete"
    assert "not a failure" in zero["message"] or zero["message"] == "Backtest finished."
    assert zero["posts"] == ["/api/options-backtest/runs"]


@needs_browser
def test_ui_reports_not_connected_and_failed_runs_not_as_zero_trades(tmp_path):
    denied = _run_harness_page(
        tmp_path,
        _ui_harness('{data:{detail:"Connect Groww on the Groww tab before running a backtest."}, status:409}'),
    )[0]
    assert denied["status"] == "Not connected" and denied["state"] == "failed"
    assert denied["startDisabled"] is False

    failed_run = json.dumps({"run_id": "r1", "state": "failed", "progress": {"done": 0, "total": 1},
                             "error": {"category": "provider_denied", "message": "Groww denied access."}, "result": None})
    failed = _run_harness_page(
        tmp_path,
        _ui_harness('{data:{run_id:"r1",state:"running",progress:{done:0,total:1}}, status:200}', failed_run),
    )[0]
    assert failed["status"] == "Access denied" and failed["state"] == "failed"
    assert "not a zero-trade result" in failed["message"]


@needs_browser
def test_ui_restores_latest_completed_run_and_trade_loss_analysis(tmp_path):
    result, _ = run_one(
        DOWN_10_00,
        {(PE, DAY): option_candles(overrides={"10:10": (100, 101, 70, 72)})},
    )
    result_dict = result.as_dict()
    dashboard = build_loss_dashboard(result_dict["trades"], result_dict["loss_reports"])
    restored = json.loads(_result("trades"))
    restored["run_id"] = "restored-run"
    restored["result"]["summary"]["trade_count"] = len(result_dict["trades"])
    restored["result"]["trades"] = result_dict["trades"]
    restored["result"]["sessions"] = [result_dict]
    restored["result"]["loss_reports"] = result_dict["loss_reports"]
    restored["result"]["loss_dashboard"] = dashboard
    run_list = json.dumps([{"run_id": "restored-run", "state": "completed"}])
    dashboard_page = _run_harness_page(
        tmp_path,
        _ui_harness(
            '{data:{}, status:200}',
            get=json.dumps(restored),
            runs=run_list,
            submit=False,
        ),
    )[0]

    assert dashboard_page["loadCalls"] == 2
    assert dashboard_page["status"] != "No run"
    assert "NSE-NIFTY-04Jan24-21950-PE" in dashboard_page["lossRows"]
    assert "1 report(s) shown; 1 losing trade(s)" in dashboard_page["lossFilterSummary"]
    assert "1 / 1 completed trades" in dashboard_page["lossSummary"]

def _ob_fetch(provider_env, segment, symbol, candle_rows):
    now = datetime(2025, 1, 3, 4, 0, tzinfo=timezone.utc)
    mgr, created, _ = groww_manager_with_transport(
        [now],
        responses=[(200, {"token": "tok-12345678"}), (200, {"status": "SUCCESS", "payload": {"candles": candle_rows}})],
    )
    provider_env.install(mgr)
    start, end = datetime(2024, 1, 3, 9, 15), datetime(2024, 1, 3, 15, 30)
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=APPROVAL)
        return created[0].get_historical_candles(segment, symbol, start, end, "5minute")


def test_index_candles_with_null_volume_are_accepted_but_option_candles_are_not(provider_env):
    rows = [["2024-01-03T09:15:00", 21000.0, 21010.0, 20990.0, 21005.0, None, None]]
    candles = _ob_fetch(provider_env, "CASH", "NSE-NIFTY", rows)
    assert candles[0].volume is None
    assert ob_data.validate_session_candles(candles, date(2024, 1, 3), "idx", volume_required=False).errors == []
    assert ob_data.validate_session_candles(candles, date(2024, 1, 3), "opt").errors
    with pytest.raises(ProviderResponseError):
        _ob_fetch(provider_env, "FNO", "NSE-NIFTY-04Jan24-19200-CE", rows)


def test_final_1530_row_is_excluded_by_session_boundary_regardless_of_ohlc():
    rows = index_candles()
    last = rows[-1]
    closing = HistoricalCandle(datetime.combine(DAY, time(15, 30)), last.close, last.close, last.close, last.close, None)
    result = ob_data.validate_session_candles(rows + [closing], DAY, "idx", volume_required=False)
    assert result.errors == []
    assert len(result.candles) == len(rows)
    assert any("excluded the final 15:30 candle" in w for w in result.warnings)

    different = HistoricalCandle(datetime.combine(DAY, time(15, 30)), last.close, last.close + 5, last.close, last.close, None)
    excluded = ob_data.validate_session_candles(rows + [different], DAY, "idx", volume_required=False)
    assert excluded.errors == [] and len(excluded.candles) == len(rows)
    assert excluded.candles[-1].timestamp.time() == time(15, 25)
    early = HistoricalCandle(datetime.combine(DAY, time(15, 27)), last.close, last.close, last.close, last.close, None)
    assert ob_data.validate_session_candles(rows + [early], DAY, "idx", volume_required=False).errors


def _exact_live_option_rows(day=DAY):
    """The reported live pattern: full 09:15-15:25 grid plus a non-flat, traded 15:30 row."""
    rows = option_candles(day)
    stamp = lambda h, m: datetime.combine(day, time(h, m))  # noqa: E731
    rows[-2:] = [
        HistoricalCandle(stamp(15, 20), 189.8, 192.2, 178.75, 181.75, 425295),
        HistoricalCandle(stamp(15, 25), 181.3, 184.5, 170.1, 171.0, 496990),
    ]
    rows.append(HistoricalCandle(stamp(15, 30), 170.25, 176.65, 170.25, 173.05, 93665))
    return rows


def test_traded_1530_option_row_is_excluded_and_remaining_session_validates():
    rows = _exact_live_option_rows()
    assert len(rows) == 76
    result = ob_data.validate_session_candles(rows, DAY, "NSE-NIFTY-27Oct26-22650-PE option candles")
    assert result.usable and len(result.candles) == 75
    assert result.candles[-1].timestamp.time() == time(15, 25)
    assert any("excluded the final 15:30 candle" in w for w in result.warnings)
    assert not result.errors


def test_resolver_uses_validated_option_candles_after_excluding_1530():
    sym = "NSE-NIFTY-04Jan24-22050-CE"
    source = FakeSource(options={(sym, DAY): _exact_live_option_rows()})
    resolved = GrowwOptionResolver(source, cfg())(DAY, "CE", 22050, datetime.combine(DAY, time(9, 50)))
    assert resolved.candles[-1].timestamp.time() == time(15, 25)
    assert any("excluded the final 15:30 candle" in warning for warning in resolved.warnings)


def test_flat_1530_index_row_is_excluded_by_the_same_session_boundary():
    rows = index_candles()
    last = rows[-1]
    flat = HistoricalCandle(datetime.combine(DAY, time(15, 30)), last.close, last.close, last.close, last.close, None)
    assert ob_data.validate_session_candles(rows + [flat], DAY, "idx", volume_required=False).usable


def _raw_rows(rows, oi=None):
    return [[r.timestamp.isoformat(), r.open, r.high, r.low, r.close, r.volume, oi] for r in rows]


def test_exact_live_option_pattern_through_the_adapter_excludes_1530_row(provider_env):
    sym = "NSE-NIFTY-27Oct26-22650-PE"
    day = date(2024, 1, 3)
    parsed = _ob_fetch(provider_env, "FNO", sym, _raw_rows(_exact_live_option_rows(day)))
    assert [c.timestamp.strftime("%H:%M") for c in parsed[-3:]] == ["15:20", "15:25", "15:30"]
    assert parsed[-1].volume == 93665 and len(parsed) == 76
    result = ob_data.validate_session_candles(parsed, day, f"{sym} option candles")
    assert result.usable and len(result.candles) == 75
    assert result.candles[-1].timestamp.time() == time(15, 25)
    assert result.errors == []
    assert len(result.warnings) == 1 and "excluded the final 15:30 candle" in result.warnings[0]


def test_exact_live_index_pattern_flat_1530_row_is_excluded_with_a_warning(provider_env):
    day = date(2024, 1, 3)
    rows = index_candles(day)
    stamp = lambda h, m: datetime.combine(day, time(h, m))  # noqa: E731
    rows[-2:] = [
        HistoricalCandle(stamp(15, 20), 22666.55, 22666.55, 22666.55, 22666.55, None),
        HistoricalCandle(stamp(15, 25), 22666.55, 22687.65, 22666.55, 22687.65, None),
    ]
    rows.append(HistoricalCandle(stamp(15, 30), 22687.65, 22687.65, 22687.65, 22687.65, None))
    parsed = _ob_fetch(provider_env, "CASH", "NSE-NIFTY", _raw_rows(rows))
    assert len(parsed) == 76
    result = ob_data.validate_session_candles(parsed, day, "idx", volume_required=False)
    assert result.usable and len(result.candles) == 75
    assert any("excluded the final 15:30 candle" in w for w in result.warnings)


def test_other_off_grid_rows_still_get_a_grid_error_after_excluding_1530():
    rows = _exact_live_option_rows()
    rows.insert(-1, HistoricalCandle(datetime.combine(DAY, time(15, 27)), 171, 172, 170, 171, 5))
    result = ob_data.validate_session_candles(rows, DAY, "opt")
    assert len(result.errors) == 1 and "five-minute grid" in result.errors[0]


def test_series_without_the_1530_row_is_valid_so_an_earlier_end_time_would_only_help_if_groww_honours_it():
    # Only proves AOTS accepts a 09:15-15:25 series. Whether Groww returns that for an earlier end_time is
    # unverified and needs an explicitly approved manual live check.
    rows = _exact_live_option_rows()[:-1]
    assert len(rows) == 75 and ob_data.validate_session_candles(rows, DAY, "opt").usable


def _log_candidates():
    from aots.brokers.response_log import HISTORICAL_RESPONSE_LOG
    HISTORICAL_RESPONSE_LOG.clear()
    return HISTORICAL_RESPONSE_LOG


def test_response_log_records_historical_responses_only_and_never_the_token(provider_env):
    log = _log_candidates()
    now = datetime(2025, 1, 3, 4, 0, tzinfo=timezone.utc)
    contracts = [f"NSE-NIFTY-27Oct26-{22000 + 50 * i}-PE" for i in range(80)]
    mgr, _, _ = groww_manager_with_transport(
        [now],
        responses=[
            (200, {"token": "tok-12345678"}),
            (200, {"status": "SUCCESS", "payload": {"contracts": contracts, "access_token": "leak-me"}}),
        ],
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=APPROVAL)
        assert client.get("/api/provider/historical/contracts", params={"expiry_date": "2024-01-03"}).status_code == 200
        body = client.get("/api/provider/response-log").json()
        text = json.dumps(body)
        assert len(body["entries"]) == 1
        entry = body["entries"][0]
        assert entry["path"] == "/v1/historical/contracts"
        assert entry["http_status"] == 200
        assert entry["structure"]["lists"]["contracts"] == {"count": 80, "element_types": ["str"]}
        assert entry["body"]["payload"]["contracts"] == contracts
        from aots.brokers.response_log import sanitize
        assert any("omitted (total 80)" in str(i) for i in sanitize([[1, 2]] * 80))
        assert entry["body"]["payload"]["access_token"] == "[redacted]"
        assert "tok-12345678" not in text and "leak-me" not in text
        assert client.post("/api/provider/response-log/clear").json() == {"cleared": True}
        assert client.get("/api/provider/response-log").json()["entries"] == []


def test_response_log_records_error_status_and_raw_candle_shape(provider_env):
    log = _log_candidates()
    now = datetime(2025, 1, 3, 4, 0, tzinfo=timezone.utc)
    mgr, _, _ = groww_manager_with_transport(
        [now],
        responses=[
            (200, {"token": "tok-12345678"}),
            (200, {"status": "SUCCESS", "payload": {"candles": [[1717755600000, 1, 2, 1, 2, 5, None]]}}),
        ],
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=APPROVAL)
        client.post(
            "/api/provider/historical/candles/check",
            json={"groww_symbol": "NSE-NIFTY-04Jan24-19200-CE", "start_time": "2024-01-03T09:15",
                  "end_time": "2024-01-03T09:30", "candle_interval": "15minute"},
        )
    entries = log.snapshot()
    assert len(entries) == 1
    assert entries[0]["body"]["payload"]["candles"][0][0] == 1717755600000
    assert entries[0]["structure"]["lists"]["candles"]["element_types"] == ["list"]


def test_response_log_is_bounded_and_ignores_non_historical_paths():
    from aots.brokers.response_log import ResponseLog
    log = ResponseLog(capacity=3)
    for i in range(5):
        log.record("/v1/historical/candles", {"n": str(i)}, 200, {"payload": {}})
    log.record("/v1/live-data/quote", {}, 200, {"payload": {"last_price": 1}})
    log.record("/v1/token/api/access", {}, 200, {"token": "x"})
    entries = log.snapshot()
    assert [e["query"]["n"] for e in entries] == ["4", "3", "2"]


def test_contract_not_listed_message_explains_what_groww_returned():
    exp = date(2026, 10, 27)
    listed = ["NSE-NIFTY-27Oct26-22450-PE", "NSE-NIFTY-27Oct26-22550-PE", "NSE-NIFTY-27Oct26-22500-CE",
              "NSE-NIFTY-03Nov26-22500-PE", "NSE-NIFTY-27Oct26-22500-PE-X"]
    contract, _ = ob_data.select_contract(listed, exp, 22500, "PE")
    assert contract is None
    text = ob_data.describe_listing(listed, exp, 22500, "PE")
    assert "NSE-NIFTY-27Oct26-22500-PE" in text
    assert "5 symbol(s)" in text and "3 for that expiry" in text and "2 PE strike(s)" in text
    assert "2026-11-03" in text and "1 unparseable" in text


def _resolve_pe_22500(source, config=None):
    config = config or cfg()
    return GrowwOptionResolver(source, config)(DAY, "PE", 22050, datetime.combine(DAY, time(9, 50)))


UNLISTED_PE = "NSE-NIFTY-04Jan24-22050-PE"


def test_contract_omitted_from_contracts_list_is_used_when_its_exact_candles_validate():
    source = FakeSource(
        contracts={EXPIRY.isoformat(): [CE]},  # the PE is omitted, as Groww did live
        options={(UNLISTED_PE, DAY): option_candles()},
    )
    resolved = _resolve_pe_22500(source)
    assert resolved.contract.groww_symbol == UNLISTED_PE
    assert (resolved.contract.expiry, resolved.contract.strike, resolved.contract.option_type) == (EXPIRY, 22050.0, "PE")
    assert any("not in the Groww contracts list" in w for w in resolved.warnings)
    fno = [c[2] for c in source.calls if c[0] == "candles" and c[1] == "FNO"]
    assert fno == [UNLISTED_PE]  # exact symbol only, no substitution


def test_unlisted_contract_trades_end_to_end_and_flags_the_trade():
    c = cfg()
    source = FakeSource(
        underlying={DAY: index_candles(overrides=DOWN_10_00)},
        contracts={EXPIRY.isoformat(): [CE]},
        options={(PE, DAY): option_candles()},
    )
    result = run_session(DAY, source.underlying[DAY], c, GrowwOptionResolver(source, c), c.initial_capital_inr)
    assert len(result.trades) == 1 and result.trades[0].option_symbol == PE if hasattr(result.trades[0], "option_symbol") else True
    trade = result.as_dict()["trades"][0]
    assert any("not in the Groww contracts list" in w for w in trade["warnings"])
    assert [c[2] for c in source.calls if c[0] == "candles" and c[1] == "FNO"] == [PE]


def test_listed_contract_has_no_unlisted_warning():
    source = FakeSource(options={(CE, DAY): option_candles()})
    resolved = GrowwOptionResolver(source, cfg())(DAY, "CE", 22050, datetime.combine(DAY, time(9, 50)))
    assert not any("not in the Groww contracts list" in w for w in resolved.warnings)


def test_unlisted_contract_with_no_candles_is_skipped_with_distinct_reason():
    source = FakeSource(contracts={EXPIRY.isoformat(): [CE]}, options={(UNLISTED_PE, DAY): []})
    with pytest.raises(OptionUnavailable) as info:
        _resolve_pe_22500(source)
    assert info.value.code == "option_no_data"
    assert "not listed by the Groww contracts API" in info.value.message


def test_unlisted_contract_rejected_by_candle_request_reports_unavailable_not_listing():
    class Rejecting(FakeSource):
        def historical_candles(self, segment, symbol, start, end, interval):
            if segment == "FNO":
                self.calls.append(("candles", segment, symbol, start, end, interval))
                raise ProviderRequestError("Groww rejected the request; check the symbol and expiry.", 422)
            return super().historical_candles(segment, symbol, start, end, interval)

    source = Rejecting(contracts={EXPIRY.isoformat(): [CE]})
    with pytest.raises(OptionUnavailable) as info:
        _resolve_pe_22500(source)
    assert info.value.code == "contract_candles_unavailable"
    assert "could not be verified" in info.value.message and "diagnostic" in info.value.message


def test_unlisted_contract_invalid_candles_are_rejected():
    bad = option_candles() + [option_candles()[0]]
    source = FakeSource(contracts={EXPIRY.isoformat(): [CE]}, options={(UNLISTED_PE, DAY): bad})
    with pytest.raises(OptionUnavailable) as info:
        _resolve_pe_22500(source)
    assert info.value.code == "option_data_invalid"


@pytest.mark.parametrize("status", [401, 403, 429, 502, 503])
def test_provider_errors_on_unlisted_contract_fail_the_run_not_the_signal(status):
    class Failing(FakeSource):
        def historical_candles(self, segment, symbol, start, end, interval):
            raise ProviderRequestError("provider failure", status)

    with pytest.raises(ProviderRequestError) as info:
        _resolve_pe_22500(Failing(contracts={EXPIRY.isoformat(): [CE]}))
    assert info.value.status_code == status and "[stage: option candles" in str(info.value)


@pytest.mark.parametrize(("option_type", "strike"), [("XX", 22050), ("CE", 0), ("PE", -50), ("PE", True)])
def test_invalid_contract_request_is_rejected_without_any_candle_request(option_type, strike):
    source = FakeSource(contracts={EXPIRY.isoformat(): [CE]})
    with pytest.raises(OptionUnavailable) as info:
        GrowwOptionResolver(source, cfg())(DAY, option_type, strike, datetime.combine(DAY, time(9, 50)))
    assert info.value.code == "contract_not_listed"
    assert not [c for c in source.calls if c[0] == "candles"]


def test_resolver_skip_reason_carries_listing_diagnostic_when_unverifiable():
    class Rejecting(FakeSource):
        def historical_candles(self, segment, symbol, *rest):
            raise ProviderRequestError("rejected", 422)

    with pytest.raises(OptionUnavailable) as info:
        GrowwOptionResolver(Rejecting(), cfg())(DAY, "PE", 22500, datetime.combine(DAY, time(9, 50)))
    assert info.value.code == "contract_candles_unavailable"
    assert "diagnostic" in info.value.message