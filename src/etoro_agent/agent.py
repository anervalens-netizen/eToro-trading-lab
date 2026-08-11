from __future__ import annotations

import json
import os
from pathlib import Path

from .audit import AuditLog
from .config import AppConfig
from .execution import EtoroDemoBroker, PaperBroker
from .market import MarketDataCollector
from .models import KillState, RiskContext
from .portfolio import DemoPortfolioMonitor, PaperPortfolioMonitor
from .risk import DeterministicRiskEngine, load_private_signing_key
from .strategy import MovingAverageStrategy


class TradingAgent:
    def __init__(
        self, config: AppConfig, audit: AuditLog, collector: MarketDataCollector, runtime_dir: Path
    ) -> None:
        self.config = config
        self.audit = audit
        self.collector = collector
        self.runtime_dir = runtime_dir
        key_path = Path(
            os.getenv(
                "ETORO_RISK_SIGNING_KEY_FILE",
                str(runtime_dir / "risk-signing.key"),
            )
        )
        if key_path.exists():
            self.risk = DeterministicRiskEngine(config.risk, load_private_signing_key(key_path))
        elif config.account_mode == "demo":
            raise RuntimeError(
                "DEMO mode requires a persistent Ed25519 signer provisioned via init-security"
            )
        else:
            self.risk = DeterministicRiskEngine(config.risk)
        self.strategy = MovingAverageStrategy(config.strategy)

    def run_once(self, symbol: str) -> dict[str, object]:
        symbol = symbol.upper()
        if symbol not in self.config.symbols:
            raise ValueError("symbol not configured")
        market = self.collector.collect(
            symbol,
            self.config.symbols[symbol],
            self.config.candle_interval,
            self.config.candle_count,
        )
        self.audit.append(
            "market_snapshot",
            {
                "symbol": symbol,
                "instrument_id": market.instrument_id,
                "bid": market.bid,
                "ask": market.ask,
                "closes": market.closes,
            },
        )
        intent = self.strategy.decide(symbol, market.closes)
        if intent is None:
            self.audit.append("decision_hold", {"symbol": symbol})
            return {"status": "hold"}
        self.audit.append("trade_intent", intent.__dict__)
        if self.config.account_mode == "demo":
            self.collector.client.verify_demo_scope()
            portfolio = DemoPortfolioMonitor(self.collector.client, self.audit).snapshot(
                market.instrument_id
            )
        else:
            portfolio = PaperPortfolioMonitor(self.audit, self.config.initial_cash_usd).snapshot(
                symbol, market.bid
            )
        context = RiskContext(
            equity_usd=portfolio.equity_usd,
            peak_equity_usd=portfolio.peak_equity_usd,
            daily_pnl_usd=portfolio.daily_pnl_usd,
            gross_exposure_usd=portfolio.gross_exposure_usd,
            symbol_exposure_usd=portfolio.symbol_exposure_usd,
            trades_today=portfolio.trades_today,
            bid=market.bid,
            ask=market.ask,
            kill_switch_active=(
                (self.runtime_dir / "KILL_SWITCH").exists()
                or self.audit.kill_state() is not KillState.ACTIVE
            ),
            quote_observed_at=int(market.captured_at.timestamp()),
            data_quality_ok=(
                market.market_open and bool(market.quality is None or market.quality.is_valid)
            ),
            audit_writable=True,
            reconciliation_ok=True,
            open_positions=1 if portfolio.symbol_exposure_usd > 0 else 0,
        )
        result = self.risk.evaluate(intent, context)
        if not result.approved or result.order is None:
            self.audit.append("risk_rejection", {"symbol": symbol, "reasons": result.reasons})
            return {"status": "rejected", "reasons": result.reasons}
        order = result.order
        request = {
            "account": "DEMO",
            "method": order.method,
            "path": order.route,
            "body": json.loads(order.body_json),
        }
        envelope_hash = self.audit.register_proposal(order.proposal_id, request, order)
        self.audit.append(
            "risk_approval",
            {
                "proposal_id": order.proposal_id,
                "envelope_hash": envelope_hash,
                "request": request,
                "expires_at": order.expires_at,
                "intent_hash": order.intent_hash,
                "risk_snapshot_hash": order.risk_snapshot_hash,
                "risk_config_hash": order.risk_config_hash,
            },
        )
        if self.config.account_mode == "paper":
            fill = PaperBroker(self.audit).execute(
                order, self.risk.verifier(), market.bid, market.ask
            )
            PaperPortfolioMonitor(self.audit, self.config.initial_cash_usd).snapshot(
                symbol, fill.price
            )
            return {"status": "paper_filled", "fill": fill.__dict__}
        return {
            "status": "awaiting_operator_approval",
            "proposal_id": order.proposal_id,
            "envelope_hash": envelope_hash,
            "expires_at": order.expires_at,
            "request": request,
        }

    def execute_pending_demo(self, proposal_id: str) -> dict[str, object]:
        if not self.config.etoro_demo_execution_enabled:
            raise PermissionError("eToro DEMO execution is disabled")
        order = self.audit.load_order(proposal_id)
        result = EtoroDemoBroker(self.collector.client, self.audit, self.runtime_dir).execute(
            order, self.risk.verifier()
        )
        return {
            "status_code": result.status_code,
            "is_success": result.is_success,
            "body": result.body,
            "x_request_id": result.x_request_id,
        }

    def reconcile_demo(self) -> dict[str, object]:
        if not self.config.etoro_demo_execution_enabled:
            raise PermissionError("eToro DEMO execution is disabled")
        return EtoroDemoBroker(self.collector.client, self.audit, self.runtime_dir).reconcile()
