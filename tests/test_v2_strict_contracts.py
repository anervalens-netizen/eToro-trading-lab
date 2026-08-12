from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from etoro_agent.ai_v2 import AIAction, AIIntentOutputV2, DecisionPacketV2
from etoro_agent.codec_v2 import decode_dataclass, decode_value
from etoro_agent.domain_v2 import (
    BrokerOrder,
    OrderCommand,
    OrderStatus,
    PositionState,
    Side,
)

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def position() -> PositionState:
    return PositionState(
        "position-1",
        "master_1000",
        "trend",
        "D_sol_plus_critic",
        "v2",
        "intent-1",
        "AAPL",
        Side.BUY,
        Decimal("1"),
        Decimal("100"),
        NOW,
        NOW,
        Decimal("95"),
        Decimal("110"),
        Decimal("0.05"),
        Decimal("0.10"),
        3600,
        NOW + timedelta(hours=1),
    )


def command() -> OrderCommand:
    return OrderCommand(
        order_command_id="command-1",
        intent_id="intent-1",
        proposal_id="proposal-1",
        client_order_id="client-1",
        portfolio_id="master_1000",
        symbol="AAPL",
        side=Side.BUY,
        amount_usd=Decimal("100"),
        quantity=None,
        reduce_only=False,
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=60),
        idempotency_key="open:intent-1",
        correlation_id="intent-1",
        intent_hash="a" * 64,
        reference_entry=Decimal("100"),
        min_acceptable_entry=Decimal("99"),
        max_acceptable_entry=Decimal("101"),
        stop_loss_fraction=Decimal("0.02"),
        take_profit_fraction=Decimal("0.04"),
        max_slippage_bps=Decimal("10"),
        max_loss_usd=Decimal("5"),
        available_loss_budget_usd=Decimal("10"),
        available_notional_budget_usd=Decimal("1000"),
        available_order_slots=1,
        proposal_source="sol_master_open",
        risk_config_hash="b" * 64,
    )


def packet(*, position_payload: dict[str, object] | None = None) -> DecisionPacketV2:
    return DecisionPacketV2(
        "packet-1",
        NOW.isoformat(),
        (NOW + timedelta(minutes=5)).isoformat(),
        "D_sol_plus_critic",
        "POSITION_REVIEW" if position_payload is not None else "ENTRY_REVIEW",
        ("market-1",),
        "feature-1",
        "c" * 64,
        "d" * 64,
        {},
        (),
        position_payload,
        ("feature-1",),
    )


def output(action: AIAction, **changes: object) -> AIIntentOutputV2:
    values: dict[str, object] = {
        "action": action,
        "self_reported_confidence": Decimal("0.5"),
        "self_reported_uncertainty": Decimal("0.5"),
        "reason_codes": ("bounded",),
        "rationale": "bounded decision",
        "evidence_refs": ("feature-1",),
        "hypothesis_id": "trend",
        "lane_id": "D_sol_plus_critic",
    }
    values.update(changes)
    return AIIntentOutputV2(**values)  # type: ignore[arg-type]


class V2StrictCodecTests(unittest.TestCase):
    def test_scalar_decoding_does_not_coerce_types_or_nonfinite_values(self) -> None:
        for annotation, value in (
            (bool, "false"),
            (bool, 0),
            (int, True),
            (int, "1"),
            (str, 1),
            (Decimal, "NaN"),
            (Decimal, "Infinity"),
            (Decimal, 1.25),
            (float, float("inf")),
        ):
            with (
                self.subTest(annotation=annotation, value=value),
                self.assertRaises((TypeError, ValueError)),
            ):
                decode_value(annotation, value)
        self.assertIs(decode_value(bool, False), False)
        self.assertEqual(decode_value(Decimal, "1.25"), Decimal("1.25"))
        self.assertEqual(decode_value(datetime, NOW), NOW)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            decode_value(datetime, datetime(2026, 8, 12, 12, 0))

    def test_dataclass_codec_rejects_unknown_and_missing_fields(self) -> None:
        valid = {
            "order_command_id": "command-1",
            "client_order_id": "client-1",
            "status": "CREATED",
        }
        decoded = decode_dataclass(BrokerOrder, valid)
        self.assertEqual(decoded.status, OrderStatus.CREATED)
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            decode_dataclass(BrokerOrder, {**valid, "unexpected": "economic"})
        with self.assertRaisesRegex(ValueError, "missing required"):
            decode_dataclass(BrokerOrder, {"status": "CREATED"})


class V2StrictDomainTests(unittest.TestCase):
    def test_position_identity_temporal_and_directional_corpus(self) -> None:
        valid = position()
        invalid_changes = (
            {"position_id": ""},
            {"lane_id": ""},
            {"expires_at": NOW - timedelta(seconds=1)},
            {"entry_processing_time": NOW - timedelta(seconds=1)},
            {"stop_price": Decimal("101")},
            {"take_profit_price": Decimal("99")},
            {"stop_fraction": Decimal("Infinity")},
            {"last_mark": Decimal("NaN")},
        )
        for changes in invalid_changes:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(valid, **changes)

        short = replace(
            valid,
            side=Side.SELL,
            stop_price=Decimal("105"),
            take_profit_price=Decimal("90"),
        )
        self.assertEqual(short.side, Side.SELL)
        with self.assertRaisesRegex(ValueError, "short position"):
            replace(short, stop_price=Decimal("99"))

    def test_order_and_broker_order_reject_temporal_identity_and_nonfinite_state(self) -> None:
        with self.assertRaisesRegex(ValueError, "expiry"):
            replace(command(), expires_at=NOW - timedelta(seconds=1))
        with self.assertRaisesRegex(ValueError, "symbol"):
            replace(command(), symbol="")
        for kwargs in (
            {"order_command_id": "", "client_order_id": "client", "status": OrderStatus.CREATED},
            {
                "order_command_id": "command",
                "client_order_id": "client",
                "status": OrderStatus.CREATED,
                "filled_quantity": Decimal("Infinity"),
            },
            {
                "order_command_id": "command",
                "client_order_id": "client",
                "status": OrderStatus.ACKNOWLEDGED,
                "submitted_at": NOW,
                "acknowledged_at": NOW,
                "last_update_at": NOW,
            },
            {
                "order_command_id": "command",
                "client_order_id": "client",
                "status": OrderStatus.CREATED,
                "submitted_at": NOW,
                "last_update_at": NOW - timedelta(seconds=1),
            },
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                BrokerOrder(**kwargs)


class V2StrictAIActionTests(unittest.TestCase):
    def test_hold_rejects_every_foreign_action_field(self) -> None:
        contaminated = (
            {"candidate_id": "candidate"},
            {"symbol": "AAPL"},
            {"side": "buy"},
            {"amount_usd": Decimal("1")},
            {"stop_loss_fraction": Decimal("0.1")},
            {"take_profit_fraction": Decimal("0.1")},
            {"max_holding_seconds": 300},
            {"max_slippage_bps": Decimal("1")},
            {"partial_close_fraction": Decimal("0.5")},
        )
        for fields in contaminated:
            with self.subTest(fields=fields), self.assertRaisesRegex(ValueError, "HOLD contains"):
                output(AIAction.HOLD, **fields).validate(packet())
        output(AIAction.HOLD).validate(packet())

    def test_position_actions_are_discriminated(self) -> None:
        position_packet = packet(position_payload={"position_id": "position-1"})
        output(AIAction.CLOSE).validate(position_packet)
        output(AIAction.PARTIAL_CLOSE, partial_close_fraction=Decimal("0.5")).validate(
            position_packet
        )
        for action, fields in (
            (AIAction.CLOSE, {"candidate_id": "candidate"}),
            (AIAction.CLOSE, {"partial_close_fraction": Decimal("0.5")}),
            (AIAction.PARTIAL_CLOSE, {"partial_close_fraction": Decimal("0.5"), "symbol": "AAPL"}),
            (
                AIAction.PARTIAL_CLOSE,
                {"partial_close_fraction": Decimal("0.5"), "candidate_id": "c"},
            ),
        ):
            with (
                self.subTest(action=action, fields=fields),
                self.assertRaisesRegex(ValueError, "contains fields"),
            ):
                output(action, **fields).validate(position_packet)


if __name__ == "__main__":
    unittest.main()
