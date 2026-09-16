"""
Per-profile resume switching — currently DISABLED by operator choice.

Naukri attaches whatever resume is currently on the profile — there is no
per-application resume picker in the Easy Apply flow. Automatic switching would
mean: upload the profile's resume file to the account BEFORE applying with that
profile, then apply.

Design decisions (for when this is re-enabled):

- **Switch once per profile, not per job.** Uploading is slow (~10s) and Naukri
  rate-limits profile updates.
- **Idempotent.** Read the currently-attached filename first and skip the
  upload when it already matches.
- **Non-fatal.** A failed swap must log and continue, never abort the run.

Current state: `ensure_resume()` is a deliberate no-op returning True.
Resumes are managed manually on Naukri; each account already carries the right
CV for its job family. Do not "fix" this method without asking the operator.
"""

from __future__ import annotations

from pathlib import Path

from playwright.async_api import Page

from ..browser.resilience import dismiss_overlays, first_visible, safe_text
from ..logging_setup import get_logger
from . import selectors as S

log = get_logger(__name__)


class ResumeManager:
    def __init__(self, page: Page, resume_dir: Path) -> None:
        self.page = page
        self.resume_dir = Path(resume_dir)
        self._current: str | None = None

    def _resolve(self, resume_file: str) -> Path | None:
        candidate = Path(resume_file)
        if candidate.is_absolute():
            return candidate if candidate.exists() else None

        if candidate.exists():
            return candidate.resolve()

        in_resume_dir = self.resume_dir / resume_file
        if in_resume_dir.exists():
            return in_resume_dir.resolve()

        if candidate.parts and candidate.parts[0] == self.resume_dir.name:
            stripped = self.resume_dir.parent / candidate
            if stripped.exists():
                return stripped.resolve()

        return None

    async def current_resume_name(self) -> str:
        if self._current is not None:
            return self._current
        try:
            await self.page.goto(S.PROFILE_URL, wait_until="domcontentloaded", timeout=45_000)
            await dismiss_overlays(self.page)
            element = await first_visible(self.page, S.RESUME_CURRENT_NAME, timeout_ms=8_000)
            self._current = (await safe_text(element)).strip()
        except Exception as exc:
            log.warning("resume.read_current_failed", error=str(exc)[:200])
            self._current = ""
        return self._current or ""

    async def ensure_resume(self, resume_file: str | None, profile: str, force_upload: bool = True) -> bool:
        """Disabled as requested — manual resume management on Naukri."""
        if not resume_file:
            return True

        path = self._resolve(resume_file)
        if not path:
            log.warning("resume.file_missing", file=resume_file, profile=profile)
            return True

        return True
