from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from .ai_store_postgres_v2 import CanonicalPostgresAIStoreV2
from .ai_v2 import AIRole, AIIntentOutputV2, DecisionPacketV2
from .codec_v2 import decode_dataclass
from .config_v2 import load_config_v2
from .decision_v2 import DecisionApplierV2
from .domain_v2 import QuoteProvenance
from .etoro_api_current_v2 import EtoroPublicApiDemoClientV2
from .kernel_v2 import UnifiedTradingKernel
from .postgres_runtime_v2 import PostgresRuntimeStoreV2
from .risk_v2 import BrokerTruth, GlobalRiskKernel


def _dsn(config_path: str) -> str:
    config = load_config_v2(config_path)
    path = os.getenv("ETORO_V2_POSTGRES_DSN_FILE") or config.postgres_dsn_file
    if not path:
        raise RuntimeError("PostgreSQL DSN credential file is required")
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("PostgreSQL DSN credential file is empty")
    return value


def _rate_row(client: EtoroPublicApiDemoClientV2, instrument_id: int) -> Mapping[str, Any]:
    response = client.rates((instrument_id,))
    rows = response.body.get("rates", []) if response.ok and isinstance(response.body, dict) else []
    if len(rows) != 1 or not isinstance(rows[0], Mapping):
        raise RuntimeError("fresh rate unavailable for AI decision application")
    return rows[0]


def _timestamp(value: object) -> datetime:
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("broker timestamp is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _quote(
    client: EtoroPublicApiDemoClientV2,
    *,
    symbol: str,
    instrument_id: int,
    broker_hash: str,
    received_at: datetime,
) -> QuoteProvenance:
    row = _rate_row(client, instrument_id)
    observed = _timestamp(row.get("date", row.get("timestamp", received_at.isoformat())))
    canonical = json.dumps(dict(row), sort_keys=True, separators=(",", ":"), default=str)
    return QuoteProvenance(
        symbol.upper(),
        Decimal(str(row["bid"])),
        Decimal(str(row["ask"])),
        observed,
        received_at,
        "etoro-public-api-decision-apply",
        str(row.get("sequence", "rest")),
        hashlib.sha256(canonical.encode()).hexdigest(),
        broker_hash,
    )


def _broker_truth(
    store: PostgresRuntimeStoreV2,
    client: EtoroPublicApiDemoClientV2,
    *,
    initial_cash: Decimal,
    now: datetime,
) -> BrokerTruth:
    response = client.demo_pnl()
    if not response.ok or not isinstance(response.body, dict):
        raise RuntimeError("DEMO P&L snapshot unavailable")
    portfolio = response.body.get("clientPortfolio", response.body)
    if not isinstance(portfolio, Mapping):
        raise RuntimeError("DEMO portfolio shape invalid")
    credit = Decimal(str(portfolio.get("credit", "0")))
    positions = portfolio.get("positions", [])
    if not isinstance(positions, list):
        raise RuntimeError("DEMO positions shape invalid")
    gross = Decimal("0")
    unrealized = Decimal("0")
    invested = Decimal("0")
    for position in positions:
        if not isinstance(position, Mapping):
            continue
        pnl = position.get("unrealizedPnL") or {}
        if not isinstance(pnl, Mapping):
            pnl = {}
        gross += abs(Decimal(str(pnl.get("exposureInAccountCurrency", position.get("amount", 0)))))
        unrealized += Decimal(str(pnl.get("pnL", 0)))
        invested += Decimal(str(position.get("amount", 0)))
    equity = credit + invested + unrealized
    if equity <= 0:
        raise RuntimeError("DEMO broker equity is invalid")
    cash = client.cash_truth().available_cash_usd
    peak_raw = store.state_get("broker_peak_equity_v2", str(equity))
    peak = max(equity, Decimal(peak_raw))
    store.state_set("broker_peak_equity_v2", str(peak))
    pnl_total = equity - initial_cash
    with store.connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM v2_fills WHERE event_time::date=%s", (now.date(),))
        trades_today = int(cursor.fetchone()[0])
    canonical = json.dumps(portfolio, sort_keys=True, separators=(",", ":"), default=str)
    snapshot_hash = hashlib.sha256(canonical.encode()).hexdigest()
    return BrokerTruth(
        equity,
        peak,
        cash,
        gross,
        gross,
        len(positions),
        trades_today,
        pnl_total,
        pnl_total,
        pnl_total,
        snapshot_hash,
        now,
    )


class DecisionApplyWorkerV2:
    def __init__(self, config_path: str) -> None:
        self.config = load_config_v2(config_path)
        self.store = PostgresRuntimeStoreV2.from_dsn(_dsn(config_path))
        self.store.migrate()
        self.queue = CanonicalPostgresAIStoreV2(self.store)
        self.kernel = UnifiedTradingKernel(self.store, GlobalRiskKernel(self.config.mandate))  # type: ignore[arg-type]
        self.applier = DecisionApplierV2(self.kernel)
        self.client = EtoroPublicApiDemoClientV2()

    def close(self) -> None:
        self.store.close()

    def run_once(self, limit: int = 20) -> int:
        applied = 0
        for row in self.queue.decided(limit):
            if row["role"] != AIRole.PORTFOLIO_DECIDER.value:
                continue
            packet = decode_dataclass(DecisionPacketV2, row["packet"])
            output = decode_dataclass(AIIntentOutputV2, row["output"])
            output.validate(packet)
            now = datetime.now(timezone.utc)
            if output.action.value == "HOLD":
                # HOLD is a complete autonomous decision and requires no broker read.
                self.queue.mark_applied(str(row["packet_id"]), now=now)
                applied += 1
                continue
            symbol = output.symbol
            if symbol is None and packet.position is not None:
                symbol = str(packet.position.get("symbol", ""))
            if not symbol or symbol not in self.config.symbols:
                self.queue.mark_applied(str(row["packet_id"]), now=now)
                continue
            truth = _broker_truth(
                self.store,
                self.client,
                initial_cash=self.config.initial_cash_usd,
                now=now,
            )
            quote = _quote(
                self.client,
                symbol=symbol,
                instrument_id=self.config.symbols[symbol],
                broker_hash=truth.snapshot_hash,
                received_at=now,
            )
            self.applier.apply(packet, output, quote=quote, broker=truth, now=now)
            self.queue.mark_applied(str(row["packet_id"]), now=now)
            applied += 1
        return applied

    def run_forever(self, interval_seconds: int = 5) -> None:
        if interval_seconds < 1:
            raise ValueError("decision apply interval must be positive")
        while True:
            try:
                self.run_once()
            except Exception:
                pass
            time.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply validated v2 AI decisions through deterministic kernel")
    parser.add_argument("--config", required=True)
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    worker = DecisionApplyWorkerV2(args.config)
    try:
        if args.once:
            print(f"V2_AI_APPLIED={worker.run_once()}")
        else:
            worker.run_forever(args.interval)
    finally:
        worker.close()


if __name__ == "__main__":
    main()
