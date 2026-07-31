"""
Authentication.

Design decisions:

- **Session-first.** We never submit the login form if a restored
  `storage_state` still authenticates us. Repeated logins are the main trigger
  for Naukri's OTP / "unusual activity" challenge, so the cheapest way to stay
  unblocked is to log in rarely.
- **Verification by DOM marker, not by URL.** Naukri redirects to
  `/mnjuser/homepage` in some flows and keeps you on the login page in others,
  so we assert on logged-in markers instead.
- **Challenges are fatal, not retryable.** OTP and captcha need a human. We
  raise `FatalAgentError`, screenshot the page and notify — retrying would just
  harden the block.
- `ensure_logged_in()` is safe to call at any point mid-run; the apply loop uses
  it to recover from a silently expired session.
"""

from __future__ import annotations

import asyncio

from playwright.async_api import Page

from ..browser.artifacts import ArtifactStore
from ..browser.manager import BrowserManager
from ..browser.resilience import (
    FatalAgentError,
    TransientPageError,
    dismiss_overlays,
    first_visible,
    human_pause,
    human_type,
    retry_async,
    safe_text,
)
from ..logging_setup import get_logger
from . import selectors as S

log = get_logger(__name__)


class NaukriAuth:
    def __init__(
        self,
        browser: BrowserManager,
        email: str,
        password: str,
        artifacts: ArtifactStore,
    ) -> None:
        self.browser = browser
        self.email = email
        self.password = password
        self.artifacts = artifacts

    # ------------------------------------------------------------------ checks
    async def is_logged_in(self, page: Page) -> bool:
        marker = await first_visible(page, S.LOGGED_IN_MARKERS, timeout_ms=6_000)
        if marker is not None:
            return True
        logged_out = await first_visible(page, S.LOGGED_OUT_MARKERS, timeout_ms=2_000)
        return logged_out is None and "nlogin" not in page.url

    async def _detect_challenge(self, page: Page) -> None:
        if await first_visible(page, S.CAPTCHA_MARKERS, timeout_ms=1_500):
            await self.artifacts.capture_failure(page, "captcha")
            raise FatalAgentError(
                "Captcha / unusual-activity challenge shown. Manual login required."
            )
        if await first_visible(page, S.OTP_CHALLENGE, timeout_ms=1_500):
            await self.artifacts.capture_failure(page, "otp-challenge")
            raise FatalAgentError("Naukri requested an OTP. Log in manually once, then rerun.")

    # ------------------------------------------------------------------- login
    async def _submit_login(self, page: Page) -> None:
        log.info("auth.login_start", email=self.email.split("@")[0] + "@…")
        await page.goto(S.LOGIN_URL, wait_until="domcontentloaded")
        await dismiss_overlays(page)
        await self._detect_challenge(page)

        email_input = await first_visible(page, S.LOGIN_EMAIL_INPUT, timeout_ms=15_000)
        password_input = await first_visible(page, S.LOGIN_PASSWORD_INPUT, timeout_ms=8_000)
        if email_input is None or password_input is None:
            await self.artifacts.capture_failure(page, "login-form-missing")
            raise TransientPageError("Login form not found — Naukri markup may have changed")

        await human_type(email_input, self.email)
        await human_pause(200, 600)
        await human_type(password_input, self.password)
        await human_pause(300, 900)

        submit = await first_visible(page, S.LOGIN_SUBMIT, timeout_ms=6_000)
        if submit is None:
            raise TransientPageError("Login submit button not found")

        # Naukri sometimes performs a client-side transition instead of a full
        # navigation, so we race navigation against a marker appearing.
        try:
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=35_000):
                await submit.click()
        except Exception:
            log.debug("auth.no_navigation_after_submit")
            await asyncio.sleep(3)

        await dismiss_overlays(page)
        await self._detect_challenge(page)

        error = await first_visible(page, S.LOGIN_ERROR, timeout_ms=2_500)
        if error is not None:
            message = await safe_text(error, "unknown login error")
            await self.artifacts.capture_failure(page, "login-error")
            # Wrong credentials will never succeed on retry.
            if any(token in message.lower() for token in ("invalid", "incorrect", "not registered")):
                raise FatalAgentError(f"Naukri rejected the credentials: {message}")
            raise TransientPageError(f"Login error: {message}")

        if not await self.is_logged_in(page):
            await self.artifacts.capture_failure(page, "login-unverified")
            raise TransientPageError("Login submitted but no logged-in marker appeared")

        await self.browser.persist_session()
        log.info("auth.login_success")

    async def ensure_logged_in(self, page: Page | None = None) -> Page:
        """
        Idempotent. Returns a page that is guaranteed authenticated.
        Strategy: reuse session -> verify -> only then fall back to a real login.
        """
        page = page or await self.browser.new_page()

        if self.browser.session_restored:
            try:
                await page.goto(S.HOME_URL, wait_until="domcontentloaded", timeout=45_000)
                await dismiss_overlays(page)
                if await self.is_logged_in(page):
                    log.info("auth.session_reused")
                    return page
                log.info("auth.session_expired")
                await self.browser.invalidate_session()
            except Exception as exc:
                log.warning("auth.session_check_failed", error=str(exc))

        await retry_async(
            lambda: self._submit_login(page),
            attempts=3,
            base_delay=5.0,
            label="naukri-login",
        )
        return page

    async def reauthenticate(self, page: Page) -> None:
        """Called when a mid-run action detects a logged-out state."""
        log.warning("auth.reauthenticating")
        await self.browser.invalidate_session()
        await retry_async(
            lambda: self._submit_login(page),
            attempts=2,
            base_delay=6.0,
            label="naukri-relogin",
        )
