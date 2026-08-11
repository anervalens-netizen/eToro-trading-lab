from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from .audit_anchor_v2 import AuditAnchorWriter
from .postgres_runtime_v2 import PostgresRuntimeStoreV2
from .runtime_store_v2 import RuntimeStoreV2


def main() -> None:
    parser = argparse.ArgumentParser(description="Sign and export the v2 audit-chain head")
    parser.add_argument("--runtime", default="runtime/v2.sqlite3")
    parser.add_argument("--postgres-dsn-file", default="")
    parser.add_argument("--key", required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    if args.postgres_dsn_file:
        dsn = Path(args.postgres_dsn_file).read_text(encoding="utf-8").strip()
        if not dsn:
            raise SystemExit("v2 anchor PostgreSQL DSN is empty")
        store = PostgresRuntimeStoreV2.from_dsn(dsn)
    else:
        store = RuntimeStoreV2(Path(args.runtime))
    try:
        if isinstance(store, PostgresRuntimeStoreV2):
            with store.connection.cursor() as cursor:
                cursor.execute("SELECT event_hash FROM v2_events ORDER BY sequence DESC LIMIT 1")
                row = cursor.fetchone()
        else:
            row = store.db.execute(
                "SELECT event_hash FROM v2_events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
        if row is None:
            raise SystemExit("no v2 audit event exists yet")
        if not store.verify_event_chain():
            raise SystemExit("v2 audit chain is invalid")
        anchor = AuditAnchorWriter(args.key, args.destination).anchor(
            str(row[0]), at=datetime.now(UTC)
        )
        print(f"V2_AUDIT_ANCHOR={anchor.anchor_id} HEAD={anchor.head_event_hash}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
