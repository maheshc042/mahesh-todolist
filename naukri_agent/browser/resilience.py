"""
Resilience primitives.

Naukri's DOM is unstable: A/B tested layouts, lazy-hydrated React widgets,
modals that appear 2 seconds late, and occasional 5xx pages. Three primitives
absorb that instability:

1. `first_visible(page, selectors)` — try a ranked list of selectors and return
   the first one that actually exists. This is how we survive markup changes
   without redeploying: add a new selector to the list, keep the old ones.
2. `retry_async` — exponential backoff with jitter for whole operations
   (navigate + parse), not just single clicks.
3. `human_pause` / `human_type` — randomised pacing. Deterministic 0 ms
   interaction bursts are the single strongest bot signal; jitter is cheap
   insurance and also lets slow React handlers settle.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Iterable
from typing import TypeVar

from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PWTimeoutError

from ..logging_setup import get_logger

log = get_logger(__name__)

T = TypeVar("T")


class TransientPageError(RuntimeError):
    """Raised when the page is in a recoverable-but-wrong state."""


class FatalAgentError(RuntimeError):
    """Raised when retrying cannot help (bad credentials, account blocked)."""


async def human_pause(min_ms: int = 350, max_ms: int = 1_400) -> None:
    await asyncio.sleep(random.uniform(min_ms, max_ms) / 1000)


async def human_type(locator: Locator, text: str, min_delay: int = 35, max_delay: int = 110) -> None:
    """Type with per-character jitter (Playwright's `delay` is a fixed value)."""
    await locator.click()
    for char in text:
        await locator.press_sequentially(char, delay=random.uniform(min_delay, max_delay))


async def first_visible(
    scope: Page | Locator,
    selectors: Iterable[str],
    timeout_ms: int = 6_000,
) -> Locator | None:
    """
    Return the first selector in `selectors` that resolves to a visible element.
    The total budget is spread across candidates so a 6-selector list cannot
    stall a run for a minute.
    """
    candidates = list(selectors)
    if not candidates:
        return None
    per_selector = max(400, int(timeout_ms / len(candidates)))
    for selector in candidates:
        locator = scope.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=per_selector)
            return locator
        except PWTimeoutError:
            continue
        except Exception as exc:
            log.debug("resilience.selector_error", selector=selector, error=str(exc))
            continue
    return None


async def click_if_present(
    scope: Page | Locator, selectors: Iterable[str], timeout_ms: int = 3_000
) -> bool:
    locator = await first_visible(scope, selectors, timeout_ms)
    if locator is None:
        return False
    try:
        await locator.click(timeout=timeout_ms)
        return True
    except Exception as exc:
        log.debug("resilience.click_failed", error=str(exc))
        return False


async def safe_text(locator: Locator | None, default: str = "") -> str:
    if locator is None:
        return default
    try:
        if await locator.count() == 0:
            return default
        value = await locator.inner_text(timeout=1500)
        return " ".join(value.split())
    except Exception:
        return default



async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 1.5,
    max_delay: float = 20.0,
    retry_on: tuple[type[BaseException], ...] = (PWTimeoutError, TransientPageError, OSError),
    label: str = "operation",
    on_retry: Callable[[int, BaseException], Awaitable[None]] | None = None,
) -> T:
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except FatalAgentError:
            raise
        except retry_on as exc:
            last_error = exc
            if attempt == attempts:
                break
            delay = base_delay * (2 ** (attempt - 1))
            delay *= random.uniform(0.75, 1.35)  # jitter avoids lockstep retries
            delay = min(max_delay, delay)
            log.warning(
                "retry.backoff",
                label=label,
                attempt=attempt,
                of=attempts,
                sleep_s=round(delay, 2),
                error=str(exc)[:200],
            )
            if on_retry:
                try:
                    await on_retry(attempt, exc)
                except Exception as retry_exc:
                    # A failing callback must not mask the original error or
                    # abort the retry loop.
                    log.warning("retry.on_retry_failed", label=label, error=str(retry_exc)[:150])
            await asyncio.sleep(delay)
    assert last_error is not None
    raise last_error


async def dismiss_overlays(page: Page) -> None:
    """
    Naukri throws chat widgets, cookie banners, push-notification prompts and
    "complete your profile" modals over the page. Any of them will intercept a
    click, so we sweep them before every meaningful interaction.
    """
    overlay_closers = [
        "#chatbot_Drawer .crossIcon",
        ".crossIcon",
        "div.chatbot_Header span.crossIcon",
        "button[aria-label='Close']",
        ".nI-gNb-drawer__close",
        "#pushNotificationsDialogBox .close",
        ".onboarding-close",
        ".modal-close",
        "[class*='cookie'] button",
    ]
    for selector in overlay_closers:
        try:
            locator = page.locator(selector).first
            if await locator.count() and await locator.is_visible():
                await locator.click(timeout=1_500)
                log.debug("resilience.overlay_dismissed", selector=selector)
                await asyncio.sleep(0.2)
        except Exception:
            continue
    # Escape closes most Naukri React modals as a final fallback.
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass


async def scroll_page(page: Page, steps: int = 6, delay_s: float = 0.35) -> None:
    """Naukri lazy-loads job cards on scroll; stepped scrolling triggers it."""
    for _ in range(steps):
        await page.mouse.wheel(0, random.randint(600, 1000))
        await asyncio.sleep(delay_s)
