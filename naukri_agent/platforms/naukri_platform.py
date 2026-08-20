"""
Wrapper to make the existing Naukri engine fit the Multi-Platform interface.
"""
from __future__ import annotations

from typing import Callable

from playwright.async_api import Page

from ..browser.artifacts import ArtifactStore
from ..browser.manager import BrowserManager
from ..config import AgentConfig, JobProfile, NaukriAccount
from ..core.answers import AnswerEngine
from ..core.models import ApplyOutcome, FilterDecision, Job
from ..core.runtime_metrics import RuntimeMetrics
from ..naukri.apply import ApplyEngine
from ..naukri.auth import NaukriAuth
from ..naukri.search import JobSearcher
from .base import BaseJobPlatform


class NaukriPlatform(BaseJobPlatform):
    def __init__(
        self,
        page: Page,
        browser: BrowserManager,
        account: NaukriAccount,
        config: AgentConfig,
        artifacts: ArtifactStore,
        answers: AnswerEngine,
        metrics: RuntimeMetrics | None = None,
    ):
        super().__init__(page, account.key)
        self.browser = browser
        self.account = account
        self.config = config
        self.artifacts = artifacts
        self.answers = answers
        self.metrics = metrics

        self.auth = NaukriAuth(browser, account.email, account.password, artifacts)
        self.searcher = JobSearcher(
            page,
            config.browser.min_action_delay_ms,
            config.browser.max_action_delay_ms,
            metrics=metrics,
        )
        self.applier = ApplyEngine(
            page,
            answers,
            artifacts,
            dry_run=False,
            nav_timeout_ms=config.browser.navigation_timeout_ms,
            attempts=config.run.max_retries_per_job,
            max_questions=15,
            metrics=metrics,
        )

    @property
    def platform_name(self) -> str:
        return "naukri"

    async def ensure_logged_in(self) -> bool:
        await self.auth.ensure_logged_in(self.page)
        return True

    async def fetch_jobs(self, profile: JobProfile, exclude_job_ids: set[str]) -> list[Job]:
        if profile.use_recommended and self.config.recommended.enabled:
            return await self.searcher.search_recommended(self.config.recommended, exclude_job_ids=exclude_job_ids)
        return []

    async def apply_to_job(
        self,
        job: Job,
        profile_name: str,
        pre_submit_check: Callable[[Job], FilterDecision] | None = None,
    ) -> ApplyOutcome:
        return await self.applier.apply(job, profile_name, pre_submit_check=pre_submit_check)
