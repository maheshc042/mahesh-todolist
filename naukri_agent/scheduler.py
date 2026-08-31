"""
Long-lived scheduler (the container's default command).

Design decisions
----------------

- **One cron job per account, never one job for both.** Two Naukri logins from
  the same IP inside the same window is the pattern most likely to trigger the
  "unusual activity" challenge, so `schedule.cron_by_account` staggers them and
  this module registers them as independent triggers.
- **A process-wide lock serialises runs anyway.** Even if two crons overlap (a
  long run, a misfire catch-up), only one browser session exists at a time:
  Chromium is memory hungry and concurrent applies make the "already applied"
  check racy.
- **`max_instances=1` + `coalesce=True` + `misfire_grace_time`.** If the machine
  was asleep or a run overran, APScheduler must fire once when it wakes up, not
  five times in a row.
- **Jitter.** Firing at exactly 10:00:00 every weekday for months is a signature.
  `schedule.jitter_seconds` spreads the real start over a window.
- **A crashed run never kills the daemon.** Each tick catches everything, logs it
  and waits for the next trigger; the notifier has already told the user.
- **SIGTERM/SIGINT shut down cleanly** so `docker stop` does not leak a Chromium
  process or a half-written `runs` row.
"""

from __future__ import annotations

import asyncio
import signal
from contextlib import suppress

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import AgentConfig, ConfigError, Settings
from .core.orchestrator import Orchestrator
from .db.locks import account_run_lock
from .db.pool import close_pool, get_pool
from .logging_setup import clear_context, get_logger

log = get_logger(__name__)


class AgentScheduler:
    def __init__(
        self,
        config: AgentConfig,
        settings: Settings,
        *,
        only_profiles: list[str] | None = None,
        dry_run: bool = False,
    ) -> None:
        self.config = config
        self.settings = settings
        self.only_profiles = only_profiles
        self.dry_run = dry_run
        self.scheduler = AsyncIOScheduler(timezone=config.schedule.timezone)
        # Serialises every run in this process regardless of trigger overlap.
        self._lock = asyncio.Lock()
        self._stopping = asyncio.Event()

    # --------------------------------------------------------------- targets
    def target_accounts(self) -> list[str]:
        """
        Accounts that are BOTH referenced by an enabled profile AND have
        credentials. A profile pointing at an unconfigured account is a warning,
        not a crash: the other account should still run every morning.
        """
        configured = {account.key for account in self.settings.configured_accounts()}
        targets: list[str] = []
        for key in self.config.accounts_in_use():
            if key in configured:
                targets.append(key)
            else:
                log.warning("schedule.account_not_configured", account=key)
        return targets

    # ------------------------------------------------------------------ tick
    async def _run_account(self, account: str, mode: str) -> None:
        if self._stopping.is_set():
            return
        async with self._lock:
            pool = await get_pool()
            async with account_run_lock(pool, account) as acquired:
                if not acquired:
                    log.warning("schedule.tick_skipped_locked", account=account, mode=mode)
                    return
                clear_context()
                log.info("schedule.tick", account=account, mode=mode)
                try:
                    orchestrator = Orchestrator(
                        self.config,
                        self.settings,
                        mode=mode,
                        only_profiles=self.only_profiles,
                        dry_run=self.dry_run,
                        account=account,
                    )
                    stats = await orchestrator.run()
                    log.info(
                        "schedule.tick_done",
                        account=account,
                        applied=stats.applied,
                        failed=stats.failed,
                    )
                except ConfigError as exc:
                    log.error("schedule.tick_misconfigured", account=account, error=str(exc))
                except Exception:
                    # The orchestrator already notified and recorded the run row.
                    log.exception("schedule.tick_crashed", account=account)
                finally:
                    clear_context()

    # ----------------------------------------------------------------- start
    async def start(self) -> None:
        schedule = self.config.schedule
        accounts = self.target_accounts()
        if not accounts:
            raise ConfigError(
                "No account is both enabled in config.yaml and configured in .env — "
                "nothing to schedule."
            )

        for account in accounts:
            cron = schedule.cron_for(account)
            try:
                trigger = CronTrigger.from_crontab(cron, timezone=schedule.timezone)
            except ValueError as exc:
                raise ConfigError(f"schedule cron for '{account}' is invalid ({cron!r}): {exc}") from exc
            trigger.jitter = schedule.jitter_seconds or None
            self.scheduler.add_job(
                self._run_account,
                trigger=trigger,
                args=[account, "scheduled"],
                id=f"apply:{account}",
                name=f"naukri apply ({account})",
                max_instances=1,
                coalesce=True,
                # A run that misfires by less than an hour (host asleep, previous
                # run overran) should still happen; older than that, skip it.
                misfire_grace_time=3_600,
                replace_existing=True,
            )
            log.info(
                "schedule.registered",
                account=account,
                cron=cron,
                timezone=schedule.timezone,
                jitter_s=schedule.jitter_seconds,
            )

        self._install_signal_handlers()
        self.scheduler.start()

        # Start background Telegram HITL Listener if credentials are configured
        listener_task: asyncio.Task[None] | None = None
        if self.settings.telegram_bot_token and self.settings.telegram_chat_id:
            try:
                from .db.repository import Repository
                from .notify.telegram_listener import TelegramListener
                repo = await Repository.create()
                listener = TelegramListener(self.settings.telegram_bot_token, self.settings.telegram_chat_id)
                listener_task = asyncio.create_task(listener.start_listening_loop(repo))
            except Exception as exc:
                log.warning("schedule.telegram_listener_failed", error=str(exc)[:150])

        for job in self.scheduler.get_jobs():
            log.info("schedule.next_fire", job=job.id, at=str(job.next_run_time))

        if schedule.run_on_start:
            log.info("schedule.run_on_start")
            for account in accounts:
                await self._run_account(account, "startup")

        try:
            await self._stopping.wait()
        finally:
            if listener_task and not listener_task.done():
                listener_task.cancel()
            await self.shutdown()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError, ValueError):
                loop.add_signal_handler(sig, self._request_stop, sig.name)

    def _request_stop(self, signal_name: str) -> None:
        log.info("schedule.stop_requested", signal=signal_name)
        self._stopping.set()

    async def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        # Wait for an in-flight run to finish so the browser and the `runs` row
        # are closed properly, but do not hang a `docker stop` forever.
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._lock.acquire(), timeout=180)
            self._lock.release()
        await close_pool()
        log.info("schedule.stopped")


async def run_scheduler(
    config: AgentConfig,
    settings: Settings,
    *,
    only_profiles: list[str] | None = None,
    dry_run: bool = False,
) -> None:
    await AgentScheduler(
        config, settings, only_profiles=only_profiles, dry_run=dry_run
    ).start()
