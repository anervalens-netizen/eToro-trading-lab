from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .codec_v2 import decode_dataclass
from .domain_v2 import (
    BrokerOrder,
    DomainEvent,
    Fill,
    IntentEnvelope,
    OrderCommand,
    OrderStatus,
    PositionState,
    ReconciliationCase,
    canonical_json,
)
from .postgres_store_v2 import PostgresStoreV2


class PostgresRuntimeStoreV2(PostgresStoreV2):
    """Production v2 store implementing the unified kernel persistence contract."""

    @staticmethod
    def _json(value: object) -> str:
        return canonical_json(value)

    @staticmethod
    def _mapping(value: object) -> Mapping[str, Any]:
        if isinstance(value, Mapping):
            return value
        if isinstance(value, str):
            parsed = json.loads(value)
            if isinstance(parsed, Mapping):
                return parsed
        raise ValueError("stored JSON object is invalid")

    def state_get(self, key: str, default: str = "") -> str:
        if key == "trading_state":
            with self.connection.cursor() as cursor:
                cursor.execute("SELECT state FROM v2_trading_state WHERE singleton=TRUE")
                row = cursor.fetchone()
                return str(row[0]) if row else default
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT value FROM v2_meta WHERE key=%s", (key,))
            row = cursor.fetchone()
            return str(row[0]) if row else default

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
        current = (at or datetime.now(UTC)).astimezone(UTC)
        invalidated = 0
        with self.transaction() as cursor:
            cursor.execute(
                """SELECT state FROM v2_broker_orders
                   WHERE status='RISK_APPROVED' ORDER BY order_command_id FOR UPDATE"""
            )
            orders = tuple(
                decode_dataclass(BrokerOrder, self._mapping(row[0])) for row in cursor.fetchall()
            )
            for order in orders:
                rejected = replace(
                    order,
                    status=OrderStatus.REJECTED,
                    last_update_at=current,
                    failure_reason="execution gate absent before broker send",
                )
                cursor.execute(
                    """UPDATE v2_broker_orders SET status='REJECTED',state=%s::jsonb,
                       updated_at=%s WHERE order_command_id=%s AND status='RISK_APPROVED'""",
                    (self._json(asdict(rejected)), current, order.order_command_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("unstarted order changed during gate invalidation")
                self._release_risk_reservation_tx(cursor, order.order_command_id, current)
                cursor.execute(
                    """UPDATE v2_outbox SET delivered_at=%s,claimed_by=NULL,claim_token=NULL,
                       lease_expires_at=NULL,last_error_type='ExecutionGateAbsent'
                       WHERE delivered_at IS NULL
                         AND payload->>'order_command_id'=%s""",
                    (current, order.order_command_id),
                )
                key = f"gate-rejected:{order.order_command_id}"
                self.append_event_tx(
                    cursor,
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

            cursor.execute(
                "SELECT state,version FROM v2_trading_state WHERE singleton=TRUE FOR UPDATE"
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("trading state singleton is missing")
            previous = str(row[0])
            if previous != "LOCKED":
                version = int(row[1]) + 1
                cursor.execute(
                    """UPDATE v2_trading_state SET state='LOCKED',actor=%s,reason=%s,
                       version=%s,changed_at=%s WHERE singleton=TRUE""",
                    (actor, reason[:500], version, current),
                )
                event_key = f"trading-state:{version}:LOCKED"
                self.append_event_tx(
                    cursor,
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

    def state_set(self, key: str, value: str, at: datetime | None = None) -> None:
        if key == "trading_state":
            self.set_trading_state(value, actor="runtime", reason="state_set")
            return
        now = (at or datetime.now(UTC)).astimezone(UTC)
        with self.transaction() as cursor:
            cursor.execute(
                """INSERT INTO v2_meta(key,value,updated_at) VALUES(%s,%s,%s)
                   ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at""",
                (key, value, now),
            )

    def set_trading_state(
        self,
        value: str,
        *,
        actor: str,
        reason: str,
        at: datetime | None = None,
    ) -> bool:
        if value not in {"ACTIVE", "HALT_NEW", "REDUCE_ONLY", "LOCKED"}:
            raise ValueError("invalid trading state")
        if not actor.strip() or not reason.strip():
            raise ValueError("trading state actor/reason are required")
        current = (at or datetime.now(UTC)).astimezone(UTC)
        with self.transaction() as cursor:
            cursor.execute(
                """SELECT state,version FROM v2_trading_state
                   WHERE singleton=TRUE FOR UPDATE"""
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("trading state singleton is missing")
            previous = str(row[0])
            if previous == value:
                return False
            version = int(row[1]) + 1
            cursor.execute(
                """UPDATE v2_trading_state SET state=%s,actor=%s,reason=%s,
                   version=%s,changed_at=%s WHERE singleton=TRUE""",
                (value, actor, reason[:500], version, current),
            )
            idempotency_key = f"trading-state:{version}:{value}"
            self.append_event_tx(
                cursor,
                DomainEvent(
                    event_id=("evt-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]),
                    event_type="TradingStateChanged",
                    schema_version=2,
                    event_time=current,
                    processing_time=current,
                    idempotency_key=idempotency_key,
                    causation_id="",
                    correlation_id="trading-state",
                    payload={
                        "previous_state": previous,
                        "state": value,
                        "actor": actor,
                        "reason": reason[:500],
                        "version": version,
                    },
                ),
            )
            return True

    def save_intent(self, intent: IntentEnvelope) -> bool:
        body = self._json(asdict(intent))
        digest = hashlib.sha256(body.encode()).hexdigest()
        with self.transaction() as cursor:
            cursor.execute(
                """INSERT INTO v2_intents(intent_id,portfolio_id,lane_id,strategy_id,state,envelope,
                   envelope_hash,created_at,expires_at,updated_at)
                   VALUES(%s,%s,%s,%s,'ACTIVE',%s::jsonb,%s,%s,%s,%s)
                   ON CONFLICT(intent_id) DO NOTHING""",
                (
                    intent.intent_id,
                    intent.portfolio_id,
                    intent.lane_id,
                    intent.strategy_id,
                    body,
                    digest,
                    intent.created_at,
                    intent.expires_at,
                    intent.created_at,
                ),
            )
            created = cursor.rowcount == 1
            if not created:
                cursor.execute(
                    "SELECT envelope_hash FROM v2_intents WHERE intent_id=%s", (intent.intent_id,)
                )
                row = cursor.fetchone()
                if row is None or str(row[0]).strip() != digest:
                    raise ValueError("intent identifier cannot be rebound")
            return created

    def intent(self, intent_id: str) -> IntentEnvelope:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT envelope FROM v2_intents WHERE intent_id=%s", (intent_id,))
            row = cursor.fetchone()
        if row is None:
            raise ValueError("intent missing")
        return decode_dataclass(IntentEnvelope, self._mapping(row[0]))

    def intent_or_none(self, intent_id: str) -> IntentEnvelope | None:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT envelope FROM v2_intents WHERE intent_id=%s", (intent_id,))
            row = cursor.fetchone()
        return None if row is None else decode_dataclass(IntentEnvelope, self._mapping(row[0]))

    def save_order_bundle(
        self,
        command: OrderCommand,
        broker_order: BrokerOrder,
        event: DomainEvent,
        *,
        outbox_topic: str | None = None,
        outbox_payload: Mapping[str, Any] | None = None,
    ) -> bool:
        command_json = self._json(asdict(command))
        command_hash = hashlib.sha256(command_json.encode()).hexdigest()
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT order_command_id,command_hash FROM v2_order_commands WHERE idempotency_key=%s",
                (command.idempotency_key,),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if (
                    str(existing[0]) != command.order_command_id
                    or str(existing[1]).strip() != command_hash
                ):
                    raise ValueError("order idempotency key cannot be rebound")
                return False
            cursor.execute(
                """INSERT INTO v2_order_commands(
                   order_command_id,intent_id,proposal_id,client_order_id,portfolio_id,symbol,
                   reduce_only,idempotency_key,command,command_hash,created_at,expires_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)""",
                (
                    command.order_command_id,
                    command.intent_id,
                    command.proposal_id,
                    command.client_order_id,
                    command.portfolio_id,
                    command.symbol,
                    command.reduce_only,
                    command.idempotency_key,
                    command_json,
                    command_hash,
                    command.created_at,
                    command.expires_at,
                ),
            )
            cursor.execute(
                """INSERT INTO v2_broker_orders(order_command_id,status,broker_order_id,
                   broker_position_id,filled_quantity,average_fill_price,state,updated_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s)""",
                (
                    broker_order.order_command_id,
                    broker_order.status.value,
                    broker_order.broker_order_id,
                    broker_order.broker_position_id,
                    broker_order.filled_quantity,
                    broker_order.average_fill_price,
                    self._json(asdict(broker_order)),
                    event.processing_time,
                ),
            )
            self._reserve_open_risk_tx(cursor, command, event.processing_time)
            if outbox_topic is not None:
                outbox_id = (
                    f"outbox-{hashlib.sha256(command.idempotency_key.encode()).hexdigest()[:24]}"
                )
                cursor.execute(
                    """INSERT INTO v2_outbox(outbox_id,topic,payload,idempotency_key,created_at)
                       VALUES(%s,%s,%s::jsonb,%s,%s)""",
                    (
                        outbox_id,
                        outbox_topic,
                        self._json(dict(outbox_payload or {})),
                        command.idempotency_key,
                        event.processing_time,
                    ),
                )
            self.append_event_tx(cursor, event)
            return True

    @staticmethod
    def _reserve_open_risk_tx(cursor: Any, command: OrderCommand, at: datetime) -> None:
        if command.reduce_only:
            return
        max_loss = command.max_loss_usd
        loss_budget = command.available_loss_budget_usd
        notional_budget = command.available_notional_budget_usd
        order_slots = command.available_order_slots
        if None in (max_loss, loss_budget, notional_budget, order_slots):
            raise ValueError("open command lacks reservation limits")
        cursor.execute("SELECT state FROM v2_trading_state WHERE singleton=TRUE FOR UPDATE")
        if cursor.fetchone() is None:
            raise RuntimeError("trading state singleton is missing")
        cursor.execute(
            """SELECT COALESCE(SUM(reserved_notional_usd),0),
                      COALESCE(SUM(reserved_loss_usd),0),COUNT(*)
               FROM v2_risk_reservations WHERE state='ACTIVE'"""
        )
        row = cursor.fetchone()
        active_notional = Decimal(str(row[0]))
        active_loss = Decimal(str(row[1]))
        active_count = int(row[2])
        if active_notional + command.amount_usd > notional_budget:
            raise PermissionError("atomic notional reservation budget exceeded")
        if active_loss + max_loss > loss_budget:
            raise PermissionError("atomic loss reservation budget exceeded")
        if active_count + 1 > order_slots:
            raise PermissionError("atomic order-slot reservation budget exceeded")
        cursor.execute(
            """INSERT INTO v2_risk_reservations(
                   order_command_id,reserved_notional_usd,reserved_loss_usd,state,created_at
               ) VALUES(%s,%s,%s,'ACTIVE',%s)""",
            (command.order_command_id, command.amount_usd, max_loss, at),
        )

    @staticmethod
    def _release_risk_reservation_tx(
        cursor: Any,
        order_command_id: str,
        at: datetime,
    ) -> None:
        cursor.execute(
            """UPDATE v2_risk_reservations SET state='RELEASED',released_at=%s
               WHERE order_command_id=%s AND state='ACTIVE'""",
            (at, order_command_id),
        )

    def active_risk_reservations(self) -> tuple[Mapping[str, Any], ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """SELECT order_command_id,reserved_notional_usd,reserved_loss_usd,created_at
                   FROM v2_risk_reservations WHERE state='ACTIVE'
                   ORDER BY created_at,order_command_id"""
            )
            rows = cursor.fetchall()
        return tuple(
            {
                "order_command_id": str(row[0]),
                "reserved_notional_usd": Decimal(str(row[1])),
                "reserved_loss_usd": Decimal(str(row[2])),
                "created_at": row[3],
            }
            for row in rows
        )

    def save_broker_order(self, order: BrokerOrder, event: DomainEvent) -> None:
        with self.transaction() as cursor:
            cursor.execute(
                """UPDATE v2_broker_orders SET status=%s,broker_order_id=%s,broker_position_id=%s,
                   filled_quantity=%s,average_fill_price=%s,state=%s::jsonb,updated_at=%s
                   WHERE order_command_id=%s""",
                (
                    order.status.value,
                    order.broker_order_id,
                    order.broker_position_id,
                    order.filled_quantity,
                    order.average_fill_price,
                    self._json(asdict(order)),
                    event.processing_time,
                    order.order_command_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("broker order missing")
            if order.status in {
                OrderStatus.REJECTED,
                OrderStatus.CANCELLED,
                OrderStatus.EXPIRED,
                OrderStatus.RECONCILED_ABSENT,
            }:
                self._release_risk_reservation_tx(
                    cursor,
                    order.order_command_id,
                    event.processing_time,
                )
            self.append_event_tx(cursor, event)

    def save_position(self, position: PositionState, event: DomainEvent) -> None:
        with self.transaction() as cursor:
            cursor.execute(
                """INSERT INTO v2_positions(position_id,portfolio_id,strategy_id,lane_id,intent_id,symbol,
                   status,broker_position_id,state,version,updated_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,1,%s)
                   ON CONFLICT(position_id) DO UPDATE SET portfolio_id=EXCLUDED.portfolio_id,
                   strategy_id=EXCLUDED.strategy_id,lane_id=EXCLUDED.lane_id,intent_id=EXCLUDED.intent_id,
                   symbol=EXCLUDED.symbol,status=EXCLUDED.status,broker_position_id=EXCLUDED.broker_position_id,
                   state=EXCLUDED.state,version=v2_positions.version+1,updated_at=EXCLUDED.updated_at""",
                (
                    position.position_id,
                    position.portfolio_id,
                    position.strategy_id,
                    position.lane_id,
                    position.intent_id,
                    position.symbol,
                    position.status.value,
                    position.broker_position_id,
                    self._json(asdict(position)),
                    event.processing_time,
                ),
            )
            self.append_event_tx(cursor, event)

    def fill_exists(self, idempotency_key: str) -> bool:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM v2_fills WHERE idempotency_key=%s", (idempotency_key,))
            return cursor.fetchone() is not None

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
        if (reconciliation_case is None) != (reconciliation_event is None):
            raise ValueError("reconciliation case and event must be supplied together")
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT fill_id FROM v2_fills WHERE idempotency_key=%s",
                (fill.idempotency_key,),
            )
            if cursor.fetchone() is not None:
                return False
            cursor.execute(
                """INSERT INTO v2_fills(fill_id,idempotency_key,order_command_id,broker_order_id,
                   broker_position_id,symbol,side,quantity,price,fee_usd,financing_usd,event_time,
                   processing_time,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                (
                    fill.fill_id,
                    fill.idempotency_key,
                    fill.order_command_id,
                    fill.broker_order_id,
                    fill.broker_position_id,
                    fill.symbol,
                    fill.side.value,
                    fill.quantity,
                    fill.price,
                    fill.fee_usd,
                    fill.financing_usd,
                    fill.event_time,
                    fill.processing_time,
                    self._json(asdict(fill)),
                ),
            )
            cursor.execute(
                """UPDATE v2_broker_orders SET status=%s,broker_order_id=%s,broker_position_id=%s,
                   filled_quantity=%s,average_fill_price=%s,state=%s::jsonb,updated_at=%s
                   WHERE order_command_id=%s""",
                (
                    order.status.value,
                    order.broker_order_id,
                    order.broker_position_id,
                    order.filled_quantity,
                    order.average_fill_price,
                    self._json(asdict(order)),
                    fill.processing_time,
                    order.order_command_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("broker order missing during fill")
            if order.status in {OrderStatus.FILLED, OrderStatus.RECONCILED_FILLED}:
                self._release_risk_reservation_tx(
                    cursor,
                    order.order_command_id,
                    fill_event.processing_time,
                )
            cursor.execute(
                """INSERT INTO v2_positions(position_id,portfolio_id,strategy_id,lane_id,intent_id,symbol,
                   status,broker_position_id,state,version,updated_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,1,%s)
                   ON CONFLICT(position_id) DO UPDATE SET portfolio_id=EXCLUDED.portfolio_id,
                   strategy_id=EXCLUDED.strategy_id,lane_id=EXCLUDED.lane_id,intent_id=EXCLUDED.intent_id,
                   symbol=EXCLUDED.symbol,status=EXCLUDED.status,broker_position_id=EXCLUDED.broker_position_id,
                   state=EXCLUDED.state,version=v2_positions.version+1,updated_at=EXCLUDED.updated_at""",
                (
                    position.position_id,
                    position.portfolio_id,
                    position.strategy_id,
                    position.lane_id,
                    position.intent_id,
                    position.symbol,
                    position.status.value,
                    position.broker_position_id,
                    self._json(asdict(position)),
                    position_event.processing_time,
                ),
            )
            self.append_event_tx(cursor, fill_event)
            self.append_event_tx(cursor, position_event)
            if reconciliation_case is not None and reconciliation_event is not None:
                self._save_reconciliation_case_tx(cursor, reconciliation_case)
                self.append_event_tx(cursor, reconciliation_event)
            return True

    def reconciliation_case(self, order_command_id: str) -> ReconciliationCase | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """SELECT case_id,order_command_id,status,opened_at,updated_at,
                          attempts,broker_snapshot_hash,detail
                   FROM v2_reconciliation_cases WHERE order_command_id=%s
                   ORDER BY updated_at DESC LIMIT 1""",
                (order_command_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return ReconciliationCase(
            case_id=str(row[0]),
            order_command_id=str(row[1]),
            status=str(row[2]),
            opened_at=row[3],
            updated_at=row[4],
            attempts=int(row[5]),
            broker_snapshot_hash=str(row[6]).strip(),
            detail=str(row[7]),
        )

    def _save_reconciliation_case_tx(self, cursor: Any, case: ReconciliationCase) -> None:
        cursor.execute(
            """INSERT INTO v2_reconciliation_cases(
                   case_id,order_command_id,status,attempts,broker_snapshot_hash,
                   detail,opened_at,updated_at
               ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(case_id) DO UPDATE SET
                 status=EXCLUDED.status,attempts=EXCLUDED.attempts,
                 broker_snapshot_hash=EXCLUDED.broker_snapshot_hash,
                 detail=EXCLUDED.detail,updated_at=EXCLUDED.updated_at""",
            (
                case.case_id,
                case.order_command_id,
                case.status,
                case.attempts,
                case.broker_snapshot_hash,
                case.detail,
                case.opened_at,
                case.updated_at,
            ),
        )

    def save_reconciliation_bundle(
        self,
        order: BrokerOrder,
        case: ReconciliationCase,
        event: DomainEvent,
    ) -> None:
        with self.transaction() as cursor:
            cursor.execute(
                """UPDATE v2_broker_orders SET status=%s,broker_order_id=%s,
                   broker_position_id=%s,filled_quantity=%s,average_fill_price=%s,
                   state=%s::jsonb,updated_at=%s WHERE order_command_id=%s""",
                (
                    order.status.value,
                    order.broker_order_id,
                    order.broker_position_id,
                    order.filled_quantity,
                    order.average_fill_price,
                    self._json(asdict(order)),
                    event.processing_time,
                    order.order_command_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("broker order missing during reconciliation")
            if order.status is OrderStatus.RECONCILED_ABSENT:
                self._release_risk_reservation_tx(
                    cursor,
                    order.order_command_id,
                    event.processing_time,
                )
            self._save_reconciliation_case_tx(cursor, case)
            self.append_event_tx(cursor, event)

    def order_command(self, order_command_id: str) -> OrderCommand:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT command FROM v2_order_commands WHERE order_command_id=%s",
                (order_command_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise ValueError("order command missing")
        return decode_dataclass(OrderCommand, self._mapping(row[0]))

    def order_command_for_idempotency(self, idempotency_key: str) -> OrderCommand | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT command FROM v2_order_commands WHERE idempotency_key=%s",
                (idempotency_key,),
            )
            row = cursor.fetchone()
        return None if row is None else decode_dataclass(OrderCommand, self._mapping(row[0]))

    def broker_order(self, order_command_id: str) -> BrokerOrder:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT state FROM v2_broker_orders WHERE order_command_id=%s", (order_command_id,)
            )
            row = cursor.fetchone()
        if row is None:
            raise ValueError("broker order missing")
        return decode_dataclass(BrokerOrder, self._mapping(row[0]))

    def broker_orders_by_status(self, statuses: tuple[str, ...]) -> tuple[BrokerOrder, ...]:
        if not statuses:
            return ()
        with self.connection.cursor() as cursor:
            cursor.execute(
                """SELECT state FROM v2_broker_orders
                   WHERE status=ANY(%s) ORDER BY updated_at,order_command_id""",
                (list(statuses),),
            )
            rows = cursor.fetchall()
        return tuple(decode_dataclass(BrokerOrder, self._mapping(row[0])) for row in rows)

    def fill_by_idempotency(self, idempotency_key: str) -> Fill | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT payload FROM v2_fills WHERE idempotency_key=%s",
                (idempotency_key,),
            )
            row = cursor.fetchone()
        return None if row is None else decode_dataclass(Fill, self._mapping(row[0]))

    def fills_for_order(self, order_command_id: str) -> tuple[Fill, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """SELECT payload FROM v2_fills WHERE order_command_id=%s
                   ORDER BY event_time,fill_id""",
                (order_command_id,),
            )
            rows = cursor.fetchall()
        return tuple(decode_dataclass(Fill, self._mapping(row[0])) for row in rows)

    def heartbeat(
        self,
        service: str,
        status: str,
        details: Mapping[str, Any],
        *,
        at: datetime | None = None,
    ) -> None:
        if not service.strip() or not status.strip():
            raise ValueError("heartbeat service/status are required")
        current = (at or datetime.now(UTC)).astimezone(UTC)
        with self.transaction() as cursor:
            cursor.execute(
                """INSERT INTO v2_service_heartbeats(
                       service,status,details,recorded_at
                   ) VALUES(%s,%s,%s::jsonb,%s)
                   ON CONFLICT(service) DO UPDATE SET status=EXCLUDED.status,
                   details=EXCLUDED.details,recorded_at=EXCLUDED.recorded_at""",
                (service, status, self._json(dict(details)), current),
            )

    def market_heartbeat(
        self,
        status: str,
        details: Mapping[str, Any],
        *,
        at: datetime | None = None,
    ) -> None:
        current = (at or datetime.now(UTC)).astimezone(UTC)
        with self.transaction() as cursor:
            cursor.execute(
                "SELECT v2_record_market_heartbeat(%s,%s::jsonb,%s)",
                (status, self._json(dict(details)), current),
            )

    def positions(
        self, portfolio_id: str | None = None, *, open_only: bool = False
    ) -> tuple[PositionState, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """SELECT state FROM v2_positions
                   WHERE (%s::text IS NULL OR portfolio_id=%s)
                     AND (NOT %s OR status='OPEN')
                   ORDER BY updated_at,position_id""",
                (portfolio_id, portfolio_id, open_only),
            )
            rows = cursor.fetchall()
        return tuple(decode_dataclass(PositionState, self._mapping(row[0])) for row in rows)

    def pending_outbox(self, limit: int = 100) -> tuple[Mapping[str, Any], ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """SELECT outbox_id,topic,payload,idempotency_key,created_at FROM v2_outbox
                   WHERE delivered_at IS NULL ORDER BY created_at LIMIT %s""",
                (max(1, min(limit, 1000)),),
            )
            rows = cursor.fetchall()
        return tuple(
            {
                "outbox_id": str(row[0]),
                "topic": str(row[1]),
                "payload": self._mapping(row[2]),
                "idempotency_key": str(row[3]),
                "created_at": row[4].isoformat(),
            }
            for row in rows
        )

    def mark_outbox_delivered(self, outbox_id: str, claim_token: str, at: datetime) -> None:
        with self.transaction() as cursor:
            cursor.execute(
                """UPDATE v2_outbox SET delivered_at=%s,claimed_by=NULL,
                   claim_token=NULL,lease_expires_at=NULL,last_error_type=NULL
                   WHERE outbox_id=%s AND delivered_at IS NULL AND claim_token=%s""",
                (at.astimezone(UTC), outbox_id, claim_token),
            )
            if cursor.rowcount != 1:
                raise PermissionError("outbox claim token is not active")

    def release_outbox_claim(
        self,
        outbox_id: str,
        claim_token: str,
        *,
        error_type: str,
    ) -> None:
        with self.transaction() as cursor:
            cursor.execute(
                """UPDATE v2_outbox SET claimed_by=NULL,claim_token=NULL,
                   lease_expires_at=NULL,last_error_type=%s
                   WHERE outbox_id=%s AND delivered_at IS NULL AND claim_token=%s""",
                (error_type[:128], outbox_id, claim_token),
            )
            if cursor.rowcount != 1:
                raise PermissionError("outbox claim token is not active")

    def queue_decision(
        self,
        decision_id: str,
        packet_hash: str,
        decision: Mapping[str, Any],
        *,
        expires_at: datetime,
        created_at: datetime,
    ) -> bool:
        with self.transaction() as cursor:
            cursor.execute(
                """INSERT INTO v2_decisions(decision_id,packet_hash,decision,state,created_at,expires_at,updated_at)
                   VALUES(%s,%s,%s::jsonb,'DECIDED',%s,%s,%s) ON CONFLICT(decision_id) DO NOTHING""",
                (
                    decision_id,
                    packet_hash,
                    self._json(dict(decision)),
                    created_at,
                    expires_at,
                    created_at,
                ),
            )
            return cursor.rowcount == 1

    def complete_decision(
        self,
        decision_id: str,
        claim_token: str,
        applied_effect: Mapping[str, Any],
        *,
        now: datetime,
    ) -> None:
        with self.transaction() as cursor:
            cursor.execute(
                """UPDATE v2_decisions SET state='APPLIED',applied_effect=%s::jsonb,claim_token=NULL,
                   lease_expires_at=NULL,updated_at=%s WHERE decision_id=%s AND state='CLAIMED'
                   AND claim_token=%s AND lease_expires_at>=%s""",
                (self._json(dict(applied_effect)), now, decision_id, claim_token, now),
            )
            if cursor.rowcount != 1:
                raise PermissionError("decision claim is absent, expired, or already applied")

    def fail_decision(
        self,
        decision_id: str,
        claim_token: str,
        *,
        retryable: bool,
        now: datetime,
        max_attempts: int = 3,
        reason: str = "apply_error",
    ) -> None:
        with self.transaction() as cursor:
            cursor.execute(
                """SELECT attempt_count FROM v2_decisions
                   WHERE decision_id=%s AND state='CLAIMED' AND claim_token=%s
                   FOR UPDATE""",
                (decision_id, claim_token),
            )
            row = cursor.fetchone()
            if row is None:
                raise PermissionError("decision claim token is not active")
            attempt = int(row[0])
            terminal = not retryable or attempt >= max_attempts
            cursor.execute(
                """UPDATE v2_decisions SET state=%s,claimed_by=NULL,claim_token=NULL,
                   lease_expires_at=NULL,applied_effect=%s::jsonb,updated_at=%s
                   WHERE decision_id=%s AND state='CLAIMED' AND claim_token=%s""",
                (
                    "FAILED_TERMINAL" if terminal else "FAILED_RETRYABLE",
                    self._json({"reason": reason[:200], "attempt": attempt}) if terminal else None,
                    now,
                    decision_id,
                    claim_token,
                ),
            )
            if cursor.rowcount != 1:
                raise PermissionError("decision claim token is not active")
            if terminal:
                key = f"decision-dead-letter:{decision_id}:{attempt}"
                self.append_event_tx(
                    cursor,
                    DomainEvent(
                        event_id="evt-" + hashlib.sha256(key.encode()).hexdigest()[:24],
                        event_type="DecisionDeadLettered",
                        schema_version=4,
                        event_time=now,
                        processing_time=now,
                        idempotency_key=key,
                        causation_id=decision_id,
                        correlation_id=decision_id,
                        payload={
                            "decision_id": decision_id,
                            "reason": reason[:200],
                            "attempt": attempt,
                        },
                    ),
                )
