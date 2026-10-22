from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest

import aots.backtest.__main__ as backtest_cli
import aots.backtest.data as backtest_data
from aots.backtest.data import (
    BacktestConfig,
    Bar,
    DataAcquisitionError,
    DataValidationError,
    KOLKATA,
    load_or_download,
    finalize_cache_validation,
    normalize_frame,
    validate_bars,
    validate_or_raise,
)
from aots.backtest.engine import (
    BacktestRunner,
    UnsupportedFilterError,
    _atr_values,
    _breakout_events,
    _ema_values,
    _research_validation,
    calculate_metrics,
    run_backtest,
)
from aots.backtest.report import write_failure_report, write_results


FIXED_NOW = datetime(2026, 10, 21, 12, 0, tzinfo=KOLKATA)
SESSION_DATE = date(2026, 10, 20)


def make_config(tmp_path, **kwargs):
    start = kwargs.pop("start", date(2026, 10, 19))
    end = kwargs.pop("end", date(2026, 10, 22))
    return BacktestConfig(
        start=start,
        end=end,
        data_dir=tmp_path / "cache",
        output_dir=tmp_path / "reports",
        **kwargs,
    )


def make_session(session_date=SESSION_DATE, *, breakout="LONG", exit_kind="target"):
    bars = []
    start = datetime.combine(session_date, time(9, 15), tzinfo=KOLKATA)
    for index in range(75):
        stamp = start + timedelta(minutes=index * 5)
        open_price = 100.0
        high = 101.0
        low = 99.0
        close = 100.0
        if index == 6:
            if breakout == "LONG":
                open_price, high, low, close = 100.5, 102.5, 100.0, 102.0
            elif breakout == "SHORT":
                open_price, high, low, close = 99.5, 100.0, 97.5, 98.0
        elif index == 7:
            if breakout == "LONG":
                open_price, high, low, close = 101.5, 102.5, 100.5, 102.0
            elif breakout == "SHORT":
                open_price, high, low, close = 98.5, 99.5, 97.5, 98.0
        elif index == 8 and exit_kind == "target":
            if breakout == "LONG":
                open_price, high, low, close = 102.0, 107.0, 101.0, 106.0
            else:
                open_price, high, low, close = 98.0, 99.0, 93.0, 94.0
        elif index == 8 and exit_kind == "both":
            if breakout == "LONG":
                open_price, high, low, close = 102.0, 107.0, 98.0, 103.0
            else:
                open_price, high, low, close = 98.0, 102.0, 93.0, 97.0
        elif index == 8 and exit_kind == "quiet":
            if breakout == "LONG":
                open_price, high, low, close = 102.0, 102.5, 100.0, 102.0
            else:
                open_price, high, low, close = 98.0, 99.0, 97.5, 98.0
        elif index >= 9:
            if exit_kind == "quiet":
                open_price, high, low, close = 100.0, 100.5, 99.5, 100.0
            elif index == 72:
                open_price, high, low, close = 100.0, 100.5, 99.5, 100.0
        bars.append(Bar(stamp, open_price, high, low, close, None))
    return bars


def validated(bars, tmp_path, **config_options):
    config = make_config(tmp_path, **config_options)
    result = validate_or_raise(
        bars,
        config,
        {"source": "test fixture", "retrieved_at_utc": FIXED_NOW.isoformat()},
    )
    return config, result


def create_cached(config, *, retrieved_at=FIXED_NOW, frame=None):
    if frame is None:
        frame = frame_from_bars(make_session())
    bars, metadata = load_or_download(
        config,
        downloader=lambda *_args, **_kwargs: frame,
        clock=lambda: retrieved_at,
        sleep=lambda _: None,
    )
    quality = validate_bars(bars, config, metadata).quality
    finalize_cache_validation(config, quality)
    return bars, metadata


def frame_from_bars(bars):
    index = pd.DatetimeIndex([bar.timestamp for bar in bars])
    return pd.DataFrame(
        {
            "Open": [bar.open for bar in bars],
            "High": [bar.high for bar in bars],
            "Low": [bar.low for bar in bars],
            "Close": [bar.close for bar in bars],
            "Volume": [bar.volume for bar in bars],
        },
        index=index,
    )


def test_normalize_timezone_aware_and_naive_index():
    aware = pd.DataFrame(
        {"Open": [100], "High": [101], "Low": [99], "Close": [100]},
        index=pd.DatetimeIndex([datetime(2026, 10, 20, 9, 15, tzinfo=KOLKATA)]),
    )
    bars, convention = normalize_frame(aware)
    assert bars[0].timestamp == datetime(2026, 10, 20, 9, 15, tzinfo=KOLKATA)
    assert "timezone-aware converted" in convention

    naive = aware.copy()
    naive.index = naive.index.tz_localize(None)
    bars, convention = normalize_frame(naive)
    assert bars[0].timestamp == datetime(2026, 10, 20, 9, 15, tzinfo=KOLKATA)
    assert "naive interpreted in Asia/Kolkata" in convention


def test_download_success_persists_provenance_and_offline_cache(tmp_path):
    config = make_config(tmp_path)
    bars = make_session()
    frame = frame_from_bars(bars)
    calls = []

    def downloader(*args, **kwargs):
        calls.append((args, kwargs))
        return frame

    downloaded, metadata = load_or_download(
        config,
        downloader=downloader,
        clock=lambda: FIXED_NOW,
        sleep=lambda _: pytest.fail("successful download must not retry"),
    )
    assert len(downloaded) == 75
    assert calls[0][0] == ("^NSEI",)
    assert calls[0][1]["interval"] == "5m"
    assert calls[0][1]["timeout"] == config.timeout_seconds
    assert calls[0][1]["auto_adjust"] is False
    assert metadata["retrieval_mode"] == "downloaded now"
    assert metadata["row_count"] == 75
    assert metadata["actual_first_timestamp_ist"].startswith("2026-10-20T09:15")
    assert (config.data_dir / f"{config.cache_stem}.csv").is_file()
    assert (config.data_dir / f"{config.cache_stem}.json").is_file()
    with pytest.raises(DataAcquisitionError, match="has not passed validation"):
        load_or_download(config, offline=True, clock=lambda: FIXED_NOW)
    quality = validate_bars(
        downloaded,
        config,
        metadata,
    ).quality
    assert quality["passed"] is True
    finalize_cache_validation(config, quality)

    offline, cached_metadata = load_or_download(
        config,
        offline=True,
        downloader=lambda *_args, **_kwargs: pytest.fail("offline mode must not call Yahoo"),
        clock=lambda: FIXED_NOW,
    )
    assert offline == downloaded
    assert cached_metadata["retrieval_mode"] == "offline cached reuse"
    assert cached_metadata["retrieved_at_utc"] == metadata["retrieved_at_utc"]
    raw_path = config.data_dir / metadata["original_download_csv"]
    raw_path.write_text("tampered", encoding="utf-8")
    with pytest.raises(DataAcquisitionError, match="Original Yahoo response"):
        load_or_download(config, offline=True, clock=lambda: FIXED_NOW)


def test_offline_cache_freshness_boundary_and_override(tmp_path):
    config = make_config(tmp_path, max_cache_age_days=1.0)
    create_cached(config)

    boundary = FIXED_NOW + timedelta(days=1)
    _, boundary_metadata = load_or_download(
        config,
        offline=True,
        clock=lambda: boundary,
    )
    assert boundary_metadata["cache_freshness_status"] == "fresh"
    assert boundary_metadata["cache_age_days"] == pytest.approx(1.0)
    assert boundary_metadata["stale_cache_override"] is False

    stale_now = boundary + timedelta(seconds=1)
    with pytest.raises(DataAcquisitionError, match="is stale"):
        load_or_download(config, offline=True, clock=lambda: stale_now)
    _, stale_metadata = load_or_download(
        config,
        offline=True,
        allow_stale_cache=True,
        clock=lambda: stale_now,
    )
    assert stale_metadata["cache_freshness_status"] == "stale"
    assert stale_metadata["stale_cache_override"] is True
    assert stale_metadata["cache_integrity_status"] == "verified"


@pytest.mark.parametrize(
    "retrieved_at",
    [
        "not-a-timestamp",
        "2026-10-20T12:00:00",
        "2026-10-22T12:00:00+05:30",
    ],
)
def test_offline_cache_rejects_malformed_naive_or_future_retrieval_time(tmp_path, retrieved_at):
    config = make_config(tmp_path)
    create_cached(config)
    manifest_path = config.data_dir / f"{config.cache_stem}.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["retrieved_at_utc"] = retrieved_at
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(DataAcquisitionError, match="retrieval timestamp"):
        load_or_download(config, offline=True, clock=lambda: FIXED_NOW)


def test_offline_cache_rejects_incompatible_manifest_and_request(tmp_path):
    config = make_config(tmp_path)
    create_cached(config)
    manifest_path = config.data_dir / f"{config.cache_stem}.json"
    original = json.loads(manifest_path.read_text(encoding="utf-8"))

    manifest_path.write_text("[]", encoding="utf-8")
    with pytest.raises(DataAcquisitionError, match="unsupported structure"):
        load_or_download(config, offline=True, clock=lambda: FIXED_NOW)

    manifest = dict(original, manifest_schema_version=999)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(DataAcquisitionError, match="schema version is incompatible"):
        load_or_download(config, offline=True, clock=lambda: FIXED_NOW)

    manifest = dict(original, manifest_schema_version=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(DataAcquisitionError, match="schema version is incompatible"):
        load_or_download(config, offline=True, clock=lambda: FIXED_NOW)

    manifest = dict(original)
    manifest["request"] = dict(original["request"], ticker="OTHER")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(DataAcquisitionError, match="does not match"):
        load_or_download(config, offline=True, clock=lambda: FIXED_NOW)


def test_legacy_manifest_without_validation_binding_cannot_be_reused(tmp_path):
    config = make_config(tmp_path)
    bars, metadata = create_cached(config)
    quality = validate_bars(bars, config, metadata).quality
    manifest_path = config.data_dir / f"{config.cache_stem}.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for field in (
        "manifest_schema_version",
        "cache_schema_version",
        "retrieval_id",
        "validated_csv_sha256",
        "validated_original_download_sha256",
        "validated_retrieval_id",
    ):
        manifest.pop(field, None)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert manifest["validation_status"] == "passed"
    assert manifest["csv_sha256"]
    assert manifest["original_download_sha256"]
    with pytest.raises(DataAcquisitionError, match="(?i)legacy.*refresh and validate"):
        load_or_download(config, offline=True, clock=lambda: FIXED_NOW)
    with pytest.raises(DataAcquisitionError, match="(?i)legacy.*refresh and validate"):
        finalize_cache_validation(config, quality)


@pytest.mark.parametrize("validation_status", ["missing", "failed"])
def test_offline_cache_requires_successful_validation(validation_status, tmp_path):
    config = make_config(tmp_path)
    create_cached(config)
    manifest_path = config.data_dir / f"{config.cache_stem}.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if validation_status == "missing":
        manifest.pop("validation_status")
    else:
        manifest["validation_status"] = validation_status
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(DataAcquisitionError, match="has not passed validation"):
        load_or_download(config, offline=True, clock=lambda: FIXED_NOW)


def test_offline_cache_rejects_normalized_checksum_mismatch(tmp_path):
    config = make_config(tmp_path)
    create_cached(config)
    cache_path = config.data_dir / f"{config.cache_stem}.csv"
    cache_path.write_text(cache_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(DataAcquisitionError, match="Cached data checksum"):
        load_or_download(config, offline=True, clock=lambda: FIXED_NOW)


def test_validation_status_cannot_be_transferred_to_changed_cache(tmp_path):
    config = make_config(tmp_path)
    bars, metadata = create_cached(config)
    quality = validate_bars(bars, config, metadata).quality
    cache_path = config.data_dir / f"{config.cache_stem}.csv"
    cache_path.write_text(cache_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(DataAcquisitionError, match="changed after validation"):
        finalize_cache_validation(config, quality)


def test_online_mode_attempts_download_and_never_falls_back_to_cache(tmp_path):
    config = make_config(tmp_path)
    create_cached(config)
    calls = []

    def fail_download(*_args, **_kwargs):
        calls.append(True)
        raise TimeoutError("private network details")

    with pytest.raises(DataAcquisitionError, match="download failed"):
        load_or_download(
            config,
            downloader=fail_download,
            clock=lambda: FIXED_NOW,
            sleep=lambda _: None,
        )
    assert calls


def test_failed_cache_update_cannot_leave_old_manifest_marking_new_data_valid(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    create_cached(config)
    frame = frame_from_bars(make_session())
    frame.iloc[0, frame.columns.get_loc("Close")] += 0.25

    def fail_normalized_write(*_args, **_kwargs):
        raise OSError("disk write failed")

    monkeypatch.setattr(backtest_data, "_write_cache", fail_normalized_write)
    with pytest.raises(DataAcquisitionError, match="complete cache"):
        load_or_download(
            config,
            downloader=lambda *_args, **_kwargs: frame,
            clock=lambda: FIXED_NOW + timedelta(minutes=1),
            sleep=lambda _: None,
        )
    with pytest.raises(DataAcquisitionError, match="Original Yahoo response"):
        load_or_download(config, offline=True, clock=lambda: FIXED_NOW + timedelta(minutes=1))


def test_online_and_offline_retrieval_modes_produce_identical_strategy_results(tmp_path):
    config = make_config(tmp_path)
    online_bars, online_metadata = create_cached(config)
    online_data = validate_or_raise(online_bars, config, online_metadata)

    offline_bars, offline_metadata = load_or_download(
        config,
        offline=True,
        clock=lambda: FIXED_NOW,
    )
    offline_data = validate_or_raise(offline_bars, config, offline_metadata)

    online_result = run_backtest(online_data, config)
    offline_result = run_backtest(offline_data, config)
    assert online_result.metrics == offline_result.metrics
    assert online_result.signals == offline_result.signals
    assert online_result.trades == offline_result.trades


def test_session_calendar_classifies_holiday_special_regular_and_unresolved(tmp_path):
    config = make_config(
        tmp_path,
        end=date(2026, 10, 23),
        session_calendar=tmp_path / "calendar.json",
    )
    config.session_calendar.write_text(
        json.dumps({
            "source": "manually verified fixture",
            "holidays": ["2026-10-19"],
            "special_sessions": ["2026-10-20"],
            "regular_sessions": ["2026-10-21"],
        }),
        encoding="utf-8",
    )
    bars = make_session(SESSION_DATE) + make_session(date(2026, 10, 21))

    result = validate_bars(bars, config)

    assert result.quality["passed"] is True
    assert result.quality["eligible_sessions"] == ["2026-10-21"]
    details = {row["date"]: row for row in result.quality["session_details"]}
    assert details["2026-10-20"]["calendar_classification"] == "special_session"
    assert details["2026-10-20"]["eligible"] is False
    assert details["2026-10-21"]["calendar_classification"] == "configured_regular_session"
    missing = {
        item["date"]: item["classification"]
        for item in result.quality["missing_session_classifications"]
    }
    assert missing["2026-10-19"] == "configured_exchange_holiday"
    assert missing["2026-10-22"] == "unresolved_holiday_special_session_or_provider_gap"
    assert "not presumed holidays" in result.quality["calendar_note"]


def test_missing_configured_regular_session_fails_quality_gate(tmp_path):
    config = make_config(
        tmp_path,
        start=date(2026, 10, 20),
        end=date(2026, 10, 22),
        session_calendar=tmp_path / "calendar.json",
    )
    config.session_calendar.write_text(
        json.dumps({"regular_sessions": ["2026-10-20", "2026-10-21"]}),
        encoding="utf-8",
    )

    result = validate_bars(make_session(SESSION_DATE), config)

    assert result.quality["eligible_sessions"] == ["2026-10-20"]
    assert result.quality["passed"] is False
    missing = {
        item["date"]: item["classification"]
        for item in result.quality["missing_session_classifications"]
    }
    assert missing["2026-10-21"] == "configured_regular_session_provider_gap"
    assert any("2026-10-21" in error and "no data" in error for error in result.quality["errors"])


def test_configured_regular_date_outside_requested_range_does_not_fail_gate(tmp_path):
    config = make_config(
        tmp_path,
        start=date(2026, 10, 20),
        end=date(2026, 10, 22),
        session_calendar=tmp_path / "calendar.json",
    )
    config.session_calendar.write_text(
        json.dumps({"regular_sessions": ["2026-10-20", "2026-10-23"]}),
        encoding="utf-8",
    )

    result = validate_bars(make_session(SESSION_DATE), config)

    assert result.quality["passed"] is True
    assert result.quality["eligible_sessions"] == ["2026-10-20"]
    assert all(
        item["date"] != "2026-10-23"
        for item in result.quality["missing_session_classifications"]
    )


def test_cli_stops_on_missing_configured_regular_session_and_clears_old_results(
    tmp_path,
    monkeypatch,
):
    config = make_config(tmp_path)
    create_cached(config)
    _, prior_data = validated(make_session(), tmp_path)
    write_results(config, prior_data, run_backtest(prior_data, config))
    calendar_path = tmp_path / "calendar.json"
    calendar_path.write_text(
        json.dumps({"regular_sessions": ["2026-10-20", "2026-10-21"]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        backtest_cli,
        "run_backtest",
        lambda *_args, **_kwargs: pytest.fail("quality failure must prevent performance backtest"),
    )

    exit_code = backtest_cli.main([
        "--start", "2026-10-19",
        "--end", "2026-10-22",
        "--offline",
        "--data-dir", str(config.data_dir),
        "--output-dir", str(config.output_dir),
        "--session-calendar", str(calendar_path),
    ])

    assert exit_code == 2
    metrics = json.loads((config.output_dir / "orb_metrics.json").read_text(encoding="utf-8"))
    assert metrics["backtest_run"] is False
    assert "2026-10-21" in " ".join(metrics["errors"])
    assert len(pd.read_csv(config.output_dir / "orb_trades.csv")) == 0
    assert len(pd.read_csv(config.output_dir / "orb_signals.csv")) == 0
    report = (config.output_dir / "orb_backtest_report.md").read_text(encoding="utf-8")
    assert "No performance results were generated for this run." in report


def test_invalid_session_calendar_fails_quality_gate(tmp_path):
    config = make_config(tmp_path, session_calendar=tmp_path / "calendar.json")
    config.session_calendar.write_text("{invalid", encoding="utf-8")

    result = validate_bars(make_session(), config)

    assert result.quality["passed"] is False
    assert any("Configured session calendar is unreadable" in error for error in result.quality["errors"])


def test_session_calendar_rejects_duplicate_dates(tmp_path):
    config = make_config(tmp_path, session_calendar=tmp_path / "calendar.json")
    config.session_calendar.write_text(
        json.dumps({"holidays": ["2026-10-19", "2026-10-19"]}),
        encoding="utf-8",
    )

    result = validate_bars(make_session(), config)

    assert result.quality["passed"] is False
    assert any("calendar dates must be ISO" in error for error in result.quality["errors"])


def test_failure_report_replaces_stale_performance_artifacts(tmp_path):
    config, data = validated(make_session(), tmp_path)
    result = run_backtest(data, config)
    write_results(config, data, result)
    failure = {
        "retrieval_mode": "offline cached reuse",
        "validation_status": "failed",
        "cache_freshness_status": "stale",
        "errors": ["Cached data is stale."],
    }

    write_failure_report(config.output_dir, failure)

    metrics = json.loads((config.output_dir / "orb_metrics.json").read_text(encoding="utf-8"))
    assert metrics == {"backtest_run": False, "errors": ["Cached data is stale."]}
    assert len(pd.read_csv(config.output_dir / "orb_trades.csv")) == 0
    assert len(pd.read_csv(config.output_dir / "orb_signals.csv")) == 0
    report = (config.output_dir / "orb_backtest_report.md").read_text(encoding="utf-8")
    assert "No performance results were generated for this run." in report
    assert "Cache freshness: stale" in report


def test_empty_download_and_network_error_retry_without_fallback(tmp_path):
    config = make_config(tmp_path, max_retries=2)
    calls = []

    def empty(*_args, **_kwargs):
        calls.append(True)
        return pd.DataFrame()

    with pytest.raises(DataAcquisitionError, match="no rows"):
        load_or_download(config, downloader=empty, clock=lambda: FIXED_NOW, sleep=lambda _: None)
    assert len(calls) == 3

    calls.clear()

    def failure(*_args, **_kwargs):
        calls.append(True)
        raise TimeoutError("could contain upstream request details")

    with pytest.raises(DataAcquisitionError, match="TimeoutError") as error:
        load_or_download(config, downloader=failure, clock=lambda: FIXED_NOW, sleep=lambda _: None)
    assert len(calls) == 3
    assert "upstream request details" not in str(error.value)
    assert not list(config.data_dir.glob("*.csv"))


def test_missing_ohlc_is_preserved_in_raw_download_and_excludes_session(tmp_path):
    config = make_config(tmp_path)
    frame = frame_from_bars(make_session())
    frame.iloc[20, frame.columns.get_loc("Open")] = float("nan")
    bars, metadata = load_or_download(
        config,
        downloader=lambda *_args, **_kwargs: frame,
        clock=lambda: FIXED_NOW,
        sleep=lambda _: None,
    )
    raw_path = config.data_dir / metadata["original_download_csv"]
    assert raw_path.is_file()
    assert pd.isna(bars[20].open)
    quality = validate_bars(bars, config, metadata).quality
    assert quality["passed"] is False
    assert quality["invalid_ohlc_count"] == 1
    assert quality["rejected_sessions"][0]["date"] == SESSION_DATE.isoformat()


def test_invalid_session_is_removed_before_strategy_indicators_and_events(tmp_path):
    invalid_session = make_session(date(2026, 10, 19))
    invalid_session[20] = replace(invalid_session[20], high=98.0)
    bars = invalid_session + make_session(date(2026, 10, 20))
    config = make_config(tmp_path)

    data = validate_bars(bars, config)

    assert data.quality["passed"] is True
    assert data.quality["invalid_ohlc_count"] == 1
    assert {bar.timestamp.date().isoformat() for bar in data.bars} == {"2026-10-20"}
    result = run_backtest(data, config)
    assert {signal.session_date for signal in result.signals} == {"2026-10-20"}
    assert {trade.session_date for trade in result.trades} == {"2026-10-20"}


def test_offline_missing_mismatch_and_expired_cache(tmp_path):
    config = make_config(tmp_path)
    with pytest.raises(DataAcquisitionError, match="no matching cached"):
        load_or_download(config, offline=True, clock=lambda: FIXED_NOW)

    old = replace(config, start=date(2026, 8, 1), end=date(2026, 8, 3))
    old_bars = make_session(session_date=date(2026, 8, 2))
    bars, _ = load_or_download(
        old,
        offline=False,
        downloader=lambda *_a, **_k: frame_from_bars(old_bars),
        clock=lambda: datetime(2026, 8, 4, 12, 0, tzinfo=KOLKATA),
    )
    assert bars
    old_metadata = json.loads(
        (old.data_dir / f"{old.cache_stem}.json").read_text(encoding="utf-8")
    )
    old_quality = validate_bars(bars, old, old_metadata).quality
    assert old_quality["passed"] is True
    finalize_cache_validation(old, old_quality)
    with pytest.raises(DataAcquisitionError, match="is stale"):
        load_or_download(old, offline=True, clock=lambda: FIXED_NOW)
    reused, metadata = load_or_download(
        old,
        offline=True,
        allow_stale_cache=True,
        clock=lambda: FIXED_NOW,
    )
    assert reused == bars
    assert metadata["retrieval_mode"] == "offline cached reuse"
    assert metadata["cache_freshness_status"] == "stale"
    assert metadata["stale_cache_override"] is True
    with pytest.raises(DataAcquisitionError, match="limited to the last 60"):
        load_or_download(old, offline=False, downloader=lambda *_a, **_k: None, clock=lambda: FIXED_NOW)


def test_invalid_ohlc_duplicates_missing_candles_and_session_boundaries(tmp_path):
    bars = make_session()
    invalid = list(bars)
    invalid[10] = replace(invalid[10], high=98.0)
    report = validate_bars(invalid, make_config(tmp_path)).quality
    assert report["passed"] is False
    assert report["invalid_ohlc_count"] == 1

    duplicate = bars + [bars[10]]
    report = validate_bars(duplicate, make_config(tmp_path)).quality
    assert report["passed"] is False
    assert report["duplicate_timestamp_count"] == 1

    incomplete = bars[:20] + bars[21:]
    report = validate_bars(incomplete, make_config(tmp_path)).quality
    assert report["passed"] is False
    assert report["eligible_session_count"] == 0
    assert report["missing_candle_count"] == 1
    with pytest.raises(DataValidationError, match="no backtest was run"):
        validate_or_raise(incomplete, make_config(tmp_path))

    outside = list(bars)
    outside.append(Bar(
        datetime(2026, 10, 20, 8, 55, tzinfo=KOLKATA), 100, 101, 99, 100
    ))
    report = validate_bars(outside, make_config(tmp_path)).quality
    assert report["passed"] is False
    assert report["off_session_row_count"] == 1

    flat = [replace(bar, open=100.0, high=100.0, low=100.0, close=100.0) for bar in bars]
    report = validate_bars(flat, make_config(tmp_path)).quality
    assert report["passed"] is False
    assert "opening-range width is zero" in report["rejected_sessions"][0]["reason"]


def test_opening_range_breakout_next_open_target_and_r_multiple(tmp_path):
    config, data = validated(make_session(), tmp_path)
    result = run_backtest(data, config)
    assert len(result.signals) == 1
    signal = result.signals[0]
    assert signal.direction == "LONG"
    assert signal.accepted is True
    assert signal.filters_passed is True
    assert signal.entry_attempted is True
    assert signal.timestamp.endswith("09:45:00+05:30")
    assert signal.decision_timestamp.endswith("09:50:00+05:30")
    assert signal.entry_timestamp.endswith("09:50:00+05:30")
    assert signal.entry_price == 101.5
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert (trade.stop_price, trade.target_price) == (99.0, 106.5)
    assert trade.exit_reason == "TARGET"
    assert trade.exit_price == 106.5
    assert trade.gross_pnl_points == 5.0
    assert trade.realized_r_multiple == 2.0
    assert result.metrics["net_underlying_points"] == 5.0
    assert result.metrics["options_backtest_performed"] is False
    assert result.metrics["raw_breakout_events"] == 1
    assert result.metrics["filter_passing_events"] == 1
    assert result.metrics["entry_attempts"] == 1
    assert result.metrics["completed_trades"] == 1


def test_breakout_events_are_transitions_not_repeated_outside_closes():
    bars = make_session()
    closes = {6: 102.0, 7: 102.0, 8: 100.0, 9: 98.0, 10: 98.0, 11: 100.0, 12: 102.0}
    for index, close in closes.items():
        bar = bars[index]
        bars[index] = replace(
            bar,
            high=max(bar.high, close),
            low=min(bar.low, close),
            close=close,
        )
    opening_high = max(bar.high for bar in bars[:6])
    opening_low = min(bar.low for bar in bars[:6])

    assert _breakout_events(bars, opening_high, opening_low) == [
        (6, "LONG"),
        (9, "SHORT"),
        (12, "LONG"),
    ]


def test_new_breakout_event_during_open_position_is_not_entered(tmp_path):
    bars = make_session(exit_kind="quiet")
    bars[7] = replace(bars[7], open=100.5, high=101.0, low=100.0, close=100.5)
    bars[8] = replace(bars[8], open=100.5, high=101.5, low=100.2, close=101.2)
    config, data = validated(bars, tmp_path)

    result = run_backtest(data, config)

    assert len(result.signals) == 2
    assert result.signals[0].entry_attempted is True
    assert result.signals[1].direction == "LONG"
    assert result.signals[1].filters_passed is True
    assert result.signals[1].entry_attempted is False
    assert "another position is already open" in result.signals[1].reason
    assert len(result.trades) == 1


def test_filter_variants_share_raw_event_set_and_reconcile_counts(tmp_path):
    bars = (
        make_session(date(2026, 10, 19), breakout=None, exit_kind="quiet")
        + make_session(date(2026, 10, 20), exit_kind="quiet")
    )
    results = []
    for options in ({}, {"ema50_filter": True}, {"opening_range_atr_max_ratio": 0.5}):
        config, data = validated(bars, tmp_path, **options)
        results.append(run_backtest(data, config))

    counts = [result.metrics["raw_breakout_events"] for result in results]
    assert len(set(counts)) == 1
    for result in results:
        metrics = result.metrics
        assert metrics["raw_breakout_events"] == len(result.signals)
        assert metrics["filter_passing_events"] + metrics["filter_rejected_events"] == len(result.signals)
        assert metrics["entry_attempts"] == sum(signal.entry_attempted for signal in result.signals)
        assert metrics["entry_rejected_events"] == sum(
            signal.filters_passed and not signal.entry_attempted
            for signal in result.signals
        )
        assert metrics["completed_trades"] == len(result.trades)
        assert sum(trade.gross_pnl_points for trade in result.trades) == pytest.approx(
            metrics["gross_underlying_points"]
        )


def test_ema_and_atr_values_do_not_depend_on_future_bars():
    bars = make_session(date(2026, 10, 19), breakout=None, exit_kind="quiet")
    later_bars = make_session(date(2026, 10, 20), breakout=None, exit_kind="quiet")
    extended = bars + later_bars

    assert _ema_values(extended)[:len(bars)] == _ema_values(bars)
    assert _atr_values(extended)[:len(bars)] == _atr_values(bars)


def test_incremental_runner_waits_for_next_open_and_matches_batch_results(tmp_path):
    config, data = validated(make_session(), tmp_path)
    runner = BacktestRunner(data.quality, config)
    emitted = []

    for bar in data.bars[:7]:
        emitted.extend(runner.process_bar(bar))
    assert emitted == []

    emitted.extend(runner.process_bar(data.bars[7]))
    assert [event["type"] for event in emitted] == ["signal", "entry"]
    assert emitted[0]["signal"]["accepted"] is True
    assert emitted[0]["signal"]["entry_timestamp"].endswith("09:50:00+05:30")
    assert emitted[1]["timestamp"].endswith("09:50:00+05:30")
    assert runner.open_position["direction"] == "LONG"
    assert runner.open_position["unrealized_points"] == pytest.approx(0.5)

    for bar in data.bars[8:]:
        emitted.extend(runner.process_bar(bar))
    incremental = runner.finish()
    batch = run_backtest(data, config)

    assert incremental.signals == batch.signals
    assert incremental.trades == batch.trades
    assert incremental.metrics == batch.metrics
    assert [event["type"] for event in emitted] == ["signal", "entry", "exit"]


def test_incremental_prefix_cannot_use_a_future_entry_candle(tmp_path):
    config, data = validated(make_session(), tmp_path)
    prefix = BacktestRunner(data.quality, config)
    for bar in data.bars[:7]:
        assert prefix.process_bar(bar) == []

    partial = prefix.finish()

    assert len(partial.signals) == 1
    assert partial.signals[0].accepted is False
    assert partial.signals[0].entry_timestamp is None
    assert "no next candle" in partial.signals[0].reason


def test_short_breakout_and_gap_through_stop(tmp_path):
    config, data = validated(make_session(breakout="SHORT"), tmp_path)
    result = run_backtest(data, config)
    trade = result.trades[0]
    assert trade.direction == "SHORT"
    assert trade.stop_price == 101.0
    assert trade.target_price == 93.5
    assert trade.gross_pnl_points == 5.0
    assert trade.realized_r_multiple == 2.0

    gapped = make_session()
    gapped[7] = replace(gapped[7], open=98.0, high=100.0, low=97.0, close=100.0)
    gapped[8] = replace(gapped[8], open=100.0, high=101.0, low=99.5, close=100.0)
    config, data = validated(gapped, tmp_path)
    result = run_backtest(data, config)
    assert result.trades == []
    assert result.signals[0].accepted is False
    assert "gap makes" in result.signals[0].reason


def test_stop_target_same_candle_uses_stop_first_and_stop_gap_fills_open(tmp_path):
    config, data = validated(make_session(exit_kind="both"), tmp_path)
    result = run_backtest(data, config)
    assert result.trades[0].exit_reason == "STOP"
    assert result.trades[0].exit_price == 99.0
    assert "conservative stop-first" in result.trades[0].execution_adjustment

    gapped = make_session(exit_kind="quiet")
    gapped[8] = replace(gapped[8], open=97.0, high=99.0, low=96.0, close=98.0)
    config, data = validated(gapped, tmp_path)
    result = run_backtest(data, config)
    assert result.trades[0].exit_price == 97.0
    assert "gap filled at candle open" in result.trades[0].execution_adjustment

    favorable_gap = make_session(exit_kind="quiet")
    favorable_gap[8] = replace(
        favorable_gap[8], open=108.0, high=109.0, low=107.0, close=108.0
    )
    config, data = validated(favorable_gap, tmp_path)
    result = run_backtest(data, config)
    assert result.trades[0].exit_reason == "TARGET"
    assert result.trades[0].exit_price == result.trades[0].target_price
    assert "no price improvement" in result.trades[0].execution_adjustment


def test_time_exit_cutoff_and_missing_next_execution_price(tmp_path):
    config, data = validated(make_session(exit_kind="quiet"), tmp_path)
    result = run_backtest(data, config)
    assert result.trades[0].exit_reason == "TIME_EXIT"
    assert result.trades[0].exit_timestamp.endswith("15:15:00+05:30")

    late = make_session(exit_kind="quiet")
    late[69] = replace(late[69], open=100.0, high=102.0, low=100.0, close=102.0)
    late_config, late_data = validated(late, tmp_path)
    late_result = run_backtest(late_data, late_config)
    assert any(not signal.accepted and "entry cutoff" in signal.reason for signal in late_result.signals)

    no_next = make_session(exit_kind="quiet")
    no_next[74] = replace(no_next[74], open=100.0, high=102.0, low=100.0, close=102.0)
    no_next_config, no_next_data = validated(no_next, tmp_path)
    no_next_result = run_backtest(no_next_data, no_next_config)
    assert any(not signal.accepted and "no next candle" in signal.reason for signal in no_next_result.signals)


def test_ema_atr_filters_and_unsupported_volume_filters(tmp_path):
    bars = make_session()
    config, data = validated(bars, tmp_path, ema50_filter=True)
    result = run_backtest(data, config)
    assert result.signals[0].active_filters[0].startswith("EMA(50)")
    assert "EMA(50) unavailable" in result.signals[0].reason

    warmup = make_session(date(2026, 10, 19), breakout=None, exit_kind="quiet")
    config, data = validated(
        warmup + make_session(),
        tmp_path,
        opening_range_atr_max_ratio=0.5,
    )
    result = run_backtest(data, config)
    atr_signal = next(signal for signal in result.signals if signal.session_date == "2026-10-20")
    assert atr_signal.accepted is False
    assert "ATR(14)" in atr_signal.active_filters[0]
    assert "exceeded" in atr_signal.reason

    ema_warmup = [
        replace(
            bar,
            open=bar.open * 3,
            high=bar.high * 3,
            low=bar.low * 3,
            close=bar.close * 3,
        )
        for bar in make_session(date(2026, 10, 19), breakout=None, exit_kind="quiet")
    ]
    config, data = validated(
        ema_warmup + make_session(),
        tmp_path,
        ema50_filter=True,
    )
    result = run_backtest(data, config)
    ema_signal = next(signal for signal in result.signals if signal.session_date == "2026-10-20")
    assert ema_signal.accepted is False
    assert "alignment failed" in ema_signal.reason

    config, data = validated(make_session(), tmp_path, vwap_filter=True)
    with pytest.raises(UnsupportedFilterError, match="not been validated"):
        run_backtest(data, config)
    config, data = validated(make_session(), tmp_path, breakout_volume_filter=True)
    with pytest.raises(UnsupportedFilterError, match="not been validated"):
        run_backtest(data, config)

    candles = [Bar(datetime(2026, 10, 19, 9, i * 5, tzinfo=KOLKATA), 1, 2, 1, 2) for i in range(4)]
    assert _ema_values(candles, period=3) == [None, None, 2.0, 2.0]
    assert _atr_values(candles, period=3) == [None, None, 1.0, 1.0]


def test_metrics_edge_cases_drawdown_streaks_and_zero_denominators(tmp_path):
    config, data = validated(make_session(), tmp_path)
    trade = run_backtest(data, config).trades[0]
    loss = replace(trade, gross_pnl_points=-3.0, realized_r_multiple=-1.0)
    win = replace(trade, session_date="2026-10-19", gross_pnl_points=5.0, realized_r_multiple=2.0)
    second_loss = replace(trade, session_date="2026-10-21", gross_pnl_points=-2.0, realized_r_multiple=-0.8)
    metrics = calculate_metrics([win, loss, second_loss], 3, [])
    assert metrics["gross_underlying_points"] == 0
    assert metrics["profit_factor"] == pytest.approx(5 / 5)
    assert metrics["maximum_drawdown_points"] == 5.0
    assert metrics["maximum_consecutive_losses"] == 2

    no_loss = calculate_metrics([win], 1, [])
    assert no_loss["profit_factor"] is None
    assert no_loss["profit_factor_defined"] is False
    assert no_loss["average_loss_points"] is None


def test_session_metrics_include_eligible_sessions_without_trades(tmp_path):
    bars = make_session(date(2026, 10, 19), breakout=None, exit_kind="quiet")
    bars[8] = replace(bars[8], open=100.0, high=101.0, low=99.0, close=100.0)
    bars += make_session(date(2026, 10, 20))
    config, data = validated(bars, tmp_path)

    result = run_backtest(data, config)

    assert result.metrics["eligible_sessions"] == 2
    assert result.metrics["by_session"]["2026-10-19"] == {
        "trades": 0,
        "gross_pnl_points": 0.0,
        "directions": [],
    }
    assert result.metrics["by_session"]["2026-10-20"]["trades"] == 1


def test_research_verdict_uses_chronological_session_holdout(tmp_path):
    config, data = validated(make_session(), tmp_path)
    template = run_backtest(data, config).trades[0]
    many = []
    for day in range(1, 11):
        session = date(2026, 10, day)
        for trade_index in range(4):
            value = -1.0 if day == 10 and trade_index == 0 else 1.0
            many.append(replace(
                template,
                session_date=session.isoformat(),
                gross_pnl_points=value,
            ))
    assessment = _research_validation(many, sessions=10)
    assert assessment["split_session_date"] == "2026-10-08"
    assert assessment["development_trades"] == 28
    assert assessment["out_of_sample_trades"] == 12
    assert assessment["verdict"] == "promising"
    assert "not evidence of future performance" in assessment["reason"]


def test_report_outputs_include_limitations_and_no_option_pnl(tmp_path):
    config, data = validated(make_session(), tmp_path)
    result = run_backtest(data, config)
    outputs = write_results(config, data, result)
    assert {path.name for path in outputs} == {
        "orb_backtest_report.md", "orb_trades.csv", "orb_signals.csv",
        "orb_data_quality.json", "orb_metrics.json",
    }
    report = (config.output_dir / "orb_backtest_report.md").read_text(encoding="utf-8")
    metrics = json.loads((config.output_dir / "orb_metrics.json").read_text(encoding="utf-8"))
    assert "NIFTY options P&L" in report
    assert "NOT PERFORMED" in report
    assert "Options backtest" in report
    assert metrics["options_backtest_performed"] is False
    assert metrics["net_underlying_points"] == metrics["gross_underlying_points"]
