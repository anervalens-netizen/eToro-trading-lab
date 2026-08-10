from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from .codec_v2 import decode_dataclass
from .domain_v2 import BrokerOrder, DomainEvent, Fill, IntentEnvelope, OrderCommand, PositionState, PositionStatus, canonical_json
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

    def state_set(self, key: str, value: str) -> None:
        now = datetime.now(timezone.utc)
        if key == "trading_state":
            if value not in {"ACTIVE", "HALT_NEW", "REDUCE_ONLY", "LOCKED"}:
                raise ValueError("invalid trading state")
            with self.transaction() as cursor:
                cursor.execute(
                    """UPDATE v2_trading_state SET state=%s,actor='runtime',reason='state_set',
                       version=version+1,changed_at=%s WHERE singleton=TRUE""",
                    (value, now),
                )
            return
        with self.transaction() as cursor:
            cursor.execute(
                """INSERT INTO v2_meta(key,value,updated_at) VALUES(%s,%s,%s)
                   ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at""",
                (key, value, now),
            )

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
                cursor.execute("SELECT envelope_hash FROM v2_intents WHERE intent_id=%s", (intent.intent_id,))
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
                if str(existing[0]) != command.order_command_id or str(existing[1]).strip() != command_hash:
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
            if outbox_topic is not None:
                outbox_id = f"outbox-{hashlib.sha256(command.idempotency_key.encode()).hexdigest()[:24]}"
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
    ) -> bool:
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
            return True

    def order_command(self, order_command_id: str) -> OrderCommand:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT command FROM v2_order_commands WHERE order_command_id=%s", (order_command_id,))
            row = cursor.fetchone()
        if row is None:
            raise ValueError("order command missing")
        return decode_dataclass(OrderCommand, self._mapping(row[0]))

    def broker_order(self, order_command_id: str) -> BrokerOrder:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT state FROM v2_broker_orders WHERE order_command_id=%s", (order_command_id,))
            row = cursor.fetchone()
        if row is None:
            raise ValueError("broker order missing")
        return decode_dataclass(BrokerOrder, self._mapping(row[0]))

    def positions(self, portfolio_id: str | None = None, *, open_only: bool = False) -> tuple[PositionState, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if portfolio_id is not None:
            clauses.append("portfolio_id=%s")
            params.append(portfolio_id)
        if open_only:
            clauses.append("status='OPEN'")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connection.cursor() as cursor:
            cursor.execute(f"SELECT state FROM v2_positions{where} ORDER BY updated_at,position_id", params)
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

    def mark_outbox_delivered(self, outbox_id: str, at: datetime) -> None:
        with self.transaction() as cursor:
            cursor.execute(
                "UPDATE v2_outbox SET delivered_at=%s,claimed_by=NULL,claim_token=NULL,lease_expires_at=NULL WHERE outbox_id=%s AND delivered_at IS NULL",
                (at.astimezone(timezone.utc), outbox_id),
            )

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
                (decision_id, packet_hash, self._json(dict(decision)), created_at, expires_at, created_at),
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
    ) -> None:
        with self.transaction() as cursor:
            cursor.execute(
                """UPDATE v2_decisions SET state=%s,claim_token=NULL,lease_expires_at=NULL,updated_at=%s
                   WHERE decision_id=%s AND state='CLAIMED' AND claim_token=%s""",
                ("FAILED_RETRYABLE" if retryable else "FAILED_TERMINAL", now, decision_id, claim_token),
            )
            if cursor.rowcount != 1:
                raise PermissionError("decision claim token is not active")
