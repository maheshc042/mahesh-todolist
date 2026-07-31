"""
Command line entrypoint.

    python -m naukri_agent migrate            # create/upgrade the schema
    python -m naukri_agent run                # default account, one-shot
    python -m naukri_agent run -a secondary   # the AI / Python account
    python -m naukri_agent run --all-accounts # both accounts, sequentially
    python -m naukri_agent run --dry-run      # scrape + filter, never submit
    python -m naukri_agent run -p "AI / Python Engineer"
    python -m naukri_agent schedule           # long-lived daemon (in-process cron)
    python -m naukri_agent accounts           # which logins/profiles are wired up
    python -m naukri_agent reviews            # list unanswered questions
    python -m naukri_agent resolve <id> "answer"
    python -m naukri_agent stats
    python -m naukri_agent login -a secondary # interactive first login (headed)

Design decision: `run` and `schedule` are separate subcommands so the SAME image
serves both deployment styles — `docker run ... run` for Kubernetes CronJob /
host cron, and `docker compose up` (default CMD `schedule`) for a always-on box.
Exit codes are meaningful (0 ok, 1 partial/failed, 2 config error) so external
schedulers can alert on them.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .config import get_settings, load_config
from .core.models import RunStatus
from .core.orchestrator import Orchestrator
from .db.migrations import run_migrations
from .db.pool import close_pool
from .db.repository import Repository
from .logging_setup import configure_logging, get_logger
from .scheduler import AgentScheduler

log = get_logger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="naukri-agent", description="Naukri AI auto-apply agent"
    )
    parser.add_argument("--config", help="Path to config.yaml (overrides CONFIG_PATH)")
    parser.add_argument("--log-level", default=None, help="DEBUG|INFO|WARNING|ERROR")
    parser.add_argument("--json-logs", action="store_true", help="Emit JSON logs to stdout")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="Create or upgrade the database schema")

    run_cmd = sub.add_parser("run", help="Execute one apply cycle and exit")
    run_cmd.add_argument(
        "-p", "--profile", action="append", dest="profiles",
        help="Only run this profile (repeatable)",
    )
    run_cmd.add_argument(
        "--dry-run", action="store_true",
        help="Scrape, filter and open jobs but never submit an application",
    )
    run_cmd.add_argument("--headed", action="store_true", help="Show the browser window")
    run_cmd.add_argument(
        "-a", "--account", default=None,
        help="Naukri account to run: primary|secondary (default: DEFAULT_ACCOUNT)",
    )
    run_cmd.add_argument(
        "--all-accounts", action="store_true",
        help="Run every configured account sequentially, in separate browser sessions",
    )

    sub.add_parser("schedule", help="Run forever on the configured cron")
    sub.add_parser("stats", help="Print aggregate statistics")
    sub.add_parser("accounts", help="Show which Naukri accounts are configured")

    reviews_cmd = sub.add_parser("reviews", help="List screening questions awaiting an answer")
    reviews_cmd.add_argument("--limit", type=int, default=25)

    resolve_cmd = sub.add_parser("resolve", help="Answer a queued question")
    resolve_cmd.add_argument("review_id", type=int)
    resolve_cmd.add_argument("answer")

    login_cmd = sub.add_parser("login", help="Perform a headed login and persist the session")
    login_cmd.add_argument(
        "--wait", type=int, default=120,
        help="Seconds to keep the browser open for manual OTP entry",
    )
    login_cmd.add_argument(
        "-a", "--account", default=None,
        help="Which account to log in: primary|secondary (default: DEFAULT_ACCOUNT)",
    )

    return parser


async def _cmd_migrate() -> int:
    applied = await run_migrations()
    log.info("migrate.done", applied=applied)
    return 0


async def _cmd_run(args) -> int:
    settings = get_settings()
    config = load_config(args.config or settings.config_path)
    if args.headed:
        config.browser.headless = False

    if args.all_accounts:
        # Accounts run SEQUENTIALLY, never in parallel: two simultaneous logins
        # from one IP is the pattern most likely to trip Naukri's fraud checks.
        # Each Orchestrator owns its own browser + storage_state, so sessions
        # stay isolated.
        configured = settings.available_accounts()
        wanted = [key for key in config.accounts_in_use() if key in configured]
        skipped = [key for key in config.accounts_in_use() if key not in configured]
        if skipped:
            log.warning("run.accounts_skipped", accounts=skipped, reason="credentials not set")
        if not wanted:
            log.error("run.no_configured_accounts", profiles_reference=config.accounts_in_use())
            return 2
    else:
        wanted = [args.account or settings.default_account]

    exit_code = 0
    for index, account_key in enumerate(wanted):
        if index:
            # Cool-down between accounts.
            await asyncio.sleep(30)
        log.info("run.account_start", account=account_key, index=index + 1, total=len(wanted))
        orchestrator = Orchestrator(
            config,
            settings,
            mode="manual",
            only_profiles=args.profiles,
            dry_run=args.dry_run,
            account=account_key,
        )
        try:
            stats = await orchestrator.run()
        except RuntimeError as exc:
            # e.g. no profiles bound to this account: keep going with the rest.
            log.error("run.account_failed", account=account_key, error=str(exc))
            exit_code = 1
            continue
        # Non-zero when nothing worked, so cron/CI can alert.
        if not stats.applied and stats.errors:
            exit_code = 1
    return exit_code


async def _cmd_schedule(args) -> int:
    settings = get_settings()
    config = load_config(args.config or settings.config_path)
    if not config.schedule.enabled:
        log.error("schedule.disabled_in_config")
        return 2
    await AgentScheduler(config, settings).start()
    return 0


async def _cmd_stats() -> int:
    repo = await Repository.create()
    summary = await repo.summary()
    width = max(len(key) for key in summary)
    for key, value in summary.items():
        print(f"{key.ljust(width)} : {value}")

    today = await repo.applied_today_by_account()
    if today:
        print("\napplied today (per account, Asia/Kolkata):")
        for row in today:
            print(f"  {str(row['account']).ljust(12)} : {row['applied']}")

    recent = await repo.recent_applications(limit=15)
    if recent:
        print("\nrecent activity:")
        for row in recent:
            print(
                f"  [{row['status']:<15}] {str(row['title'])[:52]:<52} "
                f"{str(row['company'])[:24]:<24} {row['profile']}"
            )
    return 0


async def _cmd_accounts(args) -> int:
    """
    Answers "will a run actually work?" without launching a browser: which
    credentials exist, and which profiles are bound to each account.
    """
    settings = get_settings()
    config = load_config(args.config or settings.config_path)
    configured = set(settings.available_accounts())

    for key in ("primary", "secondary"):
        profiles = config.active_profiles(account=key)
        if key in configured:
            email = settings.account(key).masked_email
            state = f"configured ({email})"
        else:
            state = "MISSING credentials"
        print(f"{key.ljust(10)} : {state}")
        print(f"{''.ljust(10)}   cron: {config.schedule.cron_for(key)}")
        if profiles:
            for profile in profiles:
                sources = ["recommended"] if profile.use_recommended else []
                if profile.searches:
                    sources.append(f"search x{len(profile.searches)}")
                print(
                    f"{''.ljust(10)}   - {profile.name} "
                    f"(cap {profile.max_applications_per_run}, {' + '.join(sources) or 'no source'})"
                )
        else:
            print(f"{''.ljust(10)}   - no enabled profiles")
    return 0


async def _cmd_reviews(args) -> int:
    repo = await Repository.create()
    pending = await repo.pending_reviews(limit=args.limit)
    if not pending:
        print("No questions awaiting review.")
        return 0
    print(f"{len(pending)} question(s) awaiting an answer:\n")
    for row in pending:
        print(f"  id={row['id']}  profile={row['profile']}  seen={row['times_seen']}x")
        print(f"    Q: {row['question']}")
        if row["options"]:
            print(f"    options: {row['options']}")
        print(f"    resolve with: python -m naukri_agent resolve {row['id']} \"<answer>\"\n")
    return 0


async def _cmd_resolve(args) -> int:
    repo = await Repository.create()
    ok = await repo.resolve_review(args.review_id, args.answer)
    if ok:
        print(f"Review {args.review_id} resolved; answer promoted to the knowledge base.")
        return 0
    print(f"No pending review with id {args.review_id}.")
    return 1


async def _cmd_login(args) -> int:
    """
    Headed, human-assisted login. Use this once on a new machine (or after an OTP
    challenge): it persists `storage_state` to Postgres so every later headless
    run reuses the session instead of re-authenticating.
    """
    import asyncio as _asyncio

    from .browser.artifacts import ArtifactStore
    from .browser.manager import BrowserManager
    from .naukri.auth import NaukriAuth

    settings = get_settings()
    account = settings.validate_for_run(args.account)
    config = load_config(args.config or settings.config_path)
    config.browser.headless = False

    repo = await Repository.create()
    artifacts = ArtifactStore(settings.artifacts_dir, "login")
    print(f"Logging in account '{account.key}' ({account.masked_email})…")

    async with BrowserManager(
        config.browser, repo, session_key=account.session_key
    ) as browser:
        auth = NaukriAuth(browser, account.email, account.password, artifacts)
        try:
            page = await auth.ensure_logged_in()
            print("Logged in. Session persisted.")
        except Exception as exc:
            print(f"Automated login failed ({exc}).")
            print(f"Complete the login manually in the open window ({args.wait}s)…")
            page = await browser.new_page()
            await page.goto("https://www.naukri.com/nlogin/login")
            await _asyncio.sleep(args.wait)
            if await auth.is_logged_in(page):
                await browser.persist_session()
                print("Manual login detected and session persisted.")
            else:
                print("Still not logged in; session not saved.")
                return 1
    return 0


async def _dispatch(args) -> int:
    if args.command == "migrate":
        return await _cmd_migrate()
    if args.command == "run":
        return await _cmd_run(args)
    if args.command == "schedule":
        return await _cmd_schedule(args)
    if args.command == "stats":
        return await _cmd_stats()
    if args.command == "accounts":
        return await _cmd_accounts(args)
    if args.command == "reviews":
        return await _cmd_reviews(args)
    if args.command == "resolve":
        return await _cmd_resolve(args)
    if args.command == "login":
        return await _cmd_login(args)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    configure_logging(
        level=args.log_level or settings.log_level,
        json_output=args.json_logs or settings.log_json,
        log_dir=settings.log_dir,
    )

    async def runner() -> int:
        try:
            return await _dispatch(args)
        finally:
            await close_pool()

    try:
        return asyncio.run(runner())
    except KeyboardInterrupt:
        log.warning("cli.interrupted")
        return 130
    except FileNotFoundError as exc:
        log.error("cli.config_missing", error=str(exc))
        return 2
    except RuntimeError as exc:
        log.error("cli.startup_error", error=str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
