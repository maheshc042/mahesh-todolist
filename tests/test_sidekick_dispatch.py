"""
Unit tests for the Sidekick handoff (contract v1): idempotency keys,
backoff schedule, payload mapping, and dispatch success/failure paths.
All network and DB access is faked; no live services required.
"""
import asyncio
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from naukri_agent.core.external_dispatcher import (
    ExternalJobDispatcher,
    idempotency_key,
    next_retry_delay,
    normalize_dispatch_url,
)
from naukri_agent.core.orchestrator import resolve_dispatch_url


def _row(**over):
    base = {
        "id": 7,
        "job_id": "hc-abc123",
        "url": "https://boards.greenhouse.io/acme/jobs/42?utm_source=x",
        "company": "Acme",
        "title": "AI Engineer",
        "platform": "hiringcafe",
        "profile": "AI / Python Engineer",
        "account": "primary",
        "source_metadata": {"posted_days_ago": 1},
        "attempts": 0,
    }
    base.update(over)
    return base


class FakeResp:
    def __init__(self, status_code=202, text=""):
        self.status_code = status_code
        self.text = text


class FakeClient:
    def __init__(self, resp=None, exc=None):
        self.resp = resp or FakeResp()
        self.exc = exc
        self.seen = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        self.seen.append({"url": url, "json": json, "headers": headers})
        if self.exc:
            raise self.exc
        return self.resp


class TestNormalize(unittest.TestCase):
    def test_tracking_stripped_identity_kept(self):
        self.assertEqual(
            normalize_dispatch_url("https://boards.greenhouse.io/acme/jobs/42?utm_source=x"),
            normalize_dispatch_url("https://boards.greenhouse.io/acme/jobs/42"),
        )
        self.assertNotEqual(
            normalize_dispatch_url("https://boards.greenhouse.io/acme/jobs/42?gh_jid=9"),
            normalize_dispatch_url("https://boards.greenhouse.io/acme/jobs/42"),
        )

    def test_idempotency_stable(self):
        url = "https://jobs.lever.co/acme/abc-123?src=hb"
        self.assertEqual(idempotency_key(url), idempotency_key(url))
        self.assertEqual(len(idempotency_key(url)), 40)


class TestBackoff(unittest.TestCase):
    def test_schedule_then_dead(self):
        self.assertEqual(next_retry_delay(0), timedelta(seconds=30))
        self.assertEqual(next_retry_delay(1), timedelta(minutes=5))
        self.assertEqual(next_retry_delay(2), timedelta(minutes=30))
        self.assertIsNone(next_retry_delay(3))
        self.assertIsNone(next_retry_delay(99))


class TestDispatch(unittest.TestCase):
    def _dispatcher(self, client):
        repo = MagicMock()
        repo.mark_dispatched = AsyncMock()
        repo.mark_dispatch_failed = AsyncMock()
        repo.mark_dispatch_dead = AsyncMock()
        disp = ExternalJobDispatcher(repo=repo, api_url="http://127.0.0.1:8000")
        patcher = patch(
            "naukri_agent.core.external_dispatcher.httpx.AsyncClient",
            return_value=client,
        )
        return disp, repo, patcher

    def test_success_marks_dispatched(self):
        client = FakeClient(FakeResp(202))
        disp, repo, patcher = self._dispatcher(client)
        with patcher:
            ok = asyncio.run(disp.dispatch_row(_row()))
        self.assertTrue(ok)
        repo.mark_dispatched.assert_awaited_once_with(7)
        sent = client.seen[0]
        self.assertTrue(sent["url"].endswith("/apply"))
        self.assertEqual(sent["headers"]["X-Sidekick-Contract"], "1")
        self.assertEqual(len(sent["headers"]["Idempotency-Key"]), 40)
        self.assertEqual(sent["json"]["source"], "hiringcafe")
        self.assertEqual(sent["json"]["posted_days_ago"], 1)
        self.assertEqual(sent["json"]["source_job_id"], "hc-abc123")

    def test_http_error_retries(self):
        client = FakeClient(FakeResp(500, "boom"))
        disp, repo, patcher = self._dispatcher(client)
        with patcher:
            ok = asyncio.run(disp.dispatch_row(_row()))
        self.assertFalse(ok)
        repo.mark_dispatch_failed.assert_awaited_once()

    def test_422_dead_immediately_no_retry(self):
        """Validation errors can never succeed on retry: straight to dead,
        without consuming the 30s -> 5m -> 30m ladder."""
        client = FakeClient(FakeResp(422, "bad url"))
        disp, repo, patcher = self._dispatcher(client)
        with patcher:
            ok = asyncio.run(disp.dispatch_row(_row()))
        self.assertFalse(ok)
        repo.mark_dispatch_dead.assert_awaited_once()
        repo.mark_dispatch_failed.assert_not_called()

    def test_source_values_pass_through(self):
        """Every sender platform must survive the source mapping unchanged
        (contract enum: hiringcafe/naukri/linkedin/wellfound/instahyre/
        cutshort/manual)."""
        for platform in ("cutshort", "instahyre", "wellfound", "naukri", "linkedin", "hiringcafe"):
            client = FakeClient(FakeResp(202))
            disp, repo, patcher = self._dispatcher(client)
            with patcher:
                asyncio.run(disp.dispatch_row(_row(platform=platform)))
            self.assertEqual(client.seen[0]["json"]["source"], platform)

    def test_exhausted_attempts_dead(self):
        client = FakeClient(exc=ConnectionError("down"))
        disp, repo, patcher = self._dispatcher(client)
        with patcher:
            ok = asyncio.run(disp.dispatch_row(_row(attempts=5)))
        self.assertFalse(ok)
        repo.mark_dispatch_dead.assert_awaited_once()

    def test_flush_counts(self):
        client = FakeClient(FakeResp(202))
        disp, repo, patcher = self._dispatcher(client)
        repo.claim_pending_dispatches = AsyncMock(return_value=[_row(), _row(id=8)])
        with patcher:
            result = asyncio.run(disp.flush_pending(limit=10))
        self.assertEqual(result, {"dispatched": 2, "failed": 0})


class TestResolveDispatchUrl(unittest.TestCase):
    """Contract v1 resolved-URL-only policy: Sidekick must never receive raw
    job-board listing links (verified live: 18 raw naukri.com rows)."""

    def test_prefers_resolved_external_url(self):
        self.assertEqual(
            resolve_dispatch_url("https://boards.greenhouse.io/acme/1",
                                 "https://www.naukri.com/job-listings-1"),
            "https://boards.greenhouse.io/acme/1",
        )

    def test_falls_back_to_job_url_when_resolved(self):
        self.assertEqual(
            resolve_dispatch_url(None, "https://careers.acme.com/jobs/9"),
            "https://careers.acme.com/jobs/9",
        )

    def test_rejects_raw_listing_links(self):
        for raw in ("https://www.naukri.com/job-listings-240926500983",
                    "https://www.linkedin.com/jobs/view/123",
                    "https://www.instahyre.com/candidate/opportunities/"):
            self.assertIsNone(resolve_dispatch_url(None, raw), raw)
            # A resolved external_url still wins even beside a raw job.url.
            self.assertEqual(
                resolve_dispatch_url("https://jobs.lever.co/acme/2", raw),
                "https://jobs.lever.co/acme/2",
            )

    def test_none_when_nothing_usable(self):
        self.assertIsNone(resolve_dispatch_url(None, ""))
        self.assertIsNone(resolve_dispatch_url(None, "not-a-url"))


if __name__ == "__main__":
    unittest.main()
