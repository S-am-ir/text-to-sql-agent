"""
Database connection layer.
Two seperate concerns, intentionally kept seperate:
    1. SQLDatabase (langchain_community) - used by the agent tools.
    2. psycopg ConnectionPool - used exculsively by the LangGraph PostgresSaver.
"""

from __future__ import annotations
import logging
from functools import lru_cache
from langchain_community.utilities import SQLDatabase
from psycopg_pool import ConnectionPool
from config import settings

logger = logging.getLogger(__name__)

@lru_cache(maxsize=1)
def get_sql_db() -> SQLDatabase:
    """
    Return a cached SQLDatabase instance.
    The underlying SQLAlchemy engine is configured with execution_options
    so that no writes can slip through even if the DB user has write rights.
    """
    db = SQLDatabase.from_uri(
        settings.DATABASE_URL,
        sample_rows_in_table_info=3,     # shows 3 sample rows in schema info → agent groks data faster
        include_tables=None,            # include all tables
    )
    logger.info("SQLDatabase connected. Tables: %s", db.get_usable_table_names())

    return db

# ---------------------------------------------------------------------------
# ConnectionPool singleton (PostgresSaver checkpointer)
# ---------------------------------------------------------------------------

_pool: ConnectionPool | None = None

def get_checkpointer_pool() -> ConnectionPool:
    """
    Return (and lazily create) a psycopg ConnectionPool used by the 
    LangGraph PostgresSaver. The pool is module-level so it persists across
    Streamlit re-runs.
    """
    global _pool
    if _pool is None:
        # Strip SQLAlchemy dialect prefix - psycopg3 wants a plain dsn
        dsn = (
            settings.DATABASE_URL
            .replace("postgresql+psycopg://", "postgresql://")
        )
        _pool = ConnectionPool(
            conninfo=dsn,
            max_size=20,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0.
            },
            open=True,
        )
        logger.info("psycopg ConnectionPool created for PostgresSaver.")
    return _pool

def close_pool() -> None:
    """Gracefully close the pool (call on app shutdown if needed)."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None

# ---------------------------------------------------------------------------
# 3. Write DSN helper  (execute_write tool)
# ---------------------------------------------------------------------------

def get_write_dsn() -> str:
    """
    Return a plain psycopg-compatible DSN for short-lived write connections.
    Called only by execute_write — intentionally not pooled so each write
    operation gets its own connection + transaction boundary.
    """
    return _to_psycopg_dsn(settings.DATABASE_URL)


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------
def _to_psycopg_dsn(url: str) -> str:
    """Strip SQLAlchemy dialect prefix so psycopg3 can use the DSN directly."""
    for prefix in ("postgresql+psycopg://", "postgres+psycopg://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    return url