"""
Command line interface — the only entrypoint (`python -m naukri_agent ...`).

Design decisions
----------------

- **One-shot commands exit with meaningful codes** so cron, Kubernetes and CI can
  alert on them: `0` success, `1` the run failed, `2` misconfiguration. A
  scheduler that cannot tell "nothing to apply to" from "wrong password" is
  useless.
- **Every command sets up logging and directories first**, then loads config, then
  touches the network. Configuration errors therefore surface before a browser is
  launched.
- **`login` is a separate, interactive command.** A first login (or one after an
  OTP challenge) needs a visible browser and a human, so it forces
  `headless=False` and then persists the storage state to Postgres — after which
  the headless container reuses that session and never sees the login form.
- **`run --all-accounts` is sequential, never parallel.** See scheduler.py: two
  simultaneous logins from one IP is what gets an account flagged.
- **Read-only inspection commands** (`accounts`, `status`, `reviews`) exist so the
  operator never needs a psql prompt to answer "did it work this morning?".
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .browser.artifacts import ArtifactStore
from .browser.manager import BrowserManager
from .config import ACCOUNT_KEYS, AgentConfig, ConfigError, Settings, get_settings, load_config
from .core.orchestrator import Orchestrator
from .core.run_policy import RunPolicy
from .db.migrations import run_migrations
from .db.pool import close_pool, get_pool
from .db.repository import Repository
from .logging_setup import get_logger, setup_logging
from .naukri.auth import NaukriAuth
from .naukri.profile import ProfileRefresher

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Naukri.com auto-apply agent (Playwright + PostgreSQL).",
)
console = Console()
log = get_logger(__name__)

EXIT_OK = 0
EXIT_RUN_FAILED = 1
EXIT_CONFIG = 2


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _bootstrap(config_path: Path | None = None) -> tuple[Settings, AgentConfig]:
    """Settings + logging + directories + validated YAML, in that order."""
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_json, settings.log_dir)
    settings.ensure_dirs()
    return settings, load_config(config_path)


def _settings_only() -> Settings:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_json, settings.log_dir)
    return settings


def _run(coro) -> None:
    """Run an async command, mapping failures onto process exit codes."""
    try:
        asyncio.run(coro)
    except ConfigError as exc:
        console.print(f"[bold red]Configuration error:[/bold red] {exc}")
        raise typer.Exit(EXIT_CONFIG) from exc
    except KeyboardInterrupt:  # pragma: no cover
        console.print("[yellow]interrupted[/yellow]")
        raise typer.Exit(130) from None


def _resolve_accounts(account: str | None, all_accounts: bool, settings: Settings) -> list[str]:
    if all_accounts:
        keys = [item.key for item in settings.configured_accounts()]
        if not keys:
            raise ConfigError("No Naukri account is configured — fill NAUKRI_EMAIL/PASSWORD in .env")
        return keys
    return [(account or settings.default_account).strip().lower()]


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------
@app.command()
def migrate(
    config_path: Path | None = typer.Option(None, "--config", help="Path to config.yaml"),
) -> None:
    """Create or upgrade the database schema (idempotent, safe to re-run)."""

    async def _main() -> None:
        _settings_only()
        try:
            applied = await run_migrations()
        finally:
            await close_pool()
        if applied:
            console.print(f"[green]applied migrations:[/green] {', '.join(applied)}")
        else:
            console.print("[green]schema already up to date[/green]")

    _ = config_path  # accepted for symmetry with the other commands
    _run(_main())


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
@app.command()
def run(
    account: str | None = typer.Option(
        None, "--account", "-a", help=f"Which login to use: {' | '.join(ACCOUNT_KEYS)}"
    ),
    all_accounts: bool = typer.Option(
        False, "--all-accounts", help="Run every configured account, one after another"
    ),
    profile: list[str] | None = typer.Option(
        None, "--profile", "-p", help="Only these profile names (repeatable)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Do everything except submitting an application"
    ),
    headed: bool | None = typer.Option(
        None, "--headed/--headless", help="Override headless mode in browser"
    ),
    config_path: Path | None = typer.Option(None, "--config", help="Path to config.yaml"),
) -> None:
    """Apply to jobs once and exit."""

    async def _main() -> None:
        settings, config = _bootstrap(config_path)
        if headed is not None:
            settings.headless = not headed
            config.browser.headless = not headed
        await run_migrations()
        targets = _resolve_accounts(account, all_accounts, settings)
        failures = 0
        try:
            for key in targets:
                # Fail fast per account, but keep going: account 2 being
                # unconfigured must not cancel account 1's run.
                try:
                    settings.validate_for_run(key)
                except ConfigError as exc:
                    console.print(f"[yellow]skipping {key}:[/yellow] {exc}")
                    failures += 1
                    continue

                orchestrator = Orchestrator(
                    config,
                    settings,
                    mode="manual",
                    only_profiles=list(profile) if profile else None,
                    dry_run=dry_run,
                    account=key,
                )
                stats = await orchestrator.run()
                _print_stats(key, stats)
                if stats.errors and stats.applied == 0:
                    failures += 1
        finally:
            await close_pool()

        if failures and failures == len(targets):
            raise typer.Exit(EXIT_RUN_FAILED)

    _run(_main())


def _print_stats(account: str, stats) -> None:
    table = Table(title=f"run summary — {account}", show_header=True, header_style="bold")
    table.add_column("metric")
    table.add_column("value", justify="right")
    for key in (
        "scraped",
        "considered",
        "filtered_out",
        "applied",
        "already_applied",
        "external",
        "needs_review",
        "failed",
    ):
        table.add_row(key.replace("_", " "), str(getattr(stats, key, 0)))
    console.print(table)
    for message in stats.errors[:5]:
        console.print(f"  [red]![/red] {message}")


# ---------------------------------------------------------------------------
# schedule
# ---------------------------------------------------------------------------
@app.command()
def schedule(
    profile: list[str] | None = typer.Option(None, "--profile", "-p", help="Only these profiles"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Never submit an application"),
    config_path: Path | None = typer.Option(None, "--config", help="Path to config.yaml"),
) -> None:
    """Stay running and apply on the configured cron (one job per account)."""

    async def _main() -> None:
        settings, config = _bootstrap(config_path)
        if not config.schedule.enabled:
            raise ConfigError("schedule.enabled is false in config.yaml")
        # Import here so `migrate`/`status` do not pay for APScheduler.
        from .scheduler import run_scheduler

        # The schema must exist before the first tick, and the scheduler is the
        # container's default command — so migrating here makes a fresh deploy
        # work without a manual step.
        await run_migrations()
        try:
            await run_scheduler(
                config,
                settings,
                only_profiles=list(profile) if profile else None,
                dry_run=dry_run,
            )
        finally:
            await close_pool()

    _run(_main())


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------
@app.command()
def login(
    account: str | None = typer.Option(None, "--account", "-a", help="Which login to use"),
    platform: str = typer.Option("naukri", "--platform", "-p", help="Platform to log in: naukri, cutshort, wellfound, linkedin, instahyre, or all"),
    headed: bool = typer.Option(True, "--headed/--headless", help="Show the browser window"),
    config_path: Path | None = typer.Option(None, "--config", help="Path to config.yaml"),
) -> None:
    """
    Log in interactively (headed browser) and store the session in Postgres.
    
    Supports: naukri, cutshort, wellfound, linkedin, instahyre, or all.
    Every subsequent headless CI run reuses the saved session.
    """

    async def _main() -> None:
        settings, config = _bootstrap(config_path)
        target = settings.validate_for_run(account)
        await run_migrations()
        repo = await Repository.create()
        browser_config = config.browser.model_copy(update={"headless": not headed})
        artifacts = ArtifactStore(settings.artifacts_dir, "login")
        from .browser.resilience import first_visible, human_pause, human_type

        selected_platforms = (
            ["naukri", "cutshort", "wellfound", "linkedin", "instahyre"]
            if platform.lower() == "all"
            else [platform.lower()]
        )

        try:
            async with BrowserManager(
                browser_config, repo, session_key=target.session_key
            ) as browser:
                page = await browser.new_page()

                for p in selected_platforms:
                    console.print(f"\n[bold cyan]=== Logging in to {p.upper()} (Account: {target.key}) ===[/bold cyan]")

                    if p == "naukri":
                        auth = NaukriAuth(browser, target.email, target.password, artifacts)
                        naukri_page = await auth.ensure_logged_in()
                        await browser.persist_session()
                        await repo.resume_platform(target.key, "naukri")
                        console.print(f"[green]✓ Logged in to Naukri as {target.masked_email}[/green]")
                        await naukri_page.close()

                    elif p == "cutshort":
                        await page.goto("https://cutshort.io/profile/jobs", wait_until="domcontentloaded")
                        await human_pause(2000, 3000)

                        auth_sel = [
                            "div[data-intercom-target='candidateProfileNav']",
                            "a[href*='/profile']",
                            "div.user-avatar",
                            "button:has-text('Logout')",
                            "div:has-text('Matches')",
                            "div:has-text('Find jobs')",
                        ]
                        if await first_visible(page, auth_sel, timeout_ms=3000):
                            console.print("[green]✓ Cutshort session already active![/green]")
                        else:
                            console.print("[yellow]Please log in to Cutshort (Google SSO, OTP, or email) in the open browser...[/yellow]")
                            console.print("[dim]Waiting up to 120 seconds for login confirmation...[/dim]")
                            try:
                                await page.wait_for_selector(", ".join(auth_sel), timeout=120000)
                                console.print("[green]✓ Cutshort login detected![/green]")
                            except Exception:
                                console.print("[red]✗ Cutshort login timed out (120s).[/red]")
                                continue

                        await browser.persist_session()
                        await repo.resume_platform(target.key, "cutshort")
                        console.print("[green]✓ Cutshort session persisted to Postgres and resumed![/green]")

                    elif p == "wellfound":
                        await page.goto("https://wellfound.com/jobs", wait_until="domcontentloaded")
                        await human_pause(2000, 3000)

                        auth_sel = [
                            "button[aria-label='User Menu']",
                            "a[href*='/profile']",
                            "text=Applied",
                            "div[data-test='JobCard']",
                            "button:has-text('Discover')",
                        ]
                        if await first_visible(page, auth_sel, timeout_ms=3000):
                            console.print("[green]✓ Wellfound session already active![/green]")
                        else:
                            console.print("[yellow]Please solve Cloudflare Turnstile / log in to Wellfound in the open browser...[/yellow]")
                            console.print("[dim]Waiting up to 120 seconds for login confirmation...[/dim]")
                            try:
                                await page.wait_for_selector(", ".join(auth_sel), timeout=120000)
                                console.print("[green]✓ Wellfound login detected![/green]")
                            except Exception:
                                console.print("[red]✗ Wellfound login timed out (120s).[/red]")
                                continue

                        await browser.persist_session()
                        await repo.resume_platform(target.key, "wellfound")
                        console.print("[green]✓ Wellfound session persisted to Postgres and resumed![/green]")

                    elif p == "linkedin":
                        await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
                        await human_pause(2000, 3000)

                        auth_sel = [
                            "nav.global-nav",
                            "img.global-nav__me-photo",
                            "a[href*='/in/']",
                            ".feed-identity-module",
                            "button[aria-label*='Account']",
                        ]
                        if await first_visible(page, auth_sel, timeout_ms=3000):
                            console.print("[green]✓ LinkedIn session already active![/green]")
                        else:
                            console.print("[yellow]Please log in to LinkedIn / complete security checkpoint in the open browser...[/yellow]")
                            console.print("[dim]Waiting up to 120 seconds for login confirmation...[/dim]")
                            try:
                                await page.wait_for_selector(", ".join(auth_sel), timeout=120000)
                                console.print("[green]✓ LinkedIn login detected![/green]")
                            except Exception:
                                console.print("[red]✗ LinkedIn login timed out (120s).[/red]")
                                continue

                        await browser.persist_session()
                        await repo.resume_platform(target.key, "linkedin")
                        console.print("[green]✓ LinkedIn session persisted to Postgres and resumed![/green]")

                    elif p == "instahyre":
                        await page.goto("https://www.instahyre.com/candidate/opportunities/?matching=true", wait_until="domcontentloaded")
                        await human_pause(2000, 3000)

                        auth_sel = [
                            "a[href*='/candidate/profile']",
                            "a[href*='/candidate/opportunities']",
                            "div.employer-row",
                            "button:has-text('Logout')",
                            "#opportunities",
                        ]
                        if await first_visible(page, auth_sel, timeout_ms=3000):
                            console.print("[green]✓ Instahyre session already active![/green]")
                        else:
                            console.print("[yellow]Please log in to Instahyre in the open browser...[/yellow]")
                            email_inp = await first_visible(page, ["input[type='email']", "input[name='email']"], timeout_ms=2000)
                            pass_inp = await first_visible(page, ["input[type='password']", "input[name='password']"], timeout_ms=2000)
                            i_email = (settings.instahyre_email or target.email).strip()
                            i_pass = (settings.instahyre_password or target.password).strip()
                            if email_inp and pass_inp and i_email and i_pass:
                                await human_type(email_inp, i_email)
                                await human_type(pass_inp, i_pass)
                                submit = await first_visible(page, ["button:has-text('Login')", "button[type='submit']"])
                                if submit:
                                    await submit.click()

                            try:
                                await page.wait_for_selector(", ".join(auth_sel), timeout=120000)
                                console.print("[green]✓ Instahyre login detected![/green]")
                            except Exception:
                                console.print("[red]✗ Instahyre login timed out (120s).[/red]")
                                continue

                        await browser.persist_session()
                        await repo.resume_platform(target.key, "instahyre")
                        console.print("[green]✓ Instahyre session persisted to Postgres and resumed![/green]")

                await browser.persist_session()
                console.print(f"\n[bold green]✓ All selected platform sessions saved under {target.session_key}![/bold green]")
                if not page.is_closed():
                    await page.close()
        finally:
            await close_pool()

    _run(_main())


# ---------------------------------------------------------------------------
# linkedin login & campaign
# ---------------------------------------------------------------------------
@app.command(name="login-linkedin")
def login_linkedin(
    timeout: int = typer.Option(180, "--timeout", "-t", help="Seconds to wait for manual login"),
) -> None:
    """
    Log in to LinkedIn manually (headed browser) and save the session to the
    persistent profile used by the cold-email campaign.
    """

    async def _main() -> None:
        settings = _settings_only()
        from .linkedin.scraper import LinkedInHunter

        console.print("[cyan]Opening LinkedIn in a browser — please log in manually.[/cyan]")
        hunter = LinkedInHunter(li_at_cookie=settings.linkedin_li_at, headless=False)
        ok = await hunter.login_interactive(timeout_s=timeout)
        if ok:
            console.print(
                "[green]✓ LinkedIn session saved to the persistent browser profile![/green] "
                "The headless campaign will reuse this session."
            )
        else:
            console.print("[red]✗ LinkedIn login timed out or failed[/red]")
            raise typer.Exit(EXIT_RUN_FAILED)

    _run(_main())


@app.command(name="campaign-linkedin")
def campaign_linkedin_cmd(
    limit: int = typer.Option(15, "--limit", "-l", help="Daily email limit"),
    headed: bool = typer.Option(False, "--headed/--headless", help="Show browser window"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Dry run: hunt and preview matches without sending emails"),
    config_path: Path | None = typer.Option(None, "--config", help="Path to config.yaml"),
) -> None:
    """
    Run the LinkedIn Cold Email Outreach Campaign.
    """

    async def _main() -> None:
        settings = _settings_only()
        if not settings.matched_outreach_enabled:
            raise ConfigError(
                "LinkedIn outreach is disabled; use matched Naukri outreach after validation"
            )
        await run_migrations()
        from .linkedin.campaign import run_campaign
        effective_dry_run = dry_run or not settings.side_effects_enabled
        sent = await run_campaign(
            daily_email_limit=limit,
            headed=headed,
            dry_run=effective_dry_run,
        )
        action_word = "previewed" if effective_dry_run else "sent"
        console.print(f"[green]✓ LinkedIn campaign completed: {sent} emails {action_word}[/green]")
        await close_pool()

    _run(_main())




# ---------------------------------------------------------------------------
# profile refresh
# ---------------------------------------------------------------------------
@app.command(name="refresh-profile")
def refresh_profile(
    account: str | None = typer.Option(None, "--account", "-a", help="Which login to use"),
    force: bool = typer.Option(False, "--force", help="Ignore the min_hours_between guard"),
    headed: bool = typer.Option(False, "--headed/--headless", help="Show the browser window"),
    config_path: Path | None = typer.Option(None, "--config", help="Path to config.yaml"),
) -> None:
    """
    Touch the Naukri profile so recruiter search ranks it as updated today.

    The apply run already does this; use the command to verify it works or to
    refresh on a day you are not applying.
    """

    async def _main() -> None:
        settings, config = _bootstrap(config_path)
        target = settings.validate_for_run(account)
        await run_migrations()
        repo = await Repository.create()
        refresh_config = config.profile_refresh
        try:
            if not force and not await repo.profile_refresh_due(
                target.key, refresh_config.min_hours_between
            ):
                last = await repo.last_profile_refresh(target.key)
                hours = round(float(last["hours_ago"] or 0), 1) if last else 0
                console.print(
                    f"[yellow]skipped:[/yellow] already refreshed {hours}h ago "
                    f"(min_hours_between={refresh_config.min_hours_between}). Use --force."
                )
                return

            artifacts = ArtifactStore(settings.artifacts_dir, "refresh")
            browser_config = config.browser.model_copy(update={"headless": not headed})
            async with BrowserManager(
                browser_config, repo, session_key=target.session_key
            ) as browser:
                auth = NaukriAuth(browser, target.email, target.password, artifacts)
                page = await auth.ensure_logged_in()
                refresher = ProfileRefresher(
                    page,
                    target.key,
                    RunPolicy(
                        dry_run=settings.dry_run,
                        side_effects_enabled=settings.side_effects_enabled,
                    ),
                    strategies=refresh_config.strategies,
                    headline_variants=refresh_config.headline_variants,
                    resume_dir=settings.resume_dir,
                    resume_file=config.resume_for(target.key),
                    verify=refresh_config.verify,
                )
                result = await refresher.refresh()
                await repo.record_profile_refresh(
                    target.key,
                    None,
                    ok=result.ok,
                    strategy=result.strategy,
                    detail=result.detail,
                    headline_before=result.before,
                    headline_after=result.after,
                    last_updated_text=result.last_updated,
                )
            if result.ok:
                console.print(f"[green]profile refreshed[/green] via {result.strategy}")
            else:
                console.print(f"[red]refresh failed:[/red] {result.detail}")
                raise typer.Exit(EXIT_RUN_FAILED)
        finally:
            await close_pool()

    _run(_main())


# ---------------------------------------------------------------------------
# inspection
# ---------------------------------------------------------------------------
@app.command()
def accounts(
    config_path: Path | None = typer.Option(None, "--config", help="Path to config.yaml"),
) -> None:
    """Show which logins are wired up, their profiles and today's usage."""

    async def _main() -> None:
        settings, config = _bootstrap(config_path)
        table = Table(title="accounts", header_style="bold")
        for column in ("account", "email", "credentials", "profiles", "cron", "applied today"):
            table.add_column(column)

        counts: dict[str, int] = {}
        if settings.database_url:
            try:
                repo = await Repository.create()
                counts = {
                    str(row["account"]): int(row["applied"] or 0)
                    for row in await repo.applied_today_by_account()
                }
            except Exception as exc:  # DB down must not hide the config view
                console.print(f"[yellow]could not read usage:[/yellow] {str(exc)[:120]}")

        for key in ACCOUNT_KEYS:
            account_cfg = settings.account(key)
            profiles = [p.name for p in config.active_profiles(account=key)]
            wired = bool(account_cfg.email and account_cfg.password)
            table.add_row(
                key,
                account_cfg.masked_email if wired else "-",
                "[green]set[/green]" if wired else "[red]missing[/red]",
                ", ".join(profiles) or "-",
                config.schedule.cron_for(key),
                str(counts.get(key, 0)),
            )
        console.print(table)
        await close_pool()

    _run(_main())


@app.command()
def status(
    limit: int = typer.Option(20, "--limit", "-n", min=1, max=200),
) -> None:
    """Recent applications, today's per-account totals and profile freshness."""

    async def _main() -> None:
        _settings_only()
        repo = await Repository.create()
        try:
            today = await repo.applied_today_by_account()
            table = Table(title="today", header_style="bold")
            for column in ("account", "applied", "failed", "needs review"):
                table.add_column(column, justify="right")
            for row in today:
                table.add_row(
                    str(row["account"]),
                    str(row["applied"]),
                    str(row["failed"]),
                    str(row["needs_review"]),
                )
            console.print(table if today else "[yellow]no applications today[/yellow]")

            freshness = await repo.profile_freshness()
            if freshness:
                fresh_table = Table(title="profile freshness", header_style="bold")
                for column in ("account", "last success", "last attempt", "ok", "failed"):
                    fresh_table.add_column(column)
                for row in freshness:
                    fresh_table.add_row(
                        str(row["account"]),
                        str(row["last_success_at"] or "-"),
                        str(row["last_attempt_at"] or "-"),
                        str(row["successes"]),
                        str(row["failures"]),
                    )
                console.print(fresh_table)

            recent = await repo.recent_applications(limit)
            recent_table = Table(title=f"last {len(recent)} decisions", header_style="bold")
            for column in ("when", "account", "status", "reason", "title", "company"):
                recent_table.add_column(column, overflow="fold")
            for row in recent:
                recent_table.add_row(
                    row["created_at"].strftime("%d %b %H:%M"),
                    str(row["account"]),
                    str(row["status"]),
                    str(row["reason"] or "-"),
                    str(row["title"])[:48],
                    str(row["company"])[:28],
                )
            console.print(recent_table)
        finally:
            await close_pool()

    _run(_main())


@app.command()
def reviews(
    limit: int = typer.Option(30, "--limit", "-n", min=1, max=200),
) -> None:
    """Screening questions the agent could not answer, most frequent first."""

    async def _main() -> None:
        _settings_only()
        repo = await Repository.create()
        try:
            pending = await repo.pending_reviews(limit)
            if not pending:
                console.print("[green]no questions waiting for review[/green]")
                return
            table = Table(title="unanswered screening questions", header_style="bold")
            for column in ("id", "profile", "seen", "kind", "question", "options"):
                table.add_column(column, overflow="fold")
            for row in pending:
                options = row["options"]
                table.add_row(
                    str(row["id"]),
                    str(row["profile"])[:24],
                    str(row["times_seen"]),
                    str(row["question_kind"]),
                    str(row["question"])[:90],
                    str(options)[:60],
                )
            console.print(table)
            console.print(
                'Answer one with: [bold]python -m naukri_agent resolve <id> "<answer>"[/bold]'
            )
        finally:
            await close_pool()

    _run(_main())


@app.command()
def resolve(
    review_id: int = typer.Argument(..., help="id from the `reviews` table"),
    answer: str = typer.Argument(..., help="the answer to submit for this question"),
) -> None:
    """
    Answer a queued screening question.

    The answer is promoted into the knowledge base at high priority, and the job
    it blocked becomes eligible again on the next run.
    """

    async def _main() -> None:
        _settings_only()
        repo = await Repository.create()
        try:
            if await repo.resolve_review(review_id, answer):
                console.print(f"[green]resolved review {review_id}[/green]")
            else:
                console.print(f"[red]no pending review with id {review_id}[/red]")
                raise typer.Exit(EXIT_RUN_FAILED)
        finally:
            await close_pool()

    _run(_main())


@app.command(name="listen-telegram")
def listen_telegram(
    config_path: Path | None = typer.Option(None, "--config", help="Path to config.yaml"),
) -> None:
    """Start interactive Telegram bot listener to train your AI directly from your phone."""

    async def _main() -> None:
        settings, config = _bootstrap(config_path)
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            raise ConfigError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be configured in .env")

        from .notify.telegram_listener import TelegramListener
        repo = await Repository.create()
        listener = TelegramListener(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
        )
        try:
            await listener.start_listening_loop(repo)
        finally:
            await close_pool()

    _run(_main())


@app.command()
def doctor(
    config_path: Path | None = typer.Option(None, "--config", help="Path to config.yaml"),
) -> None:
    """Validate environment, config and database connectivity without touching Naukri."""

    async def _main() -> None:
        settings, config = _bootstrap(config_path)
        table = Table(title="preflight", header_style="bold")
        table.add_column("check")
        table.add_column("result", overflow="fold")

        table.add_row("config file", str(settings.config_path))
        enabled_platforms = [k for k, v in config.platforms.model_dump().items() if v]
        table.add_row("platforms enabled", ", ".join(enabled_platforms) or "[yellow]none[/yellow]")
        table.add_row(
            "profiles",
            ", ".join(f"{p.name} [{p.account}]" for p in config.active_profiles()) or "none enabled",
        )
        wired = [item.key for item in settings.configured_accounts()]
        table.add_row("accounts configured", ", ".join(wired) or "[red]none[/red]")
        table.add_row("resume dir", f"{settings.resume_dir} (exists={settings.resume_dir.exists()})")
        table.add_row("artifacts dir", str(settings.artifacts_dir))
        table.add_row("dry run", str(settings.dry_run))
        table.add_row("headless", str(config.browser.headless))


        if settings.database_url:
            try:
                pool = await get_pool()
                version = await pool.fetchval("SELECT version()")
                table.add_row("database", f"[green]reachable[/green] — {str(version)[:40]}")
                applied = await run_migrations()
                table.add_row("schema", f"up to date (applied now: {applied or 'none'})")
            except Exception as exc:
                table.add_row("database", f"[red]{str(exc)[:160]}[/red]")
        else:
            table.add_row("database", "[red]DATABASE_URL not set[/red]")

        console.print(table)

        missing = [key for key in config.accounts_in_use() if key not in wired]
        if missing:
            console.print(
                f"[yellow]warning:[/yellow] profiles reference unconfigured accounts: {missing}"
            )
        await close_pool()

    _run(_main())


@app.command()
def version() -> None:
    """Print the agent version."""
    from . import __version__

    console.print(f"naukri-agent {__version__}")


@app.command()
def stats(
    days: int = typer.Option(7, "--days", "-d", help="Look-back window in days (default: 7)."),
    weekly: bool = typer.Option(False, "--weekly", "-w", help="Show weekly aggregation instead of daily."),
    profile: str | None = typer.Option(None, "--profile", "-p", help="Filter to a specific profile."),
) -> None:
    """
    Show an analytics dashboard of applications and answer KB health.

    \b
    Examples:
      naukri-agent stats                 # 7-day daily breakdown
      naukri-agent stats --days 30       # 30-day view
      naukri-agent stats --weekly        # weekly roll-up
    """
    setup_logging("INFO", json_logs=False)
    _run(_stats(days, weekly, profile))


async def _stats(days: int, weekly: bool, profile: str | None) -> None:
    try:
        _ = get_settings()
    except ConfigError as exc:
        console.print(f"[red]Config error:[/red] {exc}")
        raise typer.Exit(2) from exc

    try:
        pool = await get_pool()
        await run_migrations()
        repo = Repository(pool)

        # ── Headline metrics ────────────────────────────────────────────────
        sr = await repo.success_rate(days=days)
        console.print()
        console.rule(f"[bold cyan]📊  Naukri Agent — {days}-Day Stats[/bold cyan]")
        console.print()

        headline = Table.grid(padding=(0, 3))
        headline.add_row(
            f"[bold green]{sr.get('applied', 0)}[/bold green] applied",
            f"[bold yellow]{sr.get('needs_review', 0)}[/bold yellow] needs review",
            f"[bold red]{sr.get('failed', 0)}[/bold red] failed",
            f"[bold blue]{sr.get('skipped', 0)}[/bold blue] skipped",
            f"[bold magenta]{sr.get('success_rate_pct', 0):.1f}%[/bold magenta] success rate",
        )
        skipped_by_score = sr.get("skipped_by_score", 0)
        if skipped_by_score:
            headline.add_row(
                f"  └─ [dim]{skipped_by_score} skipped by match score pre-filter (saved time)[/dim]",
                "", "", "", "",
            )
        console.print(headline)
        console.print()

        # ── Daily (or weekly) breakdown ─────────────────────────────────────
        if weekly:
            # Pull from the DB view directly
            rows = await pool.fetch(
                f"""
                SELECT week_start, account,
                       sum(applied) AS applied, sum(failed) AS failed,
                       sum(skipped) AS skipped, sum(needs_review) AS needs_review
                  FROM weekly_application_counts
                 WHERE week_start >= (now() - interval '{days} days')::date
                   {'AND account = $1' if profile else ''}
                 GROUP BY week_start, account
                 ORDER BY week_start DESC, account
                """,
                *([profile] if profile else []),
            )
            col_label = "Week"
        else:
            rows = await repo.daily_stats(days=days)
            if profile:
                rows = [r for r in rows if r.get("account", "").lower() == profile.lower()]
            col_label = "Day"

        if rows:
            tbl = Table(
                title=f"{'Weekly' if weekly else 'Daily'} Breakdown",
                show_lines=False,
                header_style="bold",
            )
            tbl.add_column(col_label, style="dim")
            tbl.add_column("Account")
            tbl.add_column("Applied", justify="right", style="green")
            tbl.add_column("Failed", justify="right", style="red")
            tbl.add_column("Skipped", justify="right", style="blue")
            tbl.add_column("Needs Review", justify="right", style="yellow")

            for r in rows:
                tbl.add_row(
                    str(r.get("day") or r.get("week_start", "")),
                    r.get("account", ""),
                    str(r.get("applied", 0)),
                    str(r.get("failed", 0)),
                    str(r.get("skipped", 0)),
                    str(r.get("needs_review", 0)),
                )
            console.print(tbl)
        else:
            console.print("[dim]No application data in this window.[/dim]")

        console.print()

        # ── Answer KB coverage ──────────────────────────────────────────────
        cov = await repo.answer_coverage()
        cov_tbl = Table(title="Answer KB Coverage", show_header=False, box=None)
        cov_tbl.add_column("Metric", style="bold")
        cov_tbl.add_column("Value")
        cov_tbl.add_row("Questions seen", str(cov.get("total_questions", 0)))
        cov_tbl.add_row(
            "Resolved",
            f"[green]{cov.get('resolved', 0)}[/green]  "
            f"({cov.get('coverage_pct', 0):.1f}% coverage)",
        )
        cov_tbl.add_row(
            "Auto-resolved by fuzzy matching",
            f"[cyan]{cov.get('auto_resolved', 0)}[/cyan]",
        )
        cov_tbl.add_row("Pending (action required)", f"[yellow]{cov.get('pending', 0)}[/yellow]")
        cov_tbl.add_row("KB entries total", str(cov.get("kb_entries", 0)))
        cov_tbl.add_row(
            "Zero-hit entries (dead weight)",
            f"[dim]{cov.get('zero_hit_kb_entries', 0)}[/dim]  "
            f"(run [bold]learn --unused[/bold] to list them)",
        )
        console.print(cov_tbl)
        console.print()

        # ── Top unanswered questions ─────────────────────────────────────────
        unanswered = await repo.top_unanswered(limit=8)
        if unanswered:
            ua_tbl = Table(
                title="⚠  Top Unanswered Questions (run 'resolve <id> \"answer\"')",
                show_lines=True,
                header_style="bold yellow",
            )
            ua_tbl.add_column("ID", justify="right", style="dim", width=5)
            ua_tbl.add_column("Profile")
            ua_tbl.add_column("Seen", justify="right")
            ua_tbl.add_column("Question")
            ua_tbl.add_column("Kind", style="dim")
            for row in unanswered:
                ua_tbl.add_row(
                    str(row.get("id", "")),
                    row.get("profile", ""),
                    str(row.get("times_seen", 1)),
                    (row.get("question") or "")[:80],
                    row.get("question_kind", ""),
                )
            console.print(ua_tbl)
        else:
            console.print("[green]✓  No unanswered questions! KB coverage is complete.[/green]")

        console.print()
        await close_pool()

    except Exception as exc:
        console.print(f"[red]stats failed:[/red] {exc}")
        raise typer.Exit(1) from exc


@app.command()
def learn(
    unused: bool = typer.Option(False, "--unused", help="Show KB entries with zero hits (dead weight)."),
    fuzzy: bool = typer.Option(False, "--fuzzy", help="Show auto-resolved answers for human verification."),
    profile: str | None = typer.Option(None, "--profile", "-p", help="Filter to a specific profile."),
    limit: int = typer.Option(30, "--limit", "-n", help="Max rows to display."),
) -> None:
    """
    Inspect the self-learning answer KB: hit rates, auto-resolved answers, dead weight.

    \b
    Examples:
      naukri-agent learn                 # All KB entries by hit count
      naukri-agent learn --unused        # Entries with 0 hits (safe to remove)
      naukri-agent learn --fuzzy         # Auto-resolved answers to verify
    """
    setup_logging("INFO", json_logs=False)
    _run(_learn(unused, fuzzy, profile, limit))


async def _learn(
    unused: bool, fuzzy: bool, profile: str | None, limit: int
) -> None:
    try:
        await get_pool()
        await run_migrations()
        repo = Repository(await get_pool())

        # ── Coverage summary ─────────────────────────────────────────────────
        cov = await repo.answer_coverage()
        console.print()
        console.rule("[bold cyan]🧠  Answer Knowledge Base — Health Report[/bold cyan]")
        console.print(
            f"\n  [green]{cov['resolved']}[/green] resolved  "
            f"[yellow]{cov['pending']}[/yellow] pending  "
            f"[cyan]{cov['auto_resolved']}[/cyan] auto-resolved by fuzzy  "
            f"[bold]{cov['coverage_pct']:.1f}%[/bold] coverage\n"
        )

        # ── KB hit report ────────────────────────────────────────────────────
        entries = await repo.answer_hit_report(profile=profile, limit=limit)

        if unused:
            entries = [e for e in entries if (e.get("hits") or 0) == 0]
            title = "🗑  Zero-Hit KB Entries (safe to remove from config.yaml)"
        elif fuzzy:
            # Show auto-resolved entries from question_review
            rows = await (await get_pool()).fetch(
                """
                SELECT id, profile, question, answer, resolved_by, times_seen, updated_at
                  FROM question_review
                 WHERE auto_resolved = TRUE
                   AND ($1::text IS NULL OR profile = $1)
                 ORDER BY updated_at DESC
                 LIMIT $2
                """,
                profile, limit,
            )
            console.print("[bold]Auto-resolved questions[/bold] (verify these answers are correct):\n")
            if rows:
                tbl = Table(show_lines=True, header_style="bold cyan")
                tbl.add_column("ID", width=5)
                tbl.add_column("Profile")
                tbl.add_column("Method", style="dim")
                tbl.add_column("Seen", justify="right")
                tbl.add_column("Question")
                tbl.add_column("Answer", style="green")
                for r in rows:
                    tbl.add_row(
                        str(r["id"]),
                        r.get("profile", ""),
                        r.get("resolved_by", ""),
                        str(r.get("times_seen", 1)),
                        (r.get("question") or "")[:70],
                        (r.get("answer") or "")[:30],
                    )
                console.print(tbl)
                console.print(
                    "\n[dim]To correct a wrong auto-answer: "
                    "naukri-agent resolve <id> \"correct answer\"[/dim]\n"
                )
            else:
                console.print("[dim]No auto-resolved questions yet.[/dim]")
            await close_pool()
            return
        else:
            title = f"📚  KB Entries by Hit Count (top {limit})"

        if entries:
            tbl = Table(title=title, show_lines=False, header_style="bold")
            tbl.add_column("Hits", justify="right", style="bold")
            tbl.add_column("Priority", justify="right", style="dim")
            tbl.add_column("Source", style="dim")
            tbl.add_column("Pattern")
            tbl.add_column("Answer", style="green")

            for e in entries:
                hits = e.get("hits") or 0
                hit_style = "green" if hits > 5 else ("yellow" if hits > 0 else "red dim")
                tbl.add_row(
                    f"[{hit_style}]{hits}[/{hit_style}]",
                    str(e.get("priority", "")),
                    e.get("source", "yaml"),
                    (e.get("pattern") or "")[:60],
                    (e.get("answer") or "")[:35],
                )
            console.print(tbl)

            if unused:
                console.print(
                    "\n[dim]These patterns were never matched. "
                    "Remove them from config.yaml answers: to keep the KB lean.[/dim]\n"
                )
        else:
            console.print("[dim]No matching KB entries.[/dim]")

        await close_pool()

    except Exception as exc:
        console.print(f"[red]learn failed:[/red] {exc}")
        raise typer.Exit(1) from exc



def main() -> None:  # console_scripts / python -m entrypoint
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
