"""PostgreSQL-backed coordination for account runs."""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg


def advisory_lock_key(namespace: str, account: str) -> int:
    """Return a deterministic signed 64-bit PostgreSQL advisory-lock key."""
    digest = hashlib.blake2b(
        f"{namespace}:{account}".encode("utf-8"), digest_size=8
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


@asynccontextmanager
async def account_run_lock(
    pool: asyncpg.Pool,
    account: str,
) -> AsyncIterator[bool]:
    """Hold a non-blocking cross-process lock for one account run.

    The acquired connection remains reserved for the lifetime of the lock;
    PostgreSQL advisory locks are session-scoped and must be unlocked on the
    same connection.
    """
    key = advisory_lock_key("naukri-run", account)
    async with pool.acquire() as connection:
        acquired = bool(await connection.fetchval("SELECT pg_try_advisory_lock($1)", key))
        try:
            yield acquired
        finally:
            if acquired:
                await connection.fetchval("SELECT pg_advisory_unlock($1)", key)
