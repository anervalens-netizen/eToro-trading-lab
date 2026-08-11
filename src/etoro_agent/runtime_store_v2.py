from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any, Mapping

from .domain_v2 import BrokerOrder, DomainEvent, Fill, PositionState
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

    def save_fill_position_bundle(
        self,
        fill: Fill,
        order: BrokerOrder,
        position: PositionState,
        fill_event: DomainEvent,
        position_event: DomainEvent,
    ) -> bool:
        """Atomically persist fill, order projection, position projection and both events."""
        if fill.order_command_id != order.order_command_id:
            raise ValueError("fill/order identity mismatch")
        with self.atomic() as tx:
            inserted = tx.execute(
                """INSERT OR IGNORE INTO v2_fills(
                       fill_id,idempotency_key,order_command_id,fill_json,event_time
                   ) VALUES(?,?,?,?,?)""",
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
            self._append_event_tx(tx, fill_event)
            self.save_position_tx(tx, position, position_event)
            return True
