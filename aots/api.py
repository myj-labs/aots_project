from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from .config import get_settings
from .database import Database, IdempotencyConflict
from .models import PaperTradeRequest, MarkPriceRequest, KillSwitchRequest
from .paper import PaperBroker
from .risk import RiskEngine, RiskRejected

settings = get_settings()
db = Database(settings.database_path)
risk = RiskEngine(settings, db)
paper = PaperBroker(settings, db, risk)
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.event("APP_STARTED", {"mode": settings.trading_mode, "broker": settings.broker})
    yield
    db.event("APP_STOPPED", {})


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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
        result = paper.mark(req.trade_id, req.mark_price)
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
