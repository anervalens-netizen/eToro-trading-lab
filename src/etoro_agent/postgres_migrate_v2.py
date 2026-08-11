from __future__ import annotations

import argparse
from pathlib import Path

from .postgres_runtime_v2 import PostgresRuntimeStoreV2


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply verified eToro v2 PostgreSQL migrations")
    parser.add_argument("--dsn-file", required=True)
    parser.add_argument("--set-role", default="")
    args = parser.parse_args()
    dsn = Path(args.dsn_file).read_text(encoding="utf-8").strip()
    if not dsn:
        raise SystemExit("v2 migration DSN file is empty")
    store = PostgresRuntimeStoreV2.from_dsn(dsn)
    try:
        if args.set_role:
            if args.set_role != "etoro-v2-owner":
                raise SystemExit("v2 migration role is not the fixed owner role")
            with store.connection.cursor() as cursor:
                cursor.execute('SET ROLE "etoro-v2-owner"')
        store.migrate()
        store.require_schema()
        print("ETORO_V2_SCHEMA_OK version=2")
    finally:
        store.close()


if __name__ == "__main__":
    main()
