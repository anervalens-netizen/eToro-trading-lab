from __future__ import annotations

from .postgres_store_impl_v2 import *  # noqa: F403
from .postgres_store_impl_v2 import PostgresStoreV2
from .postgres_store_impl_v2 import psycopg as _psycopg


def psycopg_available() -> bool:
    """Return whether the optional PostgreSQL driver is importable."""
    return _psycopg is not None


__all__ = ["PostgresStoreV2", "psycopg_available"]
