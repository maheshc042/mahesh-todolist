"""
Notifications.

Design decisions:

- **Interface + composite.** `Notifier` is an ABC; `CompositeNotifier` fans out
  to every configured channel and swallows individual channel errors. A dead
  Telegram token must never fail a successful run.
- **Console channel always on.** It guarantees the run summary lands in the
  Docker logs even with no external channel configured.
- **Telegram over raw HTTP.** The Bot API is two endpoints; pulling in a bot
  framework for `sendMessage` would add a heavy dependency and a background
  polling loop we do not want inside a batch job.
- Messages are HTML-escaped and hard-truncated to Telegram's 4096-char limit,
  because a run that applies to 40 jobs will otherwise silently fail to send.
"""

from __future__ import annotations

import html
from abc import ABC, abstractmethod

import httpx

from ..core.models import RunStats
from ..logging_setup import get_logger

log = get_logger(__name__)

TELEGRAM_LIMIT = 4_000


class Notifier(ABC):
    @abstractmethod
    async def send(self, title: str, body: str, *, is_error: bool = False) -> None: ...

    async def close(self) -> None:  # pragma: no cover - optional hook
        return None


class ConsoleNotifier(Notifier):
    async def send(self, title: str, body: str, *, is_error: bool = False) -> None:
        log_fn = log.error if is_error else log.info
        log_fn("notify.console", title=title, body=body)


class TelegramNotifier(Notifier):
    def __init__(self, bot_token: str, chat_id: str, timeout_s: float = 15.0) -> None:
        self.url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.chat_id = chat_id
        self.timeout_s = timeout_s

    async def send(self, title: str, body: str, *, is_error: bool = False) -> None:
        prefix = "FAILED" if is_error else "OK"
        text = f"<b>[{prefix}] {html.escape(title)}</b>\n<pre>{html.escape(body)}</pre>"
        if len(text) > TELEGRAM_LIMIT:
            text = text[: TELEGRAM_LIMIT - 12] + "\n…</pre>"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.post(
                    self.url,
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
            if response.status_code >= 400:
                log.warning(
                    "notify.telegram_rejected",
                    status=response.status_code,
                    body=response.text[:200],
                )
            else:
                log.info("notify.telegram_sent", chars=len(text))
        except Exception as exc:
            log.warning("notify.telegram_failed", error=str(exc)[:200])


class CompositeNotifier(Notifier):
    def __init__(self, channels: list[Notifier]) -> None:
        self.channels = channels

    async def send(self, title: str, body: str, *, is_error: bool = False) -> None:
        for channel in self.channels:
            try:
                await channel.send(title, body, is_error=is_error)
            except Exception as exc:  # a broken channel must not break the run
                log.warning(
                    "notify.channel_failed",
                    channel=type(channel).__name__,
                    error=str(exc)[:200],
                )


def build_notifier(
    *, telegram_enabled: bool, bot_token: str, chat_id: str
) -> CompositeNotifier:
    channels: list[Notifier] = [ConsoleNotifier()]
    if telegram_enabled and bot_token and chat_id:
        channels.append(TelegramNotifier(bot_token, chat_id))
    else:
        log.info("notify.telegram_disabled")
    return CompositeNotifier(channels)


def format_run_summary(
    stats: RunStats,
    *,
    duration_s: float,
    run_id: int | None,
    include_job_list: bool = True,
    max_jobs: int = 15,
    error: str | None = None,
) -> str:
    """Plain text, aligned, greppable — reads well in both Telegram and logs."""
    lines = [
        f"run id        : {run_id if run_id is not None else 'n/a'}",
        f"duration      : {int(duration_s // 60)}m {int(duration_s % 60)}s",
        f"scraped       : {stats.scraped}",
        f"considered    : {stats.considered}",
        f"filtered out  : {stats.filtered_out}",
        f"APPLIED       : {stats.applied}",
        f"already applied: {stats.already_applied}",
        f"external skip : {stats.external}",
        f"needs review  : {stats.needs_review}",
        f"failed        : {stats.failed}",
    ]

    if stats.per_profile:
        lines.append("")
        lines.append("per profile:")
        for profile, counters in stats.per_profile.items():
            applied = counters.get("applied", 0)
            failed = counters.get("failed", 0)
            review = counters.get("needs_review", 0)
            lines.append(f"  {profile}: applied={applied} failed={failed} review={review}")

    if include_job_list and stats.applied_jobs:
        lines.append("")
        lines.append(f"✅ Applied Jobs ({len(stats.applied_jobs)}):")
        for job in stats.applied_jobs[:max_jobs]:
            lines.append(f"  - {job.get('title', '?')} @ {job.get('company', '?')}")
            form_links = job.get("form_links", [])
            if form_links:
                lines.append(f"    ⚠️ Form Required: {', '.join(form_links)}")
        remaining = len(stats.applied_jobs) - max_jobs
        if remaining > 0:
            lines.append(f"  … and {remaining} more")

    if stats.external_jobs:
        lines.append("")
        lines.append(f"🔗 Action Required / External Jobs ({len(stats.external_jobs)}):")
        for job in stats.external_jobs[:10]:
            title = job.get("title", "?")
            company = job.get("company", "?")
            url = job.get("url", "")
            form_links = job.get("form_links", [])
            lines.append(f"  - {title} @ {company}")
            if form_links:
                lines.append(f"    Forms: {', '.join(form_links)}")
            if url:
                lines.append(f"    URL: {url}")

    if stats.walkin_alerts:
        lines.append("")
        lines.append(f"📍 Bangalore Walk-in Radar ({len(stats.walkin_alerts)}):")
        for job in stats.walkin_alerts[:10]:
            title = job.get("title", "?")
            company = job.get("company", "?")
            location = job.get("location", "")
            url = job.get("url", "")
            lines.append(f"  - {title} @ {company} ({location})")
            if url:
                lines.append(f"    URL: {url}")

    if stats.errors:
        lines.append("")
        lines.append("errors:")
        for message in stats.errors[:5]:
            lines.append(f"  ! {message[:160]}")

    if error:
        lines.append("")
        lines.append(f"fatal: {error[:300]}")

    return "\n".join(lines)

