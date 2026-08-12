from __future__ import annotations

import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .backtest_v2 import (
    CanonicalCandidatePolicyV2,
    HistoricalBar,
    KernelBacktester,
    KernelBacktestResult,
    SignalFactory,
)
from .candidates_v2 import CandidateEngineV2
from .domain_v2 import IntentEnvelope, PositionStatus
from .exits_v2 import BarObservation
from .kernel_v2 import UnifiedTradingKernel
from .risk_v2 import CapitalMandate, GlobalRiskKernel
from .runtime_store_v2 import RuntimeStoreV2
from .shadow_v2 import ShadowBrokerAdapterV2
from .strategy_release_v2 import VerifiedStrategyReleaseV2


@dataclass(frozen=True)
class ParityResult:
    passed: bool
    historical_pnl: Decimal
    shadow_pnl: Decimal
    historical_closed: int
    shadow_closed: int
    historical_fills: int
    shadow_fills: int
    detail: str
    candidate_hashes_match: bool = False
    promotion_eligible: bool = False
    strategy_release_id: str | None = None


class ParityHarnessV2:
    def __init__(
        self,
        mandate: CapitalMandate,
        *,
        starting_equity: Decimal,
        spread_bps: Decimal = Decimal("5"),
        slippage_bps: Decimal = Decimal("2"),
        fee_bps: Decimal = Decimal("0"),
        financing_bps_per_day: Decimal = Decimal("0"),
    ) -> None:
        self.mandate = mandate
        self.starting_equity = starting_equity
        self.spread_bps = spread_bps
        self.slippage_bps = slippage_bps
        self.fee_bps = fee_bps
        self.financing_bps_per_day = financing_bps_per_day

    def historical(
        self, symbol: str, bars: Sequence[HistoricalBar], signal_factory: SignalFactory
    ) -> KernelBacktestResult:
        return KernelBacktester(
            self.mandate,
            spread_bps=self.spread_bps,
            slippage_bps=self.slippage_bps,
            fee_bps=self.fee_bps,
            financing_bps_per_day=self.financing_bps_per_day,
        ).run(symbol, bars, self.starting_equity, signal_factory)

    def shadow(
        self, symbol: str, bars: Sequence[HistoricalBar], signal_factory: SignalFactory
    ) -> tuple[Decimal, int, int]:
        with tempfile.TemporaryDirectory(prefix="etoro-parity-shadow-") as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            kernel = UnifiedTradingKernel(store, GlobalRiskKernel(self.mandate))
            broker = ShadowBrokerAdapterV2(
                kernel,
                starting_equity=self.starting_equity,
                spread_bps=self.spread_bps,
                slippage_bps=self.slippage_bps,
                fee_bps=self.fee_bps,
                financing_bps_per_day=self.financing_bps_per_day,
            )
            pending: IntentEnvelope | None = None
            for index, bar in enumerate(bars):
                market_hash = KernelBacktester._snapshot_hash(bar, index)
                quote_open = broker.quote(symbol, bar.open, bar.event_time, market_hash=market_hash)
                broker.evaluate_and_execute_exit(
                    symbol,
                    quote_open,
                    bar=BarObservation(bar.event_time, bar.open, bar.high, bar.low, bar.close),
                )
                if pending is not None and not store.positions(
                    pending.portfolio_id, open_only=True
                ):
                    broker.execute_open(pending, quote_open)
                    pending = None
                quote_close = broker.quote(
                    symbol, bar.close, bar.event_time, market_hash=market_hash
                )
                candidate = signal_factory(
                    index, bars[: index + 1], quote_close.bid, quote_close.ask, market_hash
                )
                if candidate is not None and not store.positions(open_only=True):
                    pending = candidate
            last = bars[-1]
            quote_final = broker.quote(
                symbol,
                last.close,
                last.event_time,
                market_hash=KernelBacktester._snapshot_hash(last, len(bars) - 1),
            )
            broker.evaluate_and_execute_exit(symbol, quote_final, end_of_test=True)
            ending, _, _, _ = broker.economics({symbol: last.close})
            closed = sum(item.status is PositionStatus.CLOSED for item in store.positions())
            fills = int(store.db.execute("SELECT COUNT(*) FROM v2_fills").fetchone()[0])
            store.close()
            return ending - self.starting_equity, closed, fills

    def compare(
        self,
        symbol: str,
        bars: Sequence[HistoricalBar],
        signal_factory: SignalFactory,
        *,
        tolerance_usd: Decimal = Decimal("0.000001"),
    ) -> ParityResult:
        historical = self.historical(symbol, bars, signal_factory)
        shadow_pnl, shadow_closed, shadow_fills = self.shadow(symbol, bars, signal_factory)
        passed = (
            abs(historical.pnl - shadow_pnl) <= tolerance_usd
            and historical.closed_positions == shadow_closed
            and historical.fills == shadow_fills
        )
        return ParityResult(
            passed,
            historical.pnl,
            shadow_pnl,
            historical.closed_positions,
            shadow_closed,
            historical.fills,
            shadow_fills,
            (
                "benchmark-only economic parity; not promotion evidence"
                if passed
                else "benchmark-only historical/shadow divergence"
            ),
        )

    def compare_canonical(
        self,
        symbol: str,
        bars: Sequence[HistoricalBar],
        engine: CandidateEngineV2,
        release: VerifiedStrategyReleaseV2,
        *,
        portfolio_id: str = "canonical-parity",
        lane_id: str = "canonical-candidate-engine",
        amount_usd: Decimal = Decimal("100"),
        tolerance_usd: Decimal = Decimal("0.000001"),
    ) -> ParityResult:
        historical = KernelBacktester(
            self.mandate,
            spread_bps=self.spread_bps,
            slippage_bps=self.slippage_bps,
            fee_bps=self.fee_bps,
            financing_bps_per_day=self.financing_bps_per_day,
        ).run_canonical(
            symbol,
            bars,
            self.starting_equity,
            engine,
            release,
            portfolio_id=portfolio_id,
            lane_id=lane_id,
            amount_usd=amount_usd,
        )
        shadow_policy = CanonicalCandidatePolicyV2(
            engine,
            release,
            portfolio_id=portfolio_id,
            lane_id=lane_id,
            amount_usd=amount_usd,
        ).bind_symbol(symbol)
        shadow_pnl, shadow_closed, shadow_fills = self.shadow(symbol, bars, shadow_policy)
        candidate_hashes_match = historical.candidate_batch_hashes == tuple(
            shadow_policy.batch_hashes
        )
        economic_match = (
            abs(historical.pnl - shadow_pnl) <= tolerance_usd
            and historical.closed_positions == shadow_closed
            and historical.fills == shadow_fills
        )
        passed = candidate_hashes_match and economic_match
        return ParityResult(
            passed,
            historical.pnl,
            shadow_pnl,
            historical.closed_positions,
            shadow_closed,
            historical.fills,
            shadow_fills,
            "canonical candidate/economic parity" if passed else "canonical parity divergence",
            candidate_hashes_match,
            passed and historical.promotion_eligible,
            release.strategy_release_id,
        )
