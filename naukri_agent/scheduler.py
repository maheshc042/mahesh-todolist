"""
Scheduler (daemon mode).

Design decisions:

- **APScheduler AsyncIOScheduler in-process.** The agent is already an asyncio
  program; an in-process cron avoids a second runtime (no system cron, no
  supervisor) and keeps `docker run` as the entire deployment story.
- **`max_instances=1` + `coalesce=True`.** A run can legitimately take 90
  minutes. Without these, a long run overlapping the next trigger would open a
  second browser on the same Naukri account — a guaranteed way to get flagged.
  Coalescing also means a container that was asleep does not fire 5 backlogged
  runs at once.
- **Randomised jitter.** Firing at exactly 09:30:00 every weekday is a
  fingerprint; `jitter_seconds` spreads the start.
- **Graceful SIGTERM/SIGINT.** Docker stop must let an in-flight run finish its
  DB write instead of leaving a `running` row forever.
"""

from __future__ import annotations

import asyncio
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import AgentConfig, Settings, load_config
from .core.orchestrator import Orchestrator
from .logging_setup import get_logger

log = get_logger(__name__)


class AgentScheduler:
    def __init__(self, config: AgentConfig, settings: Settings) -> None:
        self.config = config
        self.settings = settings
        self.scheduler = AsyncIOScheduler(timezone=config.schedule.timezone)
        self._stop = asyncio.Event()
        # A single global lock, NOT one per account: even on staggered crons a
        # long run must never overlap another account's run, because two
        # concurrent Naukri logins from one IP is what triggers the "unusual
        # activity" challenge.
        self._lock = asyncio.Lock()

    async def _job(self, account: str) -> None:
        if self._lock.locked():
            # Belt and braces: max_instances already guards a single job.
            log.warning("scheduler.skipped_overlap", account=account)
            return
        async with self._lock:
            try:
                # Reload YAML every run so config edits apply without a restart.
                config = load_config(self.settings.config_path)
                orchestrator = Orchestrator(
                    config, self.settings, mode="scheduled", account=account
                )
                await orchestrator.run()
            except Exception:
                log.exception("scheduler.run_failed", account=account)

    def _accounts(self) -> list[str]:
        """
        Accounts that have BOTH a profile in config.yaml and credentials in the
        environment. Anything else is logged and skipped rather than scheduled to
        fail every morning.
        """
        configured = set(self.settings.available_accounts())
        wanted = self.config.accounts_in_use()
        usable = [key for key in wanted if key in configured]
        missing = [key for key in wanted if key not in configured]
        if missing:
            log.warning("scheduler.accounts_skipped", accounts=missing)
        return usable

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:  # pragma: no cover - Windows
                pass

    async def start(self) -> None:
        schedule = self.config.schedule
        accounts = self._accounts()
        if not accounts:
            raise RuntimeError(
                "No account has both a profile in config.yaml and credentials in the "
                "environment. Set NAUKRI_EMAIL/NAUKRI_PASSWORD (and _2 for the second "
                "account) and check the `account:` key on each profile."
            )

        # One cron job per account so the two logins can be hours apart.
        for account in accounts:
            cron = schedule.cron_for(account)
            self.scheduler.add_job(
                self._job,
                trigger=CronTrigger.from_crontab(cron, timezone=schedule.timezone),
                args=[account],
                id=f"naukri-auto-apply:{account}",
                name=f"Naukri auto apply ({account})",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=1_800,
                jitter=schedule.jitter_seconds,
                replace_existing=True,
            )
        self.scheduler.start()
        self._install_signal_handlers()

        for account in accounts:
            job = self.scheduler.get_job(f"naukri-auto-apply:{account}")
            log.info(
                "scheduler.started",
                account=account,
                cron=schedule.cron_for(account),
                timezone=schedule.timezone,
                jitter_s=schedule.jitter_seconds,
                next_run=str(job.next_run_time if job else None),
            )

        if schedule.run_on_start:
            log.info("scheduler.run_on_start", accounts=accounts)
            for account in accounts:
                await self._job(account)

        await self._stop.wait()
        log.info("scheduler.stopping")
        self.scheduler.shutdown(wait=True)
        log.info("scheduler.stopped")
