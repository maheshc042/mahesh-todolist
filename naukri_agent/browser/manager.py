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

EXTRA_HTTP_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}


# Executed in every page before any site script runs to evade Cloudflare/DataDome.
STEALTH_SCRIPT = """
// 1. Hide navigator.webdriver — the most common bot check
Object.defineProperty(navigator, 'webdriver', { get: () => false });

// 2. Mock window.chrome (Headless browsers usually lack this)
window.chrome = {
    runtime: { id: undefined },
    loadTimes: function() {},
    csi: function() {},
    app: { isInstalled: false, getDetails: function() {}, getIsInstalled: function() {} }
};

// 3. Fake permissions API so it doesn't instantly reject notifications
if (window.navigator && window.navigator.permissions) {
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) => (
      parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
    );
}

// 4. Hide Playwright/Puppeteer/CDP specific leak variables
for (const key of Object.keys(window)) {
    if (key.startsWith('cdc_') || key.startsWith('__playwright') || key.startsWith('__selenium') || key.startsWith('__webdriver')) {
        delete window[key];
    }
}

// 5. Spoof plugins and languages to look like a real user
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const arr = [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
            { name: 'Native Client', filename: 'internal-nacl-plugin' }
        ];
        arr.item = (i) => arr[i];
        arr.namedItem = (n) => arr.find(p => p.name === n);
        arr.refresh = () => {};
        return arr;
    }
});
Object.defineProperty(navigator, 'languages', { get: () => ['en-IN', 'en-US', 'en'] });

// 6. Spoof hardware concurrency and device memory
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

// 7. Scrub automation names from error stacks. The constructor is wrapped,
// not replaced: statics (captureStackTrace, stackTraceLimit) and subclassing
// keep working by copying all own properties from the original.
const originalError = Error;
Error = function(...args) {
    const error = new originalError(...args);
    const stack = error.stack;
    if (stack && stack.includes('puppeteer') || stack && stack.includes('playwright')) {
        error.stack = stack.replace(/puppeteer|playwright/gi, 'chrome');
    }
    return error;
};
Object.setPrototypeOf(Error, originalError);
for (const key of Object.getOwnPropertyNames(originalError)) {
    try {
        if (!(key in Error)) {
            Object.defineProperty(Error, key, Object.getOwnPropertyDescriptor(originalError, key));
        }
    } catch (e) { /* non-configurable statics stay on the original */ }
}
Error.prototype = originalError.prototype;

// 8. Override toString to prevent detection via function source
const nativeToString = Function.prototype.toString;
Function.prototype.toString = function() {
    if (this === navigator.permissions.query) {
        return 'function query() { [native code] }';
    }
    return nativeToString.call(this);
};
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
        # Guards against writing a LOGGED-OUT storage_state back over a good one.
        # `stop()` persists unconditionally, so a run that invalidated the session
        # (expired cookies, OTP challenge) used to save the anonymous state on
        # teardown, guaranteeing a fresh form login next run — and repeated fresh
        # logins are the main trigger for Naukri's OTP/captcha challenge.
        self._session_trusted = False

    # ------------------------------------------------------------- lifecycle
    async def __aenter__(self) -> BrowserManager:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        try:
            await self._start_browser_locked()
        except Exception:
            # Never leak a headless Chromium when setup fails partway.
            try:
                await self.stop()
            except Exception:
                pass
            raise

    async def _start_browser_locked(self) -> None:
        assert self._playwright is not None
        self._browser = await self._playwright.chromium.launch(
            headless=self.config.headless,
            slow_mo=self.config.slow_mo_ms,
            # Use the Chromium bundled with the pinned Playwright image. A
            # system Chrome channel is not guaranteed to exist in production.
            args=LAUNCH_ARGS,
        )

        storage_state: dict[str, Any] | None = None
        if self.repo is not None:
            storage_state = await self.repo.load_session(self.session_key)
            self._restored_session = storage_state is not None
            # A restored session is trusted until something proves otherwise.
            self._session_trusted = self._restored_session

        self._context = await self._browser.new_context(
            viewport={
                "width": self.config.viewport_width,
                "height": self.config.viewport_height,
            },
            user_agent=self.config.user_agent or DEFAULT_UA,
            extra_http_headers=EXTRA_HTTP_HEADERS,
            locale=self.config.locale,
            timezone_id=self.config.timezone,
            storage_state=storage_state,  # type: ignore[arg-type]
            java_script_enabled=True,
            ignore_https_errors=False,
            permissions=[],
        )
        self._context.set_default_timeout(self.config.default_timeout_ms)
        self._context.set_default_navigation_timeout(self.config.navigation_timeout_ms)
        self._context._headed = not self.config.headless
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
                # Only if the session is still believed good — see
                # `_session_trusted`.
                await self.persist_session(only_if_trusted=True)
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
    async def persist_session(self, only_if_trusted: bool = False) -> None:
        """
        Save cookies + localStorage so the next run skips the login form.

        Called explicitly by the auth layer right after a verified login (which
        marks the session trusted), and implicitly on teardown with
        `only_if_trusted=True` so a logged-out state is never written back.
        """
        if self.repo is None or self._context is None:
            return
        if only_if_trusted and not self._session_trusted:
            log.info("browser.session_not_persisted", key=self.session_key, reason="untrusted")
            return
        try:
            state = await self._context.storage_state()
            cookies = state.get("cookies", [])
            if not cookies:
                # An empty jar overwriting a good session is strictly worse than
                # keeping the old one: Playwright returns this if the context was
                # never navigated.
                log.warning("browser.session_empty_skipped", key=self.session_key)
                return
            await self.repo.save_session(self.session_key, dict(state))
            self._session_trusted = True
            log.info("browser.session_saved", key=self.session_key, cookies=len(cookies))
        except Exception as exc:
            log.warning("browser.session_save_failed", error=str(exc))

    async def invalidate_session(self) -> None:
        if self.repo is not None:
            await self.repo.clear_session(self.session_key)
        self._restored_session = False
        self._session_trusted = False
        log.info("browser.session_invalidated", key=self.session_key)
