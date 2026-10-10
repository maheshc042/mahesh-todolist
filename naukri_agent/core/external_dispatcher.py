"""
Sidekick dispatcher (contract v1).

Consumes the `external_dispatch_queue` outbox and POSTs rows to Sidekick's
`POST /apply`. Design decisions:

- Idempotency-Key = sha1(normalized_url): redelivery after a crash can never
  double-queue on Sidekick's side (its 90-day dedup is the backstop).
- Backoff 30s -> 5m -> 30m, then dead-letter + Telegram alert. A dead
  Sidekick never blocks a run and never loses a job.
- Dry-run / kill-switch (RunPolicy.may_mutate == False): enqueue only, the
  dispatcher refuses to POST.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from ..logging_setup import get_logger

log = get_logger(__name__)

CONTRACT_VERSION = "1"
BACKOFF_SCHEDULE = (
    timedelta(seconds=30),
    timedelta(minutes=5),
    timedelta(minutes=30),
)


def idempotency_key(url: str) -> str:
    return hashlib.sha1(normalize_dispatch_url(url).encode()).hexdigest()


def normalize_dispatch_url(url: str) -> str:
    """Mirrors Sidekick's normalize_job_url so idempotency keys agree.

    Keeps identity params (gh_jid, job_id, id), drops tracking junk.
    """
    try:
        from urllib.parse import parse_qsl, urlencode

        parsed = urlparse(url.strip())
        path = parsed.path.rstrip("/") or "/"
        kept = [
            (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k.lower() in ("gh_jid", "job_id", "id", "p", "job")
        ]
        return urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path, "", urlencode(kept), "",
        )).lower()
    except Exception:
        return url.strip().lower()


def next_retry_delay(attempts: int) -> timedelta | None:
    """Backoff slot for a failed attempt count, or None when dead."""
    if attempts < len(BACKOFF_SCHEDULE):
        return BACKOFF_SCHEDULE[attempts]
    return None


def _source_platform(value: str) -> str:
    clean = re.sub(r"[^a-z]", "", (value or "").lower())
    return clean or "unknown"


class ExternalJobDispatcher:
    def __init__(
        self,
        repo: Any,
        api_url: str = "http://127.0.0.1:8000",
        request_timeout_s: int = 15,
    ) -> None:
        self.repo = repo
        self.api_url = api_url.rstrip("/")
        self.request_timeout_s = request_timeout_s

    def _payload(self, row: dict[str, Any]) -> dict[str, Any]:
        meta = row.get("source_metadata") or {}
        if isinstance(meta, str):
            import json

            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        return {
            "url": row["url"],
            "company_name": row.get("company") or "Company",
            "job_title": row.get("title") or "AI Engineer",
            "source": _source_platform(str(row.get("platform") or "")),
            "posted_days_ago": meta.get("posted_days_ago"),
            "source_job_id": row.get("job_id"),
        }

    async def dispatch_row(self, row: dict[str, Any]) -> bool:
        """POST one claimed row. Returns True on accepted/queued/skipped."""
        url = f"{self.api_url}/apply"
        headers = {
            "X-Sidekick-Contract": CONTRACT_VERSION,
            "Idempotency-Key": idempotency_key(str(row["url"])),
        }
        try:
            async with httpx.AsyncClient(timeout=self.request_timeout_s) as client:
                resp = await client.post(url, json=self._payload(row), headers=headers)
            if resp.status_code in (200, 202):
                await self.repo.mark_dispatched(int(row["id"]))
                log.info(
                    "sidekick.dispatched",
                    job_id=row.get("job_id"),
                    status=resp.status_code,
                )
                return True
            if resp.status_code == 422:
                # Validation failures can never succeed on retry: dead-letter
                # immediately instead of burning the 30s -> 5m -> 30m ladder.
                await self.repo.mark_dispatch_dead(
                    int(row["id"]), f"HTTP 422: {resp.text[:200]}"
                )
                log.error("sidekick.dispatch_dead", job_id=row.get("job_id"), error="HTTP 422 validation")
                return False
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"[:500]
            delay = next_retry_delay(int(row.get("attempts", 0)))
            if delay is None:
                await self.repo.mark_dispatch_dead(int(row["id"]), err)
                log.error("sidekick.dispatch_dead", job_id=row.get("job_id"), error=err)
            else:
                await self.repo.mark_dispatch_failed(
                    int(row["id"]), err, datetime.now(UTC) + delay
                )
                log.warning(
                    "sidekick.dispatch_retry",
                    job_id=row.get("job_id"),
                    error=err,
                    retry_in_s=int(delay.total_seconds()),
                )
            return False

    async def flush_pending(self, limit: int = 25) -> dict[str, int]:
        """Claim due rows and dispatch them. Returns {dispatched, failed}."""
        rows = await self.repo.claim_pending_dispatches(limit)
        result = {"dispatched": 0, "failed": 0}
        for row in rows:
            if await self.dispatch_row(row):
                result["dispatched"] += 1
            else:
                result["failed"] += 1
        if rows:
            log.info("sidekick.flush_done", **result, claimed=len(rows))
        return result
