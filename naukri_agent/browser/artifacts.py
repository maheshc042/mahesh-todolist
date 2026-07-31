"""
Failure artifacts.

Design decision: on any unexpected page state we capture BOTH a full-page PNG
and the DOM snapshot. Naukri ships UI changes without notice; a screenshot tells
you what a human would have seen, the HTML tells you which selector broke. Files
are namespaced `artifacts/<date>/<run>/<profile>_<jobid>_<label>.png` so they are
trivially greppable and easy to prune with a cron/`find -mtime`.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import Page

from ..logging_setup import get_logger

log = get_logger(__name__)

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(part: str, limit: int = 48) -> str:
    return _SAFE.sub("-", part).strip("-")[:limit] or "na"


class ArtifactStore:
    def __init__(self, base_dir: Path, run_id: int | str = "adhoc") -> None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.dir = Path(base_dir) / day / f"run-{run_id}"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, label: str, profile: str, job_id: str, ext: str) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%H%M%S")
        name = f"{stamp}_{_safe(profile)}_{_safe(job_id)}_{_safe(label)}.{ext}"
        return self.dir / name

    async def screenshot(
        self,
        page: Page,
        label: str,
        profile: str = "global",
        job_id: str = "none",
        full_page: bool = True,
    ) -> str | None:
        if page.is_closed():
            return None
        target = self._path(label, profile, job_id, "png")
        try:
            await page.screenshot(path=str(target), full_page=full_page, timeout=15_000)
            log.debug("artifact.screenshot", path=str(target))
            return str(target)
        except Exception as exc:  # capturing evidence must never break the run
            log.warning("artifact.screenshot_failed", error=str(exc), label=label)
            return None

    async def dump_html(
        self, page: Page, label: str, profile: str = "global", job_id: str = "none"
    ) -> str | None:
        if page.is_closed():
            return None
        target = self._path(label, profile, job_id, "html")
        try:
            target.write_text(await page.content(), encoding="utf-8")
            return str(target)
        except Exception as exc:
            log.warning("artifact.html_failed", error=str(exc), label=label)
            return None

    async def capture_failure(
        self, page: Page, label: str, profile: str = "global", job_id: str = "none"
    ) -> str | None:
        """Screenshot + HTML in one call; returns the screenshot path."""
        shot = await self.screenshot(page, label, profile, job_id)
        await self.dump_html(page, label, profile, job_id)
        return shot
