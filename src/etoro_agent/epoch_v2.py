from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from .domain_v2 import DomainEvent
from .runtime_store_v2 import RuntimeStoreV2


@dataclass(frozen=True)
class ResearchEpochV2:
    epoch_id: str
    data_snapshot_id: str
    feature_version: str
    strategy_version: str
    cost_model_version: str
    risk_version: str
    prompt_version: str
    code_sha: str
    config_hash: str
    started_at: datetime

    @property
    def fingerprint(self) -> str:
        body = "|".join(
            str(value)
            for value in (
                self.data_snapshot_id,
                self.feature_version,
                self.strategy_version,
                self.cost_model_version,
                self.risk_version,
                self.prompt_version,
                self.code_sha,
                self.config_hash,
            )
        )
        return hashlib.sha256(body.encode()).hexdigest()


class ResearchEpochManagerV2:
    """Atomically segregate research statistics whenever economic semantics change."""

    def __init__(self, store: RuntimeStoreV2) -> None:
        self.store = store

    def activate(self, epoch: ResearchEpochV2, *, reason: str) -> bool:
        current = self.store.state_get("research_epoch_v2", "")
        if current == epoch.epoch_id:
            return False
        now = epoch.started_at.astimezone(UTC)
        payload = {
            **asdict(epoch),
            "fingerprint": epoch.fingerprint,
            "previous_epoch": current or None,
            "reason": reason[:500],
        }
        event = DomainEvent(
            event_id=f"evt-epoch-{hashlib.sha256(epoch.epoch_id.encode()).hexdigest()[:24]}",
            event_type="ResearchEpochActivated",
            schema_version=2,
            event_time=now,
            processing_time=now,
            idempotency_key=f"research-epoch:{epoch.epoch_id}",
            causation_id=current,
            correlation_id=epoch.epoch_id,
            payload=payload,
        )
        with self.store.atomic() as tx:
            tx.execute(
                "UPDATE v2_intents SET state='EXPIRED',updated_at=? WHERE state='ACTIVE'",
                (now.isoformat(),),
            )
            tx.execute(
                "UPDATE v2_decisions SET state='EXPIRED',claim_token=NULL,lease_expires_at=NULL,updated_at=? WHERE state IN ('DECIDED','CLAIMED','FAILED_RETRYABLE')",
                (now.isoformat(),),
            )
            tx.execute(
                "INSERT INTO v2_state(key,value,updated_at) VALUES('research_epoch_v2',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (epoch.epoch_id, now.isoformat()),
            )
            tx.execute(
                "INSERT INTO v2_state(key,value,updated_at) VALUES('research_epoch_v2_fingerprint',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (epoch.fingerprint, now.isoformat()),
            )
            self.store._append_event_tx(tx, event)
        return True

    def comparable(self, metadata: Mapping[str, object]) -> bool:
        return str(metadata.get("research_epoch_v2", "")) == self.store.state_get(
            "research_epoch_v2", ""
        ) and str(metadata.get("research_epoch_fingerprint", "")) == self.store.state_get(
            "research_epoch_v2_fingerprint", ""
        )
