"""
Stealth LinkedIn Post Scraper (Disabled / Commented Out for Future Use).

Design Decisions:
- Persistent Context: Uses a persistent browser profile (`artifacts/linkedin_chrome_profile`) to store tracking cookies & tokens.
- JS Stealth Injection: Masks `navigator.webdriver`, languages, and plugins to evade basic anti-bot scripts.
- Human Pacing: Scrolls with variable delays and expands "...see more" buttons to reveal post text.
- Manual Login Fallback: Waits up to 60s for manual login in non-headless mode if redirected to login/authwall.
"""

# import asyncio
# import os
# import re
# from pathlib import Path
# from typing import Any

# from playwright.async_api import BrowserContext, Page, async_playwright

# from ..logging_setup import get_logger
# from .analyzer import classify_role, extract_recruiter_emails, is_experience_match

# log = get_logger(__name__)


# class CookieExpiredError(Exception):
#     """Raised when the LinkedIn li_at authentication cookie has expired."""


# class LinkedInHunter:
#     def __init__(self, li_at_cookie: str, headless: bool = True) -> None:
#         self.li_at_cookie = (li_at_cookie or "").strip()
#         self.headless = headless

#     async def _setup_stealth_context(self, p: Any) -> BrowserContext:
#         """Creates a persistent browser context that stores session state and evades anti-bot detection."""
#         profile_dir = Path("artifacts/linkedin_chrome_profile").resolve()
#         profile_dir.mkdir(parents=True, exist_ok=True)

#         context = await p.chromium.launch_persistent_context(
#             user_data_dir=str(profile_dir),
#             headless=self.headless,
#             viewport={"width": 1440, "height": 900},
#             user_agent=(
#                 "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
#                 "AppleWebKit/537.36 (KHTML, like Gecko) "
#                 "Chrome/131.0.0.0 Safari/537.36"
#             ),
#             args=["--disable-blink-features=AutomationControlled"],
#         )

#         if self.li_at_cookie:
#             clean_cookie = self.li_at_cookie.strip().strip('"').strip("'")
#             jsession_id = os.getenv("LINKEDIN_JSESSIONID", "").strip().strip('"').strip("'")

#             cookies_to_add = [
#                 {
#                     "name": "li_at",
#                     "value": clean_cookie,
#                     "domain": ".linkedin.com",
#                     "path": "/",
#                     "secure": True,
#                     "httpOnly": True,
#                 }
#             ]
#             if jsession_id:
#                 cookies_to_add.append(
#                     {
#                         "name": "JSESSIONID",
#                         "value": jsession_id,
#                         "domain": ".linkedin.com",
#                         "path": "/",
#                         "secure": True,
#                     }
#                 )

#             await context.add_cookies(cookies_to_add)
#         return context

#     async def _human_scroll(self, page: Page, max_scrolls: int = 6) -> None:
#         """Scrolls down slowly to load older posts."""
#         for i in range(max_scrolls):
#             await page.mouse.wheel(0, 1000)
#             await asyncio.sleep(2.0 + (i % 2) * 0.5)

#     async def _expand_long_posts(self, page: Page) -> None:
#         """Finds and clicks all '...see more' buttons to reveal hidden post text and email addresses."""
#         selectors = [
#             "button.feed-shared-inline-show-more-text__button",
#             "button.see-more",
#             "button[aria-label*='see more']",
#             "button[aria-label*='See more']",
#         ]
#         for sel in selectors:
#             try:
#                 buttons = await page.locator(sel).all()
#                 for btn in buttons[:15]:
#                     if await btn.is_visible():
#                         await btn.click()
#                         await asyncio.sleep(0.3)
#             except Exception:
#                 continue

#     async def hunt_for_jobs(self, search_url: str) -> list[dict[str, Any]]:
#         """
#         Navigates to the search URL, waits for posts, extracts text, classifies roles, and extracts emails.
#         """
#         results: list[dict[str, Any]] = []
#         log.info("linkedin.hunt_started", url=search_url[:70])

#         async with async_playwright() as p:
#             context = await self._setup_stealth_context(p)
#             page = context.pages[0] if context.pages else await context.new_page()

#             await page.add_init_script("""
#                 Object.defineProperty(navigator, 'webdriver', { get: () => false });
#                 window.chrome = { runtime: {}, loadTimes: function() {} };
#                 Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
#                 Object.defineProperty(navigator, 'languages', { get: () => ['en-IN', 'en-US', 'en'] });
#             """)

#             try:
#                 # Go directly to search URL
#                 await page.goto(search_url, wait_until="domcontentloaded", timeout=45_000)

#                 # Check if redirected to login
#                 if "login" in page.url.lower() or "authwall" in page.url.lower():
#                     if not self.headless:
#                         log.info("linkedin.manual_login", detail="Waiting 60 seconds for you to log in MANUALLY...")
#                         await page.wait_for_url("**/search/results/**", timeout=60_000)
#                         log.info("linkedin.manual_login_success", detail="Login detected! Session saved.")
#                     else:
#                         raise CookieExpiredError("Cookie rejected. Set headless=False to log in manually.")

#                 # Wait for search results to render
#                 try:
#                     await page.wait_for_selector("li.reusable-search__result-container, div.feed-shared-update-v2", timeout=15_000)
#                     await asyncio.sleep(2)
#                 except Exception:
#                     log.warning("linkedin.no_posts", detail="No posts appeared within 15 seconds. Page might be empty.")

#                 await self._human_scroll(page, max_scrolls=8)
#                 await self._expand_long_posts(page)

#                 containers = await page.locator(
#                     "li.reusable-search__result-container, div.feed-shared-update-v2, div.occludable-update, article"
#                 ).all()

#                 seen_texts: set[str] = set()

#                 for container in containers:
#                     text = await container.inner_text()
#                     if not text or text in seen_texts:
#                         continue
#                     seen_texts.add(text)

#                     emails = extract_recruiter_emails(text)
#                     if not emails:
#                         continue

#                     if not is_experience_match(text):
#                         continue

#                     role = classify_role(text)
#                     if not role:
#                         continue

#                     post_url = ""
#                     try:
#                         link_loc = container.locator(
#                             "a[href*='urn:li:activity'], a[href*='activity-']"
#                         ).first
#                         if await link_loc.count():
#                             href = await link_loc.get_attribute("href") or ""
#                             if href:
#                                 post_url = href if href.startswith("http") else f"https://www.linkedin.com{href}"
#                     except Exception:
#                         post_url = ""

#                     results.append({"text": text, "role": role, "emails": emails, "post_url": post_url})

#                 log.info("linkedin.hunt_completed", total_posts_read=len(containers), qualified_leads=len(results))

#             except CookieExpiredError:
#                 raise
#             except Exception as exc:
#                 err_msg = str(exc)
#                 if "ERR_TOO_MANY_REDIRECTS" in err_msg or "ERR_HTTP_RESPONSE_CODE_FAILURE" in err_msg:
#                     log.error("linkedin.blocked", detail="LinkedIn blocked the request. Try increasing delay between searches.")
#                 else:
#                     log.error("linkedin.scrape_crashed", error=err_msg[:200])
#             finally:
#                 await context.close()

#         return results
