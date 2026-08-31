"""
asyncpg connection pool.

Design decisions:
- One process-wide pool (`create_pool`) instead of per-query connections: Neon
  charges for connection setup latency and the agent issues many small writes.
- `ssl` is passed as an object rather than via the DSN because asyncpg does not
  understand libpq's `sslmode=require&channel_binding=require` query params that
  Neon includes in its pooled URL.
- `search_path` is set once per connection via `init`, so every query in the
  repository can use unqualified table names while still living in a dedicated
  `naukri` schema (keeps the agent's tables away from `neon_auth`/app tables).
- `statement_cache_size=0` is required for pooled (PgBouncer) Neon endpoints,
  which do not support prepared statement reuse across sessions.
"""

from __future__ import annotations

import ssl as ssl_module
from typing import Any

import asyncpg

from ..config import Settings, get_settings
from ..logging_setup import get_logger

log = get_logger(__name__)

_pool: asyncpg.Pool | None = None


def _ssl_context(settings: Settings) -> Any:
    if not settings.requires_ssl:
        return None
    ctx = ssl_module.create_default_context()
    if settings.db_ssl_insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl_module.CERT_NONE
        log.warning("db.tls_verification_disabled")
    return ctx


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool

    settings = get_settings()
    schema = settings.db_schema

    async def _init(conn: asyncpg.Connection) -> None:
        await conn.execute(f'SET search_path TO "{schema}", public')

    _pool = await asyncpg.create_pool(
        dsn=settings.asyncpg_dsn,
        ssl=_ssl_context(settings),
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        statement_cache_size=0,
        max_inactive_connection_lifetime=60.0,
        command_timeout=30.0,
        init=_init,
        server_settings={
            "application_name": "naukri-agent",
            "search_path": f'"{schema}", public',
        },
    )
    log.info("db.pool_ready", schema=schema, max_size=settings.db_pool_max_size)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("db.pool_closed")
