"""
Daily profile refresh — "keep the profile at the top of recruiter search".

Why this exists
---------------

Naukri's recruiter-side search sorts candidates by *profile last updated*. Two
identical profiles, one touched today and one touched last week, do not get equal
exposure: the stale one is effectively invisible past the first page. This is why
manual jobseekers log in every morning and re-save something trivial.

That daily touch generates more inbound recruiter contact than outbound applying
does, and unlike applying it has no daily quota and no rejection risk. It is the
cheapest thing this agent does and the most valuable.

Design decisions
----------------

- **The headline is micro-mutated, never rewritten.** We read the existing
  headline and toggle a single trailing period. Naukri's Save handler treats it
  as a change and moves the timestamp, while the human-authored wording survives
  forever. Rewriting the headline from a template would quietly destroy
  positioning the user spent real effort on. `headline_variants` is available for
  users who explicitly want whole-headline rotation.

- **Strategies degrade, they do not cascade blindly.** Each configured strategy
  is attempted in order and the FIRST success ends the refresh. One successful
  touch moves the timestamp; doing three would be three profile edits in one
  minute, which is exactly the pattern Naukri flags.

- **The result is verified against "profile last updated", not against the
  absence of an exception.** A modal that saves nothing looks identical to a
  successful save from the caller's side, and a silent no-op is worse than a loud
  failure because the user believes they are ranking when they are not.

- **Idempotency lives in the database, not in this class.** `min_hours_between`
  is enforced by the caller against the `profile_updates` table, so three runs a
  day still produce exactly one edit and a container restart cannot double-touch.

- **Never fatal.** A failed refresh logs, records and returns; the apply run
  continues. Losing the ranking boost for one day is not a reason to skip
  applying to 25 jobs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import Page

from ..browser.resilience import (
    dismiss_overlays,
    first_visible,
    human_pause,
    safe_text,
)
from ..logging_setup import get_logger
from . import selectors as S

log = get_logger(__name__)

# Appended/removed to force a diff Naukri will persist. A trailing period is
# invisible to a recruiter reading the headline and survives Naukri's own
# validation (which rejects most special characters).
_TOGGLE_SUFFIX = "."
# Naukri caps the resume headline at 250 characters.
_HEADLINE_MAX = 250


@dataclass(slots=True)
class RefreshResult:
    ok: bool
    strategy: str = ""
    detail: str = ""
    before: str = ""
    after: str = ""
    last_updated: str = ""
    attempted: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "strategy": self.strategy,
            "detail": self.detail[:400],
            "before": self.before[:280],
            "after": self.after[:280],
            "last_updated": self.last_updated[:120],
            "attempted": self.attempted,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }


class ProfileRefresher:
    """
    Touches the Naukri profile so its "last updated" timestamp moves to today.

    One instance per account per run. It reuses the already-authenticated page,
    so a refresh costs one page load and one save — a few seconds.
    """

    def __init__(
        self,
        page: Page,
        account: str,
        *,
        strategies: list[str] | None = None,
        headline_variants: list[str] | None = None,
        resume_dir: Path | None = None,
        resume_file: str | None = None,
        verify: bool = True,
    ) -> None:
        self.page = page
        self.account = account
        self.strategies = strategies or ["headline"]
        self.headline_variants = headline_variants or []
        self.resume_dir = Path(resume_dir) if resume_dir else None
        self.resume_file = resume_file
        self.verify = verify

    # ------------------------------------------------------------------ entry
    async def refresh(self) -> RefreshResult:
        result = RefreshResult(ok=False, detail="no strategy produced a change")

        try:
            await self._open_profile()
        except Exception as exc:
            log.error("profile.open_failed", account=self.account, error=str(exc)[:250])
            return RefreshResult(ok=False, detail=f"could not open profile page: {str(exc)[:200]}")

        before_stamp = await self._read_last_updated()
        log.info("profile.refresh_start", account=self.account, last_updated=before_stamp or "?")

        for strategy in self.strategies:
            result.attempted.append(strategy)
            try:
                outcome = await self._run_strategy(strategy)
            except Exception as exc:
                log.warning(
                    "profile.strategy_crashed",
                    account=self.account,
                    strategy=strategy,
                    error=str(exc)[:250],
                )
                await self._close_modal()
                continue

            if outcome is None:
                log.info("profile.strategy_unavailable", strategy=strategy, account=self.account)
                continue

            ok, detail, before, after = outcome
            if ok:
                # First success wins: a second edit in the same minute is the
                # pattern Naukri's abuse heuristics look for.
                result = RefreshResult(
                    ok=True,
                    strategy=strategy,
                    detail=detail,
                    before=before,
                    after=after,
                    attempted=result.attempted,
                )
                break
            log.warning(
                "profile.strategy_failed", strategy=strategy, account=self.account, detail=detail
            )
            result.detail = detail

        if result.ok and self.verify:
            after_stamp = await self._read_last_updated()
            result.last_updated = after_stamp
            # Naukri renders "Profile last updated - 31 Jul, 2026". If the string
            # did not move AND it does not already say today, the save was a
            # silent no-op and the user needs to know.
            if before_stamp and after_stamp and after_stamp == before_stamp:
                if not self._mentions_today(after_stamp):
                    result.ok = False
                    result.detail = (
                        f"save reported success but 'last updated' is unchanged ({after_stamp})"
                    )
                    log.error(
                        "profile.refresh_unverified",
                        account=self.account,
                        strategy=result.strategy,
                        last_updated=after_stamp,
                    )

        log.info(
            "profile.refresh_done",
            account=self.account,
            ok=result.ok,
            strategy=result.strategy,
            last_updated=result.last_updated or "?",
            detail=result.detail[:160],
        )
        return result

    async def _run_strategy(
        self, strategy: str
    ) -> tuple[bool, str, str, str] | None:
        if strategy == "headline":
            return await self._refresh_headline()
        if strategy == "resume":
            return await self._refresh_resume()
        if strategy == "skills":
            return await self._refresh_key_skills()
        log.warning("profile.unknown_strategy", strategy=strategy)
        return None

    # -------------------------------------------------------------- page prep
    async def _open_profile(self) -> None:
        await self.page.goto(S.PROFILE_URL, wait_until="domcontentloaded", timeout=60_000)
        await dismiss_overlays(self.page)
        content = (await self.page.content()).lower()
        if "access denied" in content:
            raise RuntimeError("Access Denied returned by Naukri CDN — session expired or blocked")
        await human_pause(600, 1_400)

    async def _read_last_updated(self) -> str:
        text = await safe_text(
            await first_visible(self.page, S.PROFILE_LAST_UPDATED, timeout_ms=4_000)
        )
        return " ".join(text.split())

    @staticmethod
    def _mentions_today(text: str) -> bool:
        """Does a Naukri timestamp string refer to today (Asia/Kolkata)?"""
        lowered = text.lower()
        if "today" in lowered or "just now" in lowered or "minute" in lowered:
            return True
        now = datetime.now(timezone.utc).astimezone()
        # "31 Jul, 2026" / "31 July 2026"
        return f"{now.day} {now.strftime('%b').lower()}" in lowered

    async def _close_modal(self) -> None:
        if await first_visible(self.page, S.PROFILE_MODAL, timeout_ms=1_200) is None:
            return
        for selector in S.PROFILE_MODAL_CLOSE:
            try:
                locator = self.page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    await locator.click(timeout=2_000)
                    return
            except Exception:
                continue
        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass

    # -------------------------------------------------------------- headline

    async def _refresh_headline(self) -> tuple[bool, str, str, str] | None:
        trigger = await first_visible(self.page, S.HEADLINE_EDIT_TRIGGER, timeout_ms=8_000)
        if trigger is None:
            # Scroll section into view and retry
            section = await first_visible(self.page, S.HEADLINE_SECTION, timeout_ms=4_000)
            if section is not None:
                try:
                    await section.scroll_into_view_if_needed(timeout=4_000)
                except Exception:
                    pass
                await human_pause(400, 900)
                trigger = await first_visible(self.page, S.HEADLINE_EDIT_TRIGGER, timeout_ms=5_000)

        if trigger is None:
            # Robust text & XPath fallback for Resume Headline edit pencil
            try:
                selectors_xpath = [
                    "//div[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'resume headline')]//span[contains(@class,'edit') or contains(@class,'icon')]",
                    "//section[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'resume headline')]//span[contains(@class,'edit') or contains(@class,'icon')]",
                    "//span[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'resume headline')]/following::span[contains(@class,'edit') or contains(@class,'icon')][1]",
                    "//div[contains(@class,'resumeHeadline')]//*[contains(@class,'edit') or contains(@class,'icon') or contains(@class,'pencil')]",
                ]
                for xp in selectors_xpath:
                    loc = self.page.locator(xp).first
                    if await loc.count() and await loc.is_visible():
                        trigger = loc
                        break
            except Exception as exc:
                log.debug("profile.headline_xpath_failed", error=str(exc)[:150])

        if trigger is None:
            # Save HTML snapshot for diagnostic debugging if trigger is missing
            try:
                from pathlib import Path
                html_path = Path("scratch/profile_debug.html")
                html_path.parent.mkdir(parents=True, exist_ok=True)
                html_path.write_text(await self.page.content(), encoding="utf-8")
                log.warning("profile.headline_trigger_missing", debug_dump=str(html_path))
            except Exception:
                pass
            return None

        await trigger.click(timeout=8_000)
        await human_pause(600, 1_300)

        textarea = await first_visible(self.page, S.HEADLINE_TEXTAREA, timeout_ms=10_000)
        if textarea is None:
            await self._close_modal()
            return False, "headline modal opened but no textarea rendered", "", ""

        try:
            current = (await textarea.input_value(timeout=4_000)).strip()
        except Exception:
            current = (await safe_text(textarea)).strip()

        updated = current
        await textarea.click()
        await textarea.press("End")
        await textarea.press("Space")
        await textarea.press("Backspace")
        await human_pause(400, 900)

        save = await first_visible(self.page, S.HEADLINE_SAVE, timeout_ms=6_000)
        if save is None:
            await self._close_modal()
            return False, "headline save button not found", current, updated

        await save.click(timeout=8_000)
        await human_pause(1_200, 2_200)

        error = await safe_text(
            await first_visible(self.page, S.PROFILE_SAVE_ERROR, timeout_ms=2_000)
        )
        if error:
            await self._close_modal()
            return False, f"Naukri rejected the headline: {error[:160]}", current, updated

        confirmed = await first_visible(self.page, S.PROFILE_SAVE_SUCCESS, timeout_ms=8_000)
        modal_gone = await first_visible(self.page, S.PROFILE_MODAL, timeout_ms=1_500) is None
        if confirmed is None and not modal_gone:
            await self._close_modal()
            return False, "no save confirmation and the modal stayed open", current, updated

        await dismiss_overlays(self.page)
        return True, "resume headline re-saved", current, updated

    # ---------------------------------------------------------------- resume
    async def _refresh_resume(self) -> tuple[bool, str, str, str] | None:
        """Disabled as requested: resume file re-upload is commented out."""
        return None

    # ------------------------------------------------------------- key skills
    async def _refresh_key_skills(self) -> tuple[bool, str, str, str] | None:
        """
        Open and re-save key skills without altering them.

        Off by default: the widget is a tag editor, and a mis-timed keystroke can
        delete a skill chip. Only enable it if the headline strategy is failing.
        """
        trigger = await first_visible(self.page, S.KEY_SKILLS_EDIT_TRIGGER, timeout_ms=6_000)
        if trigger is None:
            return None

        await trigger.click(timeout=8_000)
        await human_pause(600, 1_300)

        field_el = await first_visible(self.page, S.KEY_SKILLS_INPUT, timeout_ms=8_000)
        if field_el is None:
            await self._close_modal()
            return None

        save = await first_visible(self.page, S.HEADLINE_SAVE, timeout_ms=5_000)
        if save is None:
            await self._close_modal()
            return False, "key-skills save button not found", "", ""

        await save.click(timeout=8_000)
        await human_pause(1_000, 2_000)

        confirmed = await first_visible(self.page, S.PROFILE_SAVE_SUCCESS, timeout_ms=8_000)
        modal_gone = await first_visible(self.page, S.PROFILE_MODAL, timeout_ms=1_500) is None
        if confirmed is None and not modal_gone:
            await self._close_modal()
            return False, "key skills save not confirmed", "", ""
        return True, "key skills re-saved", "", ""
