from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class MemoryItem:
    memory_id: str
    category: str
    version: int
    body: Mapping[str, object]
    evidence_refs: tuple[str, ...]
    valid_from: datetime
    expires_at: datetime | None

    def active(self, at: datetime) -> bool:
        current = at.astimezone(UTC)
        return current >= self.valid_from.astimezone(UTC) and (
            self.expires_at is None or current <= self.expires_at.astimezone(UTC)
        )


class StructuredMemoryStore:
    ALLOWED_CATEGORIES = frozenset(
        {
            "decision_episode",
            "market_regime",
            "model_calibration",
            "failure_pattern",
            "strategy_hypothesis",
            "research_proposal",
        }
    )

    def __init__(self, path: str | Path) -> None:
        self.db = sqlite3.connect(Path(path))
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS structured_memory(
              memory_id TEXT NOT NULL, category TEXT NOT NULL, version INTEGER NOT NULL,
              body_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
              valid_from TEXT NOT NULL, expires_at TEXT,
              PRIMARY KEY(memory_id,version)
            );
            CREATE INDEX IF NOT EXISTS structured_memory_active_idx
              ON structured_memory(category,valid_from,expires_at);
            """
        )
        self.db.commit()

    def append(self, item: MemoryItem) -> None:
        if item.category not in self.ALLOWED_CATEGORIES or item.version < 1:
            raise ValueError("structured memory identity is invalid")
        if not item.evidence_refs:
            raise ValueError("structured memory requires evidence references")
        self.db.execute(
            "INSERT INTO structured_memory VALUES(?,?,?,?,?,?,?)",
            (
                item.memory_id,
                item.category,
                item.version,
                json.dumps(dict(item.body), sort_keys=True, separators=(",", ":"), default=str),
                json.dumps(list(item.evidence_refs), separators=(",", ":")),
                item.valid_from.astimezone(UTC).isoformat(),
                None if item.expires_at is None else item.expires_at.astimezone(UTC).isoformat(),
            ),
        )
        self.db.commit()

    def active(
        self, category: str, *, at: datetime, limit: int = 20
    ) -> tuple[Mapping[str, object], ...]:
        if category not in self.ALLOWED_CATEGORIES:
            raise ValueError("unknown structured memory category")
        current = at.astimezone(UTC).isoformat()
        rows = self.db.execute(
            """SELECT memory_id,version,body_json,evidence_json,valid_from,expires_at
               FROM structured_memory
               WHERE category=? AND valid_from<=? AND (expires_at IS NULL OR expires_at>=?)
               ORDER BY valid_from DESC,version DESC LIMIT ?""",
            (category, current, current, max(1, min(limit, 100))),
        ).fetchall()
        return tuple(
            {
                "memory_id": str(row[0]),
                "version": int(row[1]),
                "body": json.loads(str(row[2])),
                "evidence_refs": json.loads(str(row[3])),
                "valid_from": str(row[4]),
                "expires_at": row[5],
            }
            for row in rows
        )
