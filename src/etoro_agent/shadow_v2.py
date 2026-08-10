from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Mapping

from .domain_v2 import ExitReason, Fill, IntentEnvelope, PositionStatus, QuoteProvenance, Side, ZERO
from .exits_v2 import BarObservation, ExitContext
from .kernel_v2 import UnifiedTradingKernel
from .risk_v2 import BrokerTruth

BPS = Decimal("10000")


class ShadowBrokerAdapterV2:
    """Deterministic broker simulator; it changes transport only, never economic semantics."""

    def __init__(
        self,
        kernel: UnifiedTradingKernel,
        *,
        starting_equity: Decimal,
        spread_bps: Decimal = Decimal("5"),
        slippage_bps: Decimal = Decimal("2"),
        fee_bps: Decimal = ZERO,
    ) -> None:
        if starting_equity <= ZERO:
            raise ValueError("starting equity must be positive")
        self.kernel = kernel
        self.store = kernel.store
        self.starting_equity = starting_equity
        self.spread_bps = spread_bps
        self.slippage_bps = slippage_bps
        self.fee_bps = fee_bps
        self.sequence = 0

    def economics(self, marks: Mapping[str, Decimal]) -> tuple[Decimal, Decimal, int, Decimal]:
        positions = self.store.positions()
        realized = sum((position.realized_pnl for position in positions), ZERO)
        unrealized = ZERO
        gross = ZERO
        for position in positions:
            if position.status is not PositionStatus.OPEN:
                continue
            mark = marks.get(position.symbol, position.last_mark or position.entry_price)
            unrealized += position.pnl_at(mark)
            gross += position.gross_exposure(mark)
        equity = self.starting_equity + realized + unrealized
        open_count = sum(item.status is PositionStatus.OPEN for item in positions)
        return equity, gross, open_count, max(ZERO, equity - gross)

    def quote(self, symbol: str, mid: Decimal, at: datetime, *, market_hash: str) -> QuoteProvenance:
        half = self.spread_bps / Decimal("2") / BPS
        equity, gross, count, cash = self.economics({symbol: mid})
        self.sequence += 1
        broker_hash = hashlib.sha256(
            f"shadow:{self.sequence}:{equity}:{gross}:{count}:{cash}".encode()
        ).hexdigest()
        return QuoteProvenance(
            symbol,
            mid * (Decimal("1") - half),
            mid * (Decimal("1") + half),
            at,
            at,
            "shadow-broker-v2",
            str(self.sequence),
            market_hash,
            broker_hash,
        )

    def broker_truth(self, quote: QuoteProvenance, marks: Mapping[str, Decimal]) -> BrokerTruth:
        equity, gross, count, cash = self.economics(marks)
        return BrokerTruth(
            equity,
            max(self.starting_equity, equity),
            cash,
            gross,
            gross,
            count,
            ZERO,
            equity - self.starting_equity,
            equity - self.starting_equity,
            equity - self.starting_equity,
            quote.broker_snapshot_hash,
            quote.quote_received_at,
        )

    def execute_open(self, intent: IntentEnvelope, quote: QuoteProvenance) -> bool:
        truth = self.broker_truth(quote, {intent.symbol: quote.mid})
        risk, command = self.kernel.submit_open_intent(intent, quote, truth, now=quote.quote_received_at)
        if not risk.approved or command is None:
            return False
        self.kernel.begin_submit(command.order_command_id, quote.quote_received_at)
        broker_position_id = f"shadow-{intent.intent_id}"
        self.kernel.acknowledge(
            command.order_command_id,
            at=quote.quote_received_at,
            broker_order_id=f"shadow-order-{command.order_command_id}",
            broker_position_id=broker_position_id,
        )
        impact = self.slippage_bps / BPS
        price = (
            quote.ask * (Decimal("1") + impact)
            if intent.side is Side.BUY
            else quote.bid * (Decimal("1") - impact)
        )
        quantity = intent.amount_usd / price
        self.kernel.apply_fill(
            Fill(
                f"fill-{command.order_command_id}",
                command.order_command_id,
                command.client_order_id,
                f"shadow-order-{command.order_command_id}",
                broker_position_id,
                intent.symbol,
                intent.side,
                quantity,
                price,
                intent.amount_usd * self.fee_bps / BPS,
                ZERO,
                quote.quote_observed_at,
                quote.quote_received_at,
                f"shadow-fill:{command.order_command_id}",
            ),
            final=True,
        )
        return True

    def evaluate_and_execute_exit(
        self,
        symbol: str,
        quote: QuoteProvenance,
        *,
        bar: BarObservation | None = None,
        agent_close: bool = False,
        reduce_only_forced: bool = False,
        data_valid: bool = True,
        strategy_invalidated: bool = False,
        overnight_exit_due: bool = False,
        end_of_test: bool = False,
    ) -> int:
        closed = 0
        for position in tuple(self.store.positions(open_only=True)):
            if position.symbol != symbol:
                continue
            decision = self.kernel.evaluate_exit(
                position,
                ExitContext(
                    quote.quote_received_at,
                    quote,
                    bar,
                    agent_close,
                    reduce_only_forced,
                    data_valid,
                    strategy_invalidated,
                    overnight_exit_due,
                    end_of_test,
                ),
            )
            if not decision.should_exit or decision.execution_price is None:
                continue
            reason = decision.reason or ExitReason.STRATEGY_INVALIDATION
            command = self.kernel.create_close_command(position, now=quote.quote_received_at, reason=reason)
            self.kernel.begin_submit(command.order_command_id, quote.quote_received_at)
            self.kernel.acknowledge(
                command.order_command_id,
                at=quote.quote_received_at,
                broker_order_id=f"shadow-close-{command.order_command_id}",
                broker_position_id=position.broker_position_id or position.position_id,
            )
            self.kernel.apply_fill(
                Fill(
                    f"fill-{command.order_command_id}",
                    command.order_command_id,
                    command.client_order_id,
                    f"shadow-close-{command.order_command_id}",
                    position.broker_position_id or position.position_id,
                    position.symbol,
                    command.side,
                    position.quantity,
                    decision.execution_price,
                    position.quantity * decision.execution_price * self.fee_bps / BPS,
                    ZERO,
                    quote.quote_observed_at,
                    quote.quote_received_at,
                    f"shadow-close-fill:{command.order_command_id}",
                ),
                final=True,
                exit_reason=reason,
            )
            closed += 1
        return closed
