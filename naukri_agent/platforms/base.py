"""
Base interface for all Job Platforms.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

from playwright.async_api import Page

from ..config import JobProfile
from ..core.models import ApplyOutcome, FilterDecision, Job


class BaseJobPlatform(ABC):
    def __init__(self, page: Page, account_key: str):
        self.page = page
        self.account_key = account_key

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Returns the identifier for the database (e.g., 'naukri', 'instahyre')."""
        pass

    @abstractmethod
    async def ensure_logged_in(self) -> bool:
        """
        Authenticate the session. 
        Returns True if logged in successfully, raises FatalAgentError if blocked.
        """
        pass

    @abstractmethod
    async def fetch_jobs(self, profile: JobProfile, exclude_job_ids: set[str]) -> list[Job]:
        """
        Scrape the platform's feed/search and return a list of standard Job objects.
        Should skip jobs present in `exclude_job_ids`.
        """
        pass

    @abstractmethod
    async def apply_to_job(
        self, 
        job: Job, 
        profile_name: str, 
        pre_submit_check: Callable[[Job], FilterDecision] | None = None
    ) -> ApplyOutcome:
        """
        Execute the apply flow for a single job.
        Must handle its own UI elements, Chatbots, or popup logic.
        """
        pass

    async def handle_messages(self) -> None:
        """
        Optional post-apply hook to handle asynchronous platform messages/questionnaires.
        Does nothing by default. Platforms like Cutshort will override this.
        """
        pass

