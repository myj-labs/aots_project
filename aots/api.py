from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from .config import get_settings
from .database import Database, IdempotencyConflict
from .models import (
    PaperTradeRequest,
    MarkPriceRequest,
    KillSwitchRequest,
    HistoricalReplayRequest,
    HistoricalCandlesCheckRequest,
    StrategyEvaluationRequest,
)
from .paper import PaperBroker
from .brokers.response_log import HISTORICAL_RESPONSE_LOG, LIVE_FEED_DIAGNOSTICS
from .provider_session import (
    ProviderConnectRequest,
    ProviderRequestError,
    ProviderSessionManager,
)
from .risk import RiskEngine, RiskRejected
from .live_paper import LivePaperService, LivePaperStartRequest
from .strategy_service import StrategyService
from .backtest.data import BacktestConfig
from .options_backtest.config import OptionsBacktestConfig
from .options_backtest.service import OptionsBacktestService, RunRejected
from .backtest.replay import (
    ReplayCapacityError,
    ReplayNotFound,
    ReplayService,
)

settings = get_settings()
db = Database(settings.database_path)
risk = RiskEngine(settings, db)
paper = PaperBroker(settings, db, risk)
STATIC_DIR = Path(__file__).parent / "static"


provider_session = ProviderSessionManager()
replay_service = ReplayService()
options_backtest_service = OptionsBacktestService(lambda: provider_session)
live_paper_service = LivePaperService(
    settings, db, risk, paper, provider_session
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global replay_service, options_backtest_service, live_paper_service
    replay_service = ReplayService()
    options_backtest_service = OptionsBacktestService(lambda: provider_session)
    live_paper_service = LivePaperService(
        settings, db, risk, paper, provider_session
    )
    db.event("APP_STARTED", {"mode": settings.trading_mode, "broker": settings.broker})
    yield
    replay_service.shutdown()
    options_backtest_service.shutdown()
    live_paper_service.shutdown()
    provider_session.shutdown()
    db.event("APP_STOPPED", {})


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/api/provider/"):
        # Default errors echo submitted values; drop them so secrets never leak.
        safe = [
            {"loc": list(e.get("loc", ())), "type": e.get("type"), "msg": e.get("msg")}
            for e in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": safe})
    return await request_validation_exception_handler(request, exc)


@app.get("/api/provider/status")
def provider_status():
    return provider_session.status()


@app.get("/api/live-paper/status")
def live_paper_status():
    return live_paper_service.status()


@app.post("/api/live-paper/start")
def start_live_paper(req: LivePaperStartRequest):
    if not provider_session.status().get("connected"):
        raise HTTPException(
            status_code=409,
            detail="Connect Groww on the Groww tab before starting live paper.",
        )
    try:
        return live_paper_service.start(req)
    except ProviderRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/live-paper/resume")
def resume_live_paper():
    if not provider_session.status().get("connected"):
        raise HTTPException(
            status_code=409,
            detail="Reconnect Groww on the Groww tab before recovering the open paper position.",
        )
    try:
        return live_paper_service.resume()
    except ProviderRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/live-paper/stop")
def stop_live_paper():
    return live_paper_service.stop()


@app.get("/api/live-paper/events")
def live_paper_events():
    return [
        event for event in db.events(100)
        if event["event_type"].startswith("LIVE_PAPER_")
    ][:50]


@app.post("/api/provider/connect")
def provider_connect(req: ProviderConnectRequest):
    if live_paper_service.status()["status"] in {
        "RUNNING", "STARTING", "RUNNING_POSITION_RECOVERY",
        "STOPPING_POSITION_OPEN", "SESSION_END_EXIT_PENDING",
    }:
        raise HTTPException(
            status_code=409,
            detail="Stop live paper and reconcile any open paper position before reconnecting Groww.",
        )
    try:
        return provider_session.connect(req)
    except ProviderRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@app.post("/api/provider/disconnect")
def provider_disconnect():
    live_paper_service.shutdown()
    return provider_session.disconnect()


@app.get("/api/provider/quote")
def provider_quote(symbol: str = "NIFTY"):
    try:
        return provider_session.index_quote(symbol)
    except ProviderRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@app.get("/api/provider/option-chain")
def provider_option_chain(underlying: str = "NIFTY", expiry: str = ""):
    try:
        return provider_session.option_chain(underlying, expiry)
    except ProviderRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@app.get("/api/provider/account-insights")
def provider_account_insights():
    try:
        return provider_session.account_insights()
    except ProviderRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@app.get("/api/provider/historical/expiries")
def provider_historical_expiries(
    year: int = Query(ge=2020, le=2100),
    month: int = Query(ge=1, le=12),
):
    try:
        return {"expiries": provider_session.historical_expiries(year, month)}
    except ProviderRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@app.get("/api/provider/historical/contracts")
def provider_historical_contracts(expiry_date: str = Query(min_length=10, max_length=10)):
    try:
        return {"contracts": provider_session.historical_contracts(expiry_date)}
    except ProviderRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@app.post("/api/provider/historical/candles/check")
def provider_historical_candles_check(req: HistoricalCandlesCheckRequest):
    try:
        return provider_session.check_historical_candles(
            req.groww_symbol, req.start_time, req.end_time, req.candle_interval
        )
    except ProviderRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@app.get("/api/provider/response-log")
def provider_response_log():
    return {
        "capacity": HISTORICAL_RESPONSE_LOG.capacity,
        "scope": "Groww historical market-data responses only (expiries, contracts, candles); in memory, newest first.",
        "entries": HISTORICAL_RESPONSE_LOG.snapshot(),
    }


@app.post("/api/provider/response-log/clear")
def provider_response_log_clear():
    HISTORICAL_RESPONSE_LOG.clear()
    return {"cleared": True}


@app.get("/api/live-paper/diagnostics")
def live_paper_diagnostics():
    return {
        "capacity": LIVE_FEED_DIAGNOSTICS.capacity,
        "scope": "Redacted Live Paper ORB and Groww market-feed lifecycle diagnostics; in memory, newest first.",
        "entries": LIVE_FEED_DIAGNOSTICS.snapshot(),
    }


@app.post("/api/live-paper/diagnostics/clear")
def clear_live_paper_diagnostics():
    LIVE_FEED_DIAGNOSTICS.clear()
    return {"cleared": True}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/health")
def health():
    return {"status": "ok", "mode": settings.trading_mode, "live_trading_enabled": False}


@app.get("/api/status")
def status():
    snap = risk.snapshot()
    return {
        "app_name": settings.app_name,
        "environment": settings.environment,
        "broker": settings.broker,
        "mode": settings.trading_mode,
        "underlying": settings.underlying,
        "live_trading_enabled": False,
        "capital_inr": settings.capital_inr,
        "strategy_spot_threshold": settings.strategy_spot_threshold,
        "risk": snap.__dict__,
        "trade_count": len(db.trades()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/risk")
def risk_status():
    return {
        **risk.snapshot().__dict__,
        "max_daily_loss_pct": settings.max_daily_loss_pct,
        "max_trade_risk_pct": settings.max_trade_risk_pct,
        "max_consecutive_losses": settings.max_consecutive_losses,
        "max_open_positions": settings.max_open_positions,
        "max_lots_per_order": settings.max_lots_per_order,
        "market_data_max_age_seconds": settings.market_data_max_age_seconds,
    }


@app.post("/api/risk/kill-switch")
def kill_switch(req: KillSwitchRequest):
    risk.set_kill_switch(req.enabled)
    return {"kill_switch": risk.get_kill_switch(), "message": "Local entry gate updated. This does not send broker cancel/square-off orders."}


@app.get("/api/trades")
def trades():
    return db.trades()


@app.get("/api/positions")
def positions():
    return [t for t in db.trades() if t["status"] == "OPEN"]


@app.get("/api/events")
def events():
    return db.events(100)


@app.post("/api/replay/start")
def start_historical_replay(req: HistoricalReplayRequest):
    try:
        config = BacktestConfig(
            start=req.start,
            end=req.end,
            data_dir=Path("data/backtest"),
            ema50_filter=req.ema50_filter,
            opening_range_atr_max_ratio=req.opening_range_atr_max_ratio,
        )
        return replay_service.start(
            config,
            offline=req.offline,
            allow_stale_cache=req.allow_stale_cache,
        )
    except ReplayCapacityError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/replay")
def historical_replay_sessions():
    return replay_service.list_sessions()


@app.get("/api/replay/{session_id}")
def historical_replay_status(session_id: str):
    try:
        return replay_service.status(session_id)
    except ReplayNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/replay/{session_id}/events")
def historical_replay_events(session_id: str, after: int = Query(default=0, ge=0)):
    try:
        return replay_service.events(session_id, after=after)
    except ReplayNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/replay/{session_id}/results")
def historical_replay_results(session_id: str):
    try:
        return replay_service.results(session_id)
    except ReplayNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/replay/{session_id}/stop")
def stop_historical_replay(session_id: str):
    try:
        return replay_service.stop(session_id)
    except ReplayNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/options-backtest/runs")
def start_options_backtest(config: OptionsBacktestConfig):
    if not provider_session.status().get("connected"):
        raise HTTPException(status_code=409, detail="Connect Groww on the Groww tab before running a backtest.")
    try:
        return options_backtest_service.start(config)
    except RunRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.get("/api/options-backtest/runs")
def list_options_backtests():
    return options_backtest_service.list()


@app.get("/api/options-backtest/runs/{run_id}")
def options_backtest_run(run_id: str):
    try:
        return options_backtest_service.get(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Backtest run not found.") from exc


@app.post("/api/options-backtest/runs/{run_id}/stop")
def stop_options_backtest(run_id: str):
    try:
        return options_backtest_service.stop(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Backtest run not found.") from exc


def _strategy_service():
    return StrategyService(settings, db, risk, paper)


@app.post("/api/strategy/evaluate")
def evaluate_strategy(req: StrategyEvaluationRequest):
    return _strategy_service().evaluate(req)


@app.post("/api/strategy/process")
def process_strategy(req: StrategyEvaluationRequest):
    try:
        return _strategy_service().process(req)
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.get("/api/strategy/decisions")
def strategy_decisions():
    return db.strategy_decisions()


@app.post("/api/paper/trade")
def open_paper_trade(req: PaperTradeRequest):
    try:
        return paper.open_trade(req).model_dump(mode="json")
    except RiskRejected as exc:
        db.event("ENTRY_REJECTED", {"reason": str(exc), "symbol": req.symbol})
        raise HTTPException(status_code=409, detail=str(exc))
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/api/paper/mark")
def mark_paper_trade(req: MarkPriceRequest):
    try:
        result = paper.mark(
            req.trade_id, req.mark_price, req.observation_sequence
        )
        return result.model_dump(mode="json")
    except KeyError:
        raise HTTPException(status_code=404, detail="Trade not found.")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/paper/close/{trade_id}")
def close_paper_trade(trade_id: str, exit_price: float):
    try:
        result = paper.close_trade(trade_id, exit_price)
        return result.model_dump(mode="json")
    except KeyError:
        raise HTTPException(status_code=404, detail="Trade not found.")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
