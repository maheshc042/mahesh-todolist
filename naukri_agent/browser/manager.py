"""
Playwright lifecycle + session persistence.

Design decisions:

- **Chromium with a real UA and India locale.** Naukri serves different markup
  to unknown user agents and its salary/date strings are locale-sensitive.
- **`storage_state` stored in Postgres, not on disk.** The container is
  disposable (Docker/Fly/ECS); persisting cookies + localStorage in the
  `browser_sessions` table means a redeploy does not trigger a fresh login (and
  fresh logins are what triggers OTP/captcha challenges).
- **Anti-automation hardening via an init script.** We remove
  `navigator.webdriver` and patch the handful of properties headless Chromium
  leaks. This is not a full stealth suite — it is the minimum needed to keep
  Naukri from serving the degraded/bot page.
- **Resource blocking** for fonts/media cuts page weight substantially, which
  matters because a run loads hundreds of pages.
- Context manager semantics guarantee the browser is closed even when the run
  raises, otherwise a scheduled container leaks a Chromium process per run.
"""

from __future__ import annotations

from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from ..config import BrowserConfig
from ..db.repository import Repository
from ..logging_setup import get_logger

log = get_logger(__name__)

# Fallback only. Real runs pass the account-scoped key from
# `NaukriAccount.session_key`: a single shared key meant account B's cookies
# overwrote account A's on every run, so the two logins fought each other.
SESSION_KEY = "naukri:session:primary"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Executed in every page before any site script runs.
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-IN', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
window.chrome = window.chrome || { runtime: {} };
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
  parameters.name === 'notifications'
    ? Promise.resolve({ state: Notification.permission })
    : originalQuery(parameters)
);
"""

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",  # required in Docker: /dev/shm is 64MB by default
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-gpu",
    "--disable-notifications",
    "--disable-popup-blocking",
    "--window-size=1440,900",
]


class BrowserManager:
    def __init__(
        self,
        config: BrowserConfig,
        repo: Repository | None = None,
        session_key: str = SESSION_KEY,
    ) -> None:
        self.config = config
        self.repo = repo
        # One storage-state row per Naukri account.
        self.session_key = session_key or SESSION_KEY
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._restored_session = False

    # ------------------------------------------------------------- lifecycle
    async def __aenter__(self) -> "BrowserManager":
        await self.start()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.stop()

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.config.headless,
            slow_mo=self.config.slow_mo_ms,
            args=LAUNCH_ARGS,
        )

        storage_state: dict[str, Any] | None = None
        if self.repo is not None:
            storage_state = await self.repo.load_session(self.session_key)
            self._restored_session = storage_state is not None

        self._context = await self._browser.new_context(
            viewport={
                "width": self.config.viewport_width,
                "height": self.config.viewport_height,
            },
            user_agent=self.config.user_agent or DEFAULT_UA,
            locale=self.config.locale,
            timezone_id=self.config.timezone,
            storage_state=storage_state,  # type: ignore[arg-type]
            java_script_enabled=True,
            ignore_https_errors=True,
            permissions=[],
        )
        self._context.set_default_timeout(self.config.default_timeout_ms)
        self._context.set_default_navigation_timeout(self.config.navigation_timeout_ms)
        await self._context.add_init_script(STEALTH_SCRIPT)

        if self.config.block_resources:
            await self._context.route("**/*", self._route_filter)

        log.info(
            "browser.started",
            headless=self.config.headless,
            session_restored=self._restored_session,
        )

    async def _route_filter(self, route: Any, request: Any) -> None:
        if request.resource_type in self.config.block_resources:
            await route.abort()
            return
        # Third-party trackers add seconds per page and nothing of value.
        blocked_hosts = ("googletagmanager.com", "google-analytics.com", "doubleclick.net",
                         "hotjar.com", "clarity.ms", "facebook.net")
        if any(host in request.url for host in blocked_hosts):
            await route.abort()
            return
        await route.continue_()

    async def stop(self) -> None:
        try:
            if self._context is not None:
                await self.persist_session()
                await self._context.close()
        finally:
            if self._browser is not None:
                await self._browser.close()
            if self._playwright is not None:
                await self._playwright.stop()
            self._context = None
            self._browser = None
            self._playwright = None
            log.info("browser.stopped")

    # ---------------------------------------------------------------- access
    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("BrowserManager.start() was not awaited")
        return self._context

    @property
    def session_restored(self) -> bool:
        return self._restored_session

    async def new_page(self) -> Page:
        page = await self.context.new_page()
        page.set_default_timeout(self.config.default_timeout_ms)
        page.set_default_navigation_timeout(self.config.navigation_timeout_ms)
        return page

    # --------------------------------------------------------------- session
    async def persist_session(self) -> None:
        """Save cookies + localStorage so the next run skips the login form."""
        if self.repo is None or self._context is None:
            return
        try:
            state = await self._context.storage_state()
            await self.repo.save_session(self.session_key, dict(state))
            log.info(
                "browser.session_saved",
                key=self.session_key,
                cookies=len(state.get("cookies", [])),
            )
        except Exception as exc:
            log.warning("browser.session_save_failed", error=str(exc))

    async def invalidate_session(self) -> None:
        if self.repo is not None:
            await self.repo.clear_session(self.session_key)
        self._restored_session = False
        log.info("browser.session_invalidated", key=self.session_key)
