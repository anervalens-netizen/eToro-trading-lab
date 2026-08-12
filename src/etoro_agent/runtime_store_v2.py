from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Any

from .domain_v2 import (
    BrokerOrder,
    DomainEvent,
    Fill,
    OrderStatus,
    PositionState,
    ReconciliationCase,
    utc,
)
from .runtime_store_impl_v2 import RuntimeStoreV2 as _RuntimeStoreV2


class RuntimeStoreV2(_RuntimeStoreV2):
    """Canonical SQLite v2 store with compatibility and atomic fill projection helpers."""

    def trading_state_snapshot(self) -> Mapping[str, Any]:
        state_row = self.db.execute(
            "SELECT value,updated_at FROM v2_state WHERE key='trading_state'"
        ).fetchone()
        version_row = self.db.execute(
            "SELECT value FROM v2_state WHERE key='trading_state_version'"
        ).fetchone()
        return {
            "state": "LOCKED" if state_row is None else str(state_row[0]),
            "version": 0 if version_row is None else int(version_row[0]),
            "changed_at": None if state_row is None else datetime.fromisoformat(str(state_row[1])),
        }

    def lock_and_invalidate_unstarted(
        self,
        *,
        actor: str,
        reason: str,
        at: datetime | None = None,
    ) -> int:
        """Atomically lock trading and reject every command not yet submitted."""

        if not actor.strip() or not reason.strip():
            raise ValueError("gate lock actor/reason are required")
        current = utc(at or datetime.now(UTC))
        invalidated = 0
        with self.atomic() as tx:
            rows = tx.execute(
                "SELECT state_json FROM v2_broker_orders ORDER BY order_command_id"
            ).fetchall()
            for row in rows:
                order = self._broker_order_from_json(str(row[0]))
                if order.status is not OrderStatus.RISK_APPROVED:
                    continue
                rejected = replace(
                    order,
                    status=OrderStatus.REJECTED,
                    last_update_at=current,
                    failure_reason="execution gate absent before broker send",
                )
                tx.execute(
                    "UPDATE v2_broker_orders SET state_json=?,updated_at=? WHERE order_command_id=?",
                    (
                        self._json(asdict(rejected)),
                        current.isoformat(),
                        order.order_command_id,
                    ),
                )
                self._release_risk_reservation_tx(tx, order.order_command_id, current)
                tx.execute(
                    """UPDATE v2_outbox SET delivered_at=?,claimed_by=NULL,claim_token=NULL,
                       lease_expires_at=NULL,last_error_type='ExecutionGateAbsent'
                       WHERE delivered_at IS NULL
                         AND json_extract(payload_json,'$.order_command_id')=?""",
                    (current.isoformat(), order.order_command_id),
                )
                key = f"gate-rejected:{order.order_command_id}"
                self._append_event_tx(
                    tx,
                    DomainEvent(
                        event_id="evt-" + hashlib.sha256(key.encode()).hexdigest()[:24],
                        event_type="OrderRejectedBeforeSend",
                        schema_version=2,
                        event_time=current,
                        processing_time=current,
                        idempotency_key=key,
                        causation_id=order.order_command_id,
                        correlation_id=order.order_command_id,
                        payload={
                            "order_command_id": order.order_command_id,
                            "reason": "execution gate absent",
                            "network_write_attempted": False,
                        },
                    ),
                )
                invalidated += 1

            row = tx.execute("SELECT value FROM v2_state WHERE key='trading_state'").fetchone()
            previous = "LOCKED" if row is None else str(row[0])
            if row is None or previous != "LOCKED":
                version_row = tx.execute(
                    "SELECT value FROM v2_state WHERE key='trading_state_version'"
                ).fetchone()
                version = (0 if version_row is None else int(version_row[0])) + 1
                for key, value in (
                    ("trading_state", "LOCKED"),
                    ("trading_state_version", str(version)),
                ):
                    tx.execute(
                        """INSERT INTO v2_state(key,value,updated_at) VALUES(?,?,?)
                           ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                           updated_at=excluded.updated_at""",
                        (key, value, current.isoformat()),
                    )
                event_key = f"trading-state:{version}:LOCKED"
                self._append_event_tx(
                    tx,
                    DomainEvent(
                        event_id="evt-" + hashlib.sha256(event_key.encode()).hexdigest()[:24],
                        event_type="TradingStateChanged",
                        schema_version=2,
                        event_time=current,
                        processing_time=current,
                        idempotency_key=event_key,
                        causation_id="",
                        correlation_id="trading-state",
                        payload={
                            "previous_state": previous,
                            "state": "LOCKED",
                            "actor": actor,
                            "reason": reason[:500],
                            "version": version,
                            "invalidated_unstarted_orders": invalidated,
                        },
                    ),
                )
        return invalidated

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

    def fills_for_order(self, order_command_id: str) -> tuple[Fill, ...]:
        rows = self.db.execute(
            """SELECT idempotency_key FROM v2_fills
               WHERE order_command_id=? ORDER BY event_time,fill_id""",
            (order_command_id,),
        ).fetchall()
        fills = tuple(self.fill_by_idempotency(str(row[0])) for row in rows)
        return tuple(fill for fill in fills if fill is not None)

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
