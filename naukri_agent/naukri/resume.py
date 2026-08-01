"""
Per-profile resume switching.

Naukri attaches whatever resume is currently on the profile — there is no
per-application resume picker in the Easy Apply flow. So "select the right
resume for this profile" means: upload the profile's resume file to the account
BEFORE applying with that profile, then apply.

Design decisions:

- **Switch once per profile, not per job.** Uploading is slow (~10s) and Naukri
  rate-limits profile updates. The orchestrator calls `ensure_resume()` once when
  it starts a profile; every job in that profile then inherits the right CV.
- **Idempotent.** We read the currently-attached filename first and skip the
  upload when it already matches, so a run that applies to 3 profiles performs at
  most 3 uploads — often zero.
- **Non-fatal.** A failed resume swap logs a warning and continues with the
  existing resume rather than aborting the whole run; applying with a slightly
  generic CV beats not applying at all. Set `strict_resume` to change that.
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
        return True

        current = await self.current_resume_name()
        if not force_upload and current and path.stem.lower() in current.lower():
            log.info("resume.already_current", profile=profile, resume=current)
            return True

        log.info("resume.uploading", profile=profile, file=path.name, previous=current)
        try:
            await self.page.goto(S.PROFILE_URL, wait_until="domcontentloaded", timeout=45_000)
            await dismiss_overlays(self.page)

            file_input = None
            for selector in S.RESUME_UPLOAD_INPUT:
                locator = self.page.locator(selector).first
                if await locator.count():
                    file_input = locator
                    break
            if file_input is None:
                log.warning("resume.input_not_found", profile=profile)
                return False

            # The real <input type=file> is visually hidden behind a styled label,
            # so set the files directly instead of clicking.
            await file_input.set_input_files(str(path))
            success = await first_visible(self.page, S.RESUME_SUCCESS, timeout_ms=25_000)
            if success is None:
                log.warning("resume.upload_unconfirmed", profile=profile, file=path.name)
                return False

            self._current = path.name
            log.info("resume.upload_success", profile=profile, file=path.name)
            return True
        except Exception as exc:
            log.warning("resume.upload_failed", profile=profile, error=str(exc)[:250])
            return False
