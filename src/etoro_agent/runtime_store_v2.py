from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from typing import Any

from .domain_v2 import (
    BrokerOrder,
    DomainEvent,
    Fill,
    OrderStatus,
    PositionState,
    ReconciliationCase,
)
from .runtime_store_impl_v2 import RuntimeStoreV2 as _RuntimeStoreV2


class RuntimeStoreV2(_RuntimeStoreV2):
    """Canonical SQLite v2 store with compatibility and atomic fill projection helpers."""

    def queue_decision(
        self,
        decision_id: str,
        packet_hash: str,
        decision: Mapping[str, Any],
        *,
        created_at: datetime,
        expires_at: datetime,
    ) -> bool:
        """Compatibility name for the canonical enqueue operation."""
        return self.enqueue_decision(
            decision_id,
            packet_hash,
            decision,
            created_at=created_at,
            expires_at=expires_at,
        )

    def fill_exists(self, idempotency_key: str) -> bool:
        if not idempotency_key.strip():
            raise ValueError("fill idempotency key is required")
        return (
            self.db.execute(
                "SELECT 1 FROM v2_fills WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            is not None
        )

    def reconciliation_case(self, order_command_id: str) -> ReconciliationCase | None:
        row = self.db.execute(
            """SELECT case_id,order_command_id,status,opened_at,updated_at,
                      attempts,broker_snapshot_hash,detail
               FROM v2_reconciliation_cases WHERE order_command_id=?""",
            (order_command_id,),
        ).fetchone()
        if row is None:
            return None
        return ReconciliationCase(
            case_id=str(row[0]),
            order_command_id=str(row[1]),
            status=str(row[2]),
            opened_at=datetime.fromisoformat(str(row[3])),
            updated_at=datetime.fromisoformat(str(row[4])),
            attempts=int(row[5]),
            broker_snapshot_hash=str(row[6]),
            detail=str(row[7]),
        )

    @staticmethod
    def _save_reconciliation_case_tx(tx: Any, case: ReconciliationCase) -> None:
        tx.execute(
            """INSERT INTO v2_reconciliation_cases(
                   case_id,order_command_id,status,attempts,broker_snapshot_hash,
                   detail,opened_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(order_command_id) DO UPDATE SET
                 status=excluded.status,attempts=excluded.attempts,
                 broker_snapshot_hash=excluded.broker_snapshot_hash,
                 detail=excluded.detail,updated_at=excluded.updated_at""",
            (
                case.case_id,
                case.order_command_id,
                case.status,
                case.attempts,
                case.broker_snapshot_hash,
                case.detail,
                case.opened_at.isoformat(),
                case.updated_at.isoformat(),
            ),
        )

    def save_reconciliation_bundle(
        self,
        order: BrokerOrder,
        case: ReconciliationCase,
        event: DomainEvent,
    ) -> None:
        with self.atomic() as tx:
            updated = tx.execute(
                """UPDATE v2_broker_orders SET state_json=?,updated_at=?
                   WHERE order_command_id=?""",
                (
                    self._json(asdict(order)),
                    event.processing_time.isoformat(),
                    order.order_command_id,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("broker order missing during reconciliation")
            if order.status is OrderStatus.RECONCILED_ABSENT:
                self._release_risk_reservation_tx(
                    tx,
                    order.order_command_id,
                    event.processing_time,
                )
            self._save_reconciliation_case_tx(tx, case)
            self._append_event_tx(tx, event)

    def save_fill_position_bundle(
        self,
        fill: Fill,
        order: BrokerOrder,
        position: PositionState,
        fill_event: DomainEvent,
        position_event: DomainEvent,
        reconciliation_case: ReconciliationCase | None = None,
        reconciliation_event: DomainEvent | None = None,
    ) -> bool:
        """Atomically persist fill, order projection, position projection and both events."""
        if fill.order_command_id != order.order_command_id:
            raise ValueError("fill/order identity mismatch")
        if (reconciliation_case is None) != (reconciliation_event is None):
            raise ValueError("reconciliation case and event must be supplied together")
        with self.atomic() as tx:
            inserted = tx.execute(
                """INSERT INTO v2_fills(
                       fill_id,idempotency_key,order_command_id,fill_json,event_time
                   ) VALUES(?,?,?,?,?) ON CONFLICT(idempotency_key) DO NOTHING""",
                (
                    fill.fill_id,
                    fill.idempotency_key,
                    fill.order_command_id,
                    self._json(asdict(fill)),
                    fill.event_time.isoformat(),
                ),
            )
            if inserted.rowcount == 0:
                return False
            updated = tx.execute(
                """UPDATE v2_broker_orders
                   SET state_json=?,updated_at=? WHERE order_command_id=?""",
                (
                    self._json(asdict(order)),
                    fill_event.processing_time.isoformat(),
                    order.order_command_id,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("broker order missing")
            if order.status in {OrderStatus.FILLED, OrderStatus.RECONCILED_FILLED}:
                self._release_risk_reservation_tx(
                    tx,
                    order.order_command_id,
                    fill_event.processing_time,
                )
            self._append_event_tx(tx, fill_event)
            self.save_position_tx(tx, position, position_event)
            if reconciliation_case is not None and reconciliation_event is not None:
                self._save_reconciliation_case_tx(tx, reconciliation_case)
                self._append_event_tx(tx, reconciliation_event)
            return True
