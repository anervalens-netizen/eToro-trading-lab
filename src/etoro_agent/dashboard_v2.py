from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .audit_anchor_v2 import AuditAnchor, AuditAnchorWriter
from .config_v2 import load_config_v2
from .domain_v2 import PositionState
from .execution_gate_v2 import execution_gate_present
from .postgres_runtime_v2 import PostgresRuntimeStoreV2
from .runtime_store_impl_v2 import ZERO_HASH
from .runtime_store_v2 import RuntimeStoreV2


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _age_seconds(path: Path, now: datetime) -> float | None:
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return None
    return max(0.0, (now - modified).total_seconds())


def _anchor_checkpoint() -> tuple[int, str, datetime] | None:
    marker = Path(
        os.getenv(
            "ETORO_V2_ANCHOR_LATEST",
            "/storage/backups/db/etoro/v2-anchors/LATEST.json",
        )
    )
    public_key_path = os.getenv("ETORO_V2_ANCHOR_PUBLIC_KEY_FILE", "")
    if not marker.is_file() or not public_key_path:
        return None
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
        anchor = AuditAnchor(
            str(value["anchor_id"]),
            str(value["head_event_hash"]),
            str(value["signature"]),
            str(value["algorithm"]),
            str(value["anchored_at"]),
            str(value["destination"]),
        )
        if not AuditAnchorWriter.verify(anchor, Path(public_key_path).read_bytes()):
            return None
        return (
            int(value["sequence"]),
            anchor.head_event_hash,
            datetime.fromisoformat(anchor.anchored_at.replace("Z", "+00:00")),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _incremental_chain_valid(
    rows: list[tuple[int, str, str, str, str]],
    *,
    sequence: int,
    head_hash: str,
) -> bool:
    previous = head_hash if sequence else ZERO_HASH
    expected_sequence = sequence + 1
    for stored_sequence, stored_previous, event_hash, body, body_hash in rows:
        if int(stored_sequence) != expected_sequence or str(stored_previous).strip() != previous:
            return False
        canonical = str(body)
        if hashlib.sha256(canonical.encode()).hexdigest() != str(body_hash).strip():
            return False
        expected = hashlib.sha256((previous + canonical).encode()).hexdigest()
        if expected != str(event_hash).strip():
            return False
        previous = expected
        expected_sequence += 1
    return True


def _health_payload(
    *,
    trading_state: str,
    heartbeats: Mapping[str, tuple[str, datetime, Mapping[str, Any]]],
    oldest_outbox_at: datetime | None,
    oldest_unknown_at: datetime | None,
    oldest_reconciliation_at: datetime | None,
    dead_letters_total: int,
    dead_letters_recent: int,
    chain_valid: bool,
    anchor_at: datetime | None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    failures: list[str] = []
    warnings: list[str] = []
    gate = execution_gate_present()
    required = {
        "v2-market",
        "v2-coordinator",
        "v2-reconciliation",
        "v2-role-apply",
    }
    if gate:
        required.update({"v2-decision-apply", "v2-demo-executor", "v2-exit-manager"})
    else:
        required.add("v2-decision-shadow")
    stale: list[str] = []
    for service in required:
        item = heartbeats.get(service)
        if item is None or (now - item[1]).total_seconds() > 300:
            stale.append(service)
        elif item[0] == "error":
            failures.append(f"heartbeat_error:{service}")
    if stale:
        failures.append("stale_heartbeats:" + ",".join(sorted(stale)))
    reconciliation = heartbeats.get("v2-reconciliation")
    if reconciliation and reconciliation[2].get("economic_drift"):
        failures.append("broker_economic_drift")
    if not chain_valid:
        failures.append("audit_chain_or_checkpoint_invalid")
    if anchor_at is None or now - anchor_at > timedelta(hours=2, minutes=15):
        failures.append("audit_anchor_stale")
    if oldest_unknown_at is not None:
        failures.append("unknown_order_pending")
    if oldest_reconciliation_at is not None and now - oldest_reconciliation_at > timedelta(
        minutes=5
    ):
        failures.append("reconciliation_lag")
    if oldest_outbox_at is not None and now - oldest_outbox_at > timedelta(minutes=2):
        failures.append("outbox_lag")
    if dead_letters_recent:
        warnings.append(f"recent_ai_dead_letters:{dead_letters_recent}")

    backup_root = Path(os.getenv("ETORO_V2_BACKUP_ROOT", "/storage/backups/db/etoro/v2"))
    backup_age = _age_seconds(backup_root / "LAST_BACKUP_OK", now)
    restore_age = _age_seconds(backup_root / "LAST_RESTORE_DRILL_OK", now)
    offhost_age = _age_seconds(
        Path(
            os.getenv(
                "ETORO_V2_OFFHOST_MARKER",
                "/var/lib/etoro-v2-offhost/LAST_OFFHOST_OK",
            )
        ),
        now,
    )
    if backup_age is None or backup_age > 27 * 3600:
        warnings.append("backup_stale")
    if restore_age is None or restore_age > 8 * 24 * 3600:
        warnings.append("restore_drill_stale")
    if offhost_age is None or offhost_age > 51 * 3600:
        warnings.append("offhost_backup_stale")

    if gate and trading_state == "LOCKED":
        failures.append("execution_gate_state_mismatch")
    if not gate and trading_state != "LOCKED":
        failures.append("execution_gate_absent_but_state_not_locked")
    status = (
        "error"
        if failures
        else "degraded"
        if warnings
        else "locked"
        if trading_state == "LOCKED"
        else "ok"
    )
    return _json_safe(
        {
            "status": status,
            "real_money": False,
            "account_mode": "DEMO",
            "trading_state": trading_state,
            "execution_gate_present": gate,
            "execution_enabled": gate and trading_state != "LOCKED",
            "failures": failures,
            "warnings": warnings,
            "stale_heartbeats": stale,
            "queue": {
                "oldest_outbox_age_seconds": None
                if oldest_outbox_at is None
                else max(0.0, (now - oldest_outbox_at).total_seconds()),
                "oldest_unknown_age_seconds": None
                if oldest_unknown_at is None
                else max(0.0, (now - oldest_unknown_at).total_seconds()),
                "oldest_reconciliation_age_seconds": None
                if oldest_reconciliation_at is None
                else max(0.0, (now - oldest_reconciliation_at).total_seconds()),
                "dead_letters_total": dead_letters_total,
                "dead_letters_recent_15m": dead_letters_recent,
            },
            "audit": {
                "incremental_chain_valid": chain_valid,
                "last_anchor_at": None if anchor_at is None else anchor_at.isoformat(),
            },
            "backup": {
                "age_seconds": backup_age,
                "restore_drill_age_seconds": restore_age,
                "offhost_age_seconds": offhost_age,
            },
        }
    )


def _snapshot_payload(
    *,
    config: Any,
    positions: tuple[PositionState, ...],
    trading_state: str,
    research_epoch: str,
    chain_valid: bool,
    events: int,
    fills: int,
    decisions: Mapping[str, int],
    order_states: Mapping[str, int],
) -> dict[str, Any]:
    open_positions = [item for item in positions if item.status.value == "OPEN"]
    closed_positions = [item for item in positions if item.status.value == "CLOSED"]
    realized = sum((item.realized_pnl for item in positions), Decimal("0"))
    unrealized = sum((item.unrealized_pnl for item in open_positions), Decimal("0"))
    fees = sum((item.fees_accrued for item in positions), Decimal("0"))
    financing = sum((item.financing_accrued for item in positions), Decimal("0"))
    unknown_orders = int(order_states.get("UNKNOWN", 0))
    health_status = (
        "error"
        if not chain_valid
        else "halted"
        if trading_state != "ACTIVE"
        else "degraded"
        if unknown_orders
        else "ok"
    )
    return _json_safe(
        {
            "schema_version": 4,
            "generated_at": datetime.now(UTC).isoformat(),
            "real_money": False,
            "account_mode": "DEMO",
            "trading_state": trading_state,
            "research_epoch": research_epoch,
            "health": {
                "status": health_status,
                "unknown_orders": unknown_orders,
            },
            "audit": {"events": events, "chain_valid": chain_valid},
            "portfolio": {
                "initial_cash_usd": str(config.initial_cash_usd),
                "realized_pnl_usd": str(realized),
                "unrealized_pnl_usd": str(unrealized),
                "fees_usd": str(fees),
                "financing_usd": str(financing),
                "open_positions": len(open_positions),
                "closed_positions": len(closed_positions),
                "fills": fills,
            },
            "positions": [asdict(item) for item in positions],
            "orders": dict(order_states),
            "ai_decisions": dict(decisions),
            "compatibility": [asdict(item) for item in config.compatibility()],
        }
    )


class DashboardServiceV2:
    """Read-only projection for v2 economic/runtime/research state."""

    def __init__(self, runtime_db: str | Path, config_path: str | Path) -> None:
        self.runtime_db = Path(runtime_db)
        self.config_path = Path(config_path)

    def snapshot(self) -> dict[str, Any]:
        config = load_config_v2(self.config_path)
        store = RuntimeStoreV2(self.runtime_db)
        try:
            positions = store.positions()
            events = int(store.db.execute("SELECT COUNT(*) FROM v2_events").fetchone()[0])
            fills = int(store.db.execute("SELECT COUNT(*) FROM v2_fills").fetchone()[0])
            decisions = {
                str(row[0]): int(row[1])
                for row in store.db.execute(
                    "SELECT state,COUNT(*) FROM v2_decisions GROUP BY state"
                )
            }
            order_states: dict[str, int] = {}
            for row in store.db.execute("SELECT state_json FROM v2_broker_orders"):
                try:
                    state = str(json.loads(str(row[0])).get("status", "UNKNOWN"))
                except json.JSONDecodeError:
                    state = "UNKNOWN"
                order_states[state] = order_states.get(state, 0) + 1
            return _snapshot_payload(
                config=config,
                positions=positions,
                trading_state=store.state_get("trading_state", "LOCKED"),
                research_epoch=store.state_get("research_epoch_v2", ""),
                chain_valid=store.verify_event_chain(),
                events=events,
                fills=fills,
                decisions=decisions,
                order_states=order_states,
            )
        finally:
            store.close()

    def health(self) -> dict[str, Any]:
        try:
            store = RuntimeStoreV2(self.runtime_db)
        except Exception as exc:
            return {
                "status": "error",
                "real_money": False,
                "account_mode": "DEMO",
                "failures": [
                    "audit_chain_or_checkpoint_invalid",
                    f"health_query:{type(exc).__name__}",
                ],
            }
        try:
            checkpoint = _anchor_checkpoint()
            if checkpoint is None:
                raw_sequence = store.state_get("audit_checkpoint_sequence", "0")
                raw_hash = store.state_get("audit_checkpoint_hash", ZERO_HASH)
                raw_at = store.state_get("audit_checkpoint_verified_at", "")
                try:
                    checkpoint = (
                        int(raw_sequence),
                        raw_hash,
                        datetime.fromisoformat(raw_at),
                    )
                except (TypeError, ValueError):
                    checkpoint = None
            if checkpoint is None:
                chain_valid = False
                anchor_at = None
            else:
                sequence, head_hash, anchor_at = checkpoint
                anchored_row = store.db.execute(
                    "SELECT event_hash FROM v2_events WHERE sequence=?", (sequence,)
                ).fetchone()
                rows = store.db.execute(
                    """SELECT sequence,previous_hash,event_hash,canonical_body,
                              canonical_body_hash FROM v2_events
                       WHERE sequence>? ORDER BY sequence LIMIT 10001""",
                    (sequence,),
                ).fetchall()
                chain_valid = (
                    anchored_row is not None
                    and str(anchored_row[0]).strip() == head_hash
                    and len(rows) <= 10000
                    and _incremental_chain_valid(
                        [tuple(row) for row in rows], sequence=sequence, head_hash=head_hash
                    )
                )
                if store.state_get("audit_integrity_failure"):
                    chain_valid = False
            heartbeats = {
                str(row[0]): (
                    str(row[1]),
                    datetime.fromisoformat(str(row[3])),
                    json.loads(str(row[2])),
                )
                for row in store.db.execute(
                    "SELECT service,status,details_json,recorded_at FROM v2_service_heartbeats"
                )
            }
            outbox = store.db.execute(
                "SELECT MIN(created_at) FROM v2_outbox WHERE delivered_at IS NULL"
            ).fetchone()[0]
            unknown: list[datetime] = []
            for row in store.db.execute("SELECT state_json,updated_at FROM v2_broker_orders"):
                if json.loads(str(row[0])).get("status") == "UNKNOWN":
                    unknown.append(datetime.fromisoformat(str(row[1])))
            reconciliation = store.db.execute(
                "SELECT MIN(updated_at) FROM v2_reconciliation_cases WHERE status='OPEN'"
            ).fetchone()[0]
            dead_letters_total = int(
                store.db.execute(
                    "SELECT COUNT(*) FROM v2_decisions WHERE state='FAILED_TERMINAL'"
                ).fetchone()[0]
            )
            recent_cutoff = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
            dead_letters_recent = int(
                store.db.execute(
                    """SELECT COUNT(*) FROM v2_decisions
                       WHERE state='FAILED_TERMINAL' AND updated_at>=?""",
                    (recent_cutoff,),
                ).fetchone()[0]
            )
            return _health_payload(
                trading_state=store.state_get("trading_state", "LOCKED"),
                heartbeats=heartbeats,
                oldest_outbox_at=None if outbox is None else datetime.fromisoformat(str(outbox)),
                oldest_unknown_at=min(unknown) if unknown else None,
                oldest_reconciliation_at=(
                    None if reconciliation is None else datetime.fromisoformat(str(reconciliation))
                ),
                dead_letters_total=dead_letters_total,
                dead_letters_recent=dead_letters_recent,
                chain_valid=chain_valid,
                anchor_at=anchor_at,
            )
        finally:
            store.close()


class PostgresDashboardServiceV2:
    """Read-only dashboard projection from the canonical PostgreSQL runtime."""

    def __init__(self, dsn: str, config_path: str | Path) -> None:
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self.dsn = dsn
        self.config_path = Path(config_path)

    def snapshot(self) -> dict[str, Any]:
        config = load_config_v2(self.config_path)
        store = PostgresRuntimeStoreV2.from_dsn(self.dsn)
        try:
            store.require_schema()
            positions = store.positions()
            with store.connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM v2_events")
                events = int(cursor.fetchone()[0])
                cursor.execute("SELECT COUNT(*) FROM v2_fills")
                fills = int(cursor.fetchone()[0])
                cursor.execute("SELECT status,COUNT(*) FROM v2_broker_orders GROUP BY status")
                order_states = {str(row[0]): int(row[1]) for row in cursor.fetchall()}
                cursor.execute("SELECT to_regclass('v2_ai_packets')")
                ai_table = cursor.fetchone()[0]
                decisions: dict[str, int] = {}
                if ai_table is not None:
                    cursor.execute("SELECT state,COUNT(*) FROM v2_ai_packets GROUP BY state")
                    decisions = {str(row[0]): int(row[1]) for row in cursor.fetchall()}
            return _snapshot_payload(
                config=config,
                positions=positions,
                trading_state=store.state_get("trading_state", "LOCKED"),
                research_epoch=store.state_get("research_epoch_v2", ""),
                chain_valid=store.verify_event_chain(),
                events=events,
                fills=fills,
                decisions=decisions,
                order_states=order_states,
            )
        finally:
            store.close()

    def health(self) -> dict[str, Any]:
        store = PostgresRuntimeStoreV2.from_dsn(self.dsn)
        try:
            store.require_schema()
            checkpoint = _anchor_checkpoint()
            anchor_at: datetime | None = None
            chain_valid = checkpoint is not None
            with store.connection.cursor() as cursor:
                if checkpoint is not None:
                    sequence, head_hash, anchor_at = checkpoint
                    cursor.execute(
                        "SELECT event_hash FROM v2_events WHERE sequence=%s", (sequence,)
                    )
                    anchored_row = cursor.fetchone()
                    cursor.execute(
                        """SELECT sequence,previous_hash,event_hash,canonical_body,
                                  canonical_body_hash FROM v2_events
                           WHERE sequence>%s ORDER BY sequence LIMIT 10001""",
                        (sequence,),
                    )
                    rows = cursor.fetchall()
                    chain_valid = (
                        anchored_row is not None
                        and str(anchored_row[0]).strip() == head_hash
                        and len(rows) <= 10000
                        and _incremental_chain_valid(
                            [tuple(row) for row in rows],
                            sequence=sequence,
                            head_hash=head_hash,
                        )
                    )
                cursor.execute("SELECT value FROM v2_meta WHERE key='audit_integrity_failure'")
                if cursor.fetchone() is not None:
                    chain_valid = False
                cursor.execute(
                    "SELECT service,status,recorded_at,details FROM v2_service_heartbeats"
                )
                heartbeats = {
                    str(row[0]): (
                        str(row[1]),
                        row[2].astimezone(UTC),
                        dict(store._mapping(row[3])),
                    )
                    for row in cursor.fetchall()
                }
                cursor.execute("SELECT MIN(created_at) FROM v2_outbox WHERE delivered_at IS NULL")
                oldest_outbox = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT MIN(updated_at) FROM v2_broker_orders WHERE status='UNKNOWN'"
                )
                oldest_unknown = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT MIN(updated_at) FROM v2_reconciliation_cases WHERE status='OPEN'"
                )
                oldest_reconciliation = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM v2_ai_packets WHERE state='DEAD_LETTER'")
                dead_letters_total = int(cursor.fetchone()[0])
                cursor.execute(
                    """SELECT COUNT(*) FROM v2_ai_packets
                       WHERE state='DEAD_LETTER' AND dead_lettered_at>=now()-interval '15 minutes'"""
                )
                dead_letters_recent = int(cursor.fetchone()[0])
            return _health_payload(
                trading_state=store.state_get("trading_state", "LOCKED"),
                heartbeats=heartbeats,
                oldest_outbox_at=oldest_outbox,
                oldest_unknown_at=oldest_unknown,
                oldest_reconciliation_at=oldest_reconciliation,
                dead_letters_total=dead_letters_total,
                dead_letters_recent=dead_letters_recent,
                chain_valid=chain_valid,
                anchor_at=anchor_at,
            )
        except Exception as exc:
            return {
                "status": "error",
                "real_money": False,
                "account_mode": "DEMO",
                "failures": [f"health_query:{type(exc).__name__}"],
            }
        finally:
            store.close()


def create_v2_app(service: DashboardServiceV2 | PostgresDashboardServiceV2) -> Any:
    try:
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("FastAPI is required for dashboard v2") from exc
    app = FastAPI(title="eToro Trading Lab v2", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> Any:
        health = service.health()
        health_status = str(health["status"])
        status = 200 if health_status in {"ok", "locked"} else 503
        return JSONResponse(health, status_code=status)

    @app.get("/api/v2/snapshot")
    async def snapshot() -> Any:
        return JSONResponse(service.snapshot(), headers={"Cache-Control": "no-store"})

    return app
