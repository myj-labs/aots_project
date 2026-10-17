import hashlib
import json
from datetime import datetime

from .models import PaperTradeRequest, StrategyEvaluationRequest
from .risk import RiskRejected
from .strategy import StrategyCandidate, evaluate_spot_threshold


class StrategyService:
    def __init__(self, settings, db, risk, paper):
        self.settings = settings
        self.db = db
        self.risk = risk
        self.paper = paper

    def evaluate(self, request: StrategyEvaluationRequest) -> dict:
        evaluated_at = self.risk.current_time()
        return self._evaluate(request, evaluated_at).as_dict()

    def process(self, request: StrategyEvaluationRequest) -> dict:
        strategy_id = "spot-threshold-demo"
        inputs = {
            "request": request.model_dump(mode="json"),
            "strategy_version": "1",
            "spot_threshold": self.settings.strategy_spot_threshold,
            "max_age_seconds": self.settings.market_data_max_age_seconds,
        }
        input_hash = hashlib.sha256(
            json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        trade_idempotency_key = (
            "strategy:"
            + hashlib.sha256(
                f"{strategy_id}:{request.observation_id}".encode("utf-8")
            ).hexdigest()
        )

        def process_new(conn):
            evaluated_at = self.risk.current_time()
            decision = self._evaluate(request, evaluated_at).as_dict()
            if decision["status"] == "SIGNAL":
                age_seconds = (
                    evaluated_at - request.observed_at
                ).total_seconds()
                paper_request = PaperTradeRequest(
                    symbol=request.symbol,
                    idempotency_key=trade_idempotency_key,
                    side="BUY",
                    entry_price=request.entry_price,
                    stop_price=request.stop_price,
                    take_profit_profit_inr=request.take_profit_profit_inr,
                    quantity=request.quantity,
                    lots=request.lots,
                    market_data_age_seconds=age_seconds,
                )
                try:
                    trade = self.paper.open_trade_in_transaction(
                        paper_request,
                        conn=conn,
                        idempotency_key=trade_idempotency_key,
                    )
                except RiskRejected as exc:
                    decision["status"] = "RISK_REJECTED"
                    decision["reason"] = str(exc)
                    self.db.event(
                        "STRATEGY_ENTRY_REJECTED",
                        {
                            "strategy_id": strategy_id,
                            "observation_id": request.observation_id,
                            "reason": str(exc),
                        },
                        conn=conn,
                    )
                else:
                    decision["status"] = "TRADE_CREATED"
                    decision["reason"] = (
                        "Candidate passed authoritative risk validation; "
                        "a simulated paper trade was created."
                    )
                    decision["resulting_trade_id"] = trade.trade_id
            self.db.event(
                "STRATEGY_DECISION_RECORDED",
                {
                    "strategy_id": strategy_id,
                    "observation_id": request.observation_id,
                    "status": decision["status"],
                    "reason": decision["reason"],
                    "resulting_trade_id": decision["resulting_trade_id"],
                },
                conn=conn,
            )
            return decision

        return self.db.process_strategy_decision(
            strategy_id=strategy_id,
            observation_id=request.observation_id,
            input_hash=input_hash,
            process=process_new,
        )

    def _evaluate(
        self, request: StrategyEvaluationRequest, evaluated_at: datetime
    ):
        candidate = StrategyCandidate(
            symbol=request.symbol,
            entry_price=request.entry_price,
            stop_price=request.stop_price,
            quantity=request.quantity,
            lots=request.lots,
            take_profit_profit_inr=request.take_profit_profit_inr,
        )
        return evaluate_spot_threshold(
            observation_id=request.observation_id,
            underlying=request.underlying.upper(),
            spot=request.spot,
            observed_at=request.observed_at,
            candidate=candidate,
            threshold=self.settings.strategy_spot_threshold,
            expected_underlying=self.settings.underlying.upper(),
            evaluated_at=evaluated_at,
            max_age_seconds=self.settings.market_data_max_age_seconds,
        )
