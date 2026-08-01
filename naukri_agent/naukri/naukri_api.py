"""
Naukri internal REST API client.

Design decisions
----------------

- **Best-effort, fail-open.** Every public method returns `None` on any error
  (network timeout, 4xx, 5xx, JSON parse failure). The caller never has to
  handle exceptions; a `None` result simply means "fall back to the Playwright
  flow". This is the correct contract for an optimisation layer.

- **JWT extraction from Playwright cookies.** Naukri's internal API requires
  the same session token that the browser carries. We pull it from the page's
  cookies after login rather than maintaining a separate auth flow. This keeps
  the authentication surface area minimal and means the API token is always
  fresh.

- **httpx (already a dependency) over aiohttp.** httpx is already declared in
  pyproject.toml, integrates cleanly with asyncio, and has built-in timeout
  objects. No new dependencies added.

- **Concurrency limited by a semaphore.** The match score calls happen in a
  batch before browser navigation, so we cap concurrency at `max_concurrent`
  (default 3) to avoid rate-limit triggers on Naukri's side.

- **Score cache via DB.** Results are persisted to `match_scores` so re-runs
  within the same session (or across sessions for the same job) skip the HTTP
  call entirely. Cache TTL is 24 hours — a job's relevance doesn't change
  within a day.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from ..logging_setup import get_logger

log = get_logger(__name__)

# Base URL for Naukri's internal job API (undocumented but stable since 2021).
_BASE = "https://www.naukri.com"
_MATCH_SCORE_URL = _BASE + "/jobapi/v3/job/{job_id}/matchscore"

# Headers that mimic the browser's XHR requests. Without these, the API
# returns 403 immediately.
_COMMON_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.naukri.com/",
    "Origin": "https://www.naukri.com",
    "appid": "109",
    "clientid": "d3skt0p",
    "gzip": "true",
    "systemid": "Naukri",
}


@dataclass(slots=True)
class MatchScoreResult:
    """Parsed response from Naukri's matchscore API."""

    job_id: str
    keyskills_score: int       # 0–100; 0 means no keyword overlap at all
    experience_match: bool     # True if the job's exp range includes the profile's years
    overall_score: int         # 0–100 composite score
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_worth_applying(self) -> bool:
        """Conservative gate: only skip jobs with zero skill overlap."""
        return self.keyskills_score > 0

    def to_row(self) -> dict:
        return {
            "job_id": self.job_id,
            "keyskills_score": self.keyskills_score,
            "experience_match": self.experience_match,
            "overall_score": self.overall_score,
        }


class NaukriApiClient:
    """
    Lightweight async client for Naukri's internal REST endpoints.

    Constructed with the session JWT extracted from the browser after login.
    All methods are best-effort and return None on any failure — the caller
    should always have a fallback.
    """

    def __init__(
        self,
        nauk_token: str,
        *,
        timeout_s: float = 5.0,
        max_concurrent: int = 3,
    ) -> None:
        self._token = nauk_token
        self._timeout = httpx.Timeout(timeout_s, connect=3.0)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "NaukriApiClient":
        self._client = httpx.AsyncClient(
            headers={
                **_COMMON_HEADERS,
                "Authorization": self._token,
            },
            timeout=self._timeout,
            follow_redirects=False,
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ---------------------------------------------------------------- public
    async def match_score(self, job_id: str) -> MatchScoreResult | None:
        """
        Fetch the skill-match score for a single job.

        Returns None on any error; the caller should proceed with the job as if
        it passed.
        """
        async with self._semaphore:
            return await self._fetch_score(job_id)

    async def batch_match_scores(
        self,
        job_ids: list[str],
    ) -> dict[str, MatchScoreResult]:
        """
        Fetch match scores for multiple jobs concurrently (rate-limited by the
        semaphore set at construction time).

        Returns a mapping of job_id → result for jobs that returned a valid
        score. Jobs that errored are simply absent from the dict; the caller
        should treat absent jobs as "pass".
        """
        if not job_ids:
            return {}

        tasks = [self._fetch_score(job_id) for job_id in job_ids]
        results = await asyncio.gather(*tasks, return_exceptions=False)

        scores: dict[str, MatchScoreResult] = {}
        for job_id, result in zip(job_ids, results):
            if result is not None:
                scores[job_id] = result
        return scores

    # -------------------------------------------------------------- internal
    async def _fetch_score(self, job_id: str) -> MatchScoreResult | None:
        if not self._client:
            log.warning("naukri_api.client_not_open", job_id=job_id)
            return None
        url = _MATCH_SCORE_URL.format(job_id=job_id)
        try:
            async with self._semaphore:
                resp = await self._client.get(url)

            if resp.status_code == 406:
                # reCAPTCHA required — back off silently, don't spam logs
                log.debug("naukri_api.recaptcha_block", job_id=job_id)
                return None
            if resp.status_code == 401:
                log.warning("naukri_api.session_expired")
                return None
            if resp.status_code != 200:
                log.debug(
                    "naukri_api.non_200",
                    job_id=job_id,
                    status=resp.status_code,
                )
                return None

            data: dict = resp.json()
            keyskills = int(data.get("Keyskills") or data.get("keyskills") or 0)
            overall = int(data.get("Overall") or data.get("overall") or 0)
            exp_match = bool(data.get("experience") or data.get("Experience"))

            result = MatchScoreResult(
                job_id=job_id,
                keyskills_score=keyskills,
                experience_match=exp_match,
                overall_score=overall,
            )
            log.debug(
                "naukri_api.score",
                job_id=job_id,
                keyskills=keyskills,
                overall=overall,
                exp_match=exp_match,
            )
            return result

        except httpx.TimeoutException:
            log.debug("naukri_api.timeout", job_id=job_id)
            return None
        except Exception as exc:
            log.debug("naukri_api.error", job_id=job_id, error=str(exc)[:120])
            return None


# ------------------------------------------------------------------- helpers

async def extract_naukri_token(page) -> str | None:
    """
    Pull the Naukri session JWT from the Playwright page's cookies.

    Naukri uses a cookie named `nauk_at` (access token) for authenticated
    API calls. If that cookie is absent we return None and the API client
    construction is skipped.
    """
    try:
        cookies = await page.context.cookies("https://www.naukri.com")
        for cookie in cookies:
            name = (cookie.get("name") or "").lower()
            if name in ("nauk_at", "nkpref", "nktoken"):
                token = cookie.get("value", "")
                if token:
                    log.debug("naukri_api.token_found", cookie_name=name)
                    return token
        log.info("naukri_api.no_token_cookie")
        return None
    except Exception as exc:
        log.warning("naukri_api.token_extract_failed", error=str(exc)[:120])
        return None
