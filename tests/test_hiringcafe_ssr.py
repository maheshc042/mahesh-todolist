"""
Unit tests for the HiringCafe SSR path (no browser needed): search-URL shape,
ATS-link picking, and relative-timestamp parsing.
"""
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

from naukri_agent.config import NaukriAccount
from naukri_agent.core.run_policy import RunPolicy
from naukri_agent.platforms import hiringcafe as hc_mod
from naukri_agent.platforms.base import PlatformWalledError
from naukri_agent.platforms.hiringcafe import HiringCafePlatform as HC


class TestSearchUrl(unittest.TestCase):
    def test_round_trips_through_search_state(self):
        url = HC._build_search_url("https://hiring.cafe", "AI Engineer", 3, 3, 0)
        parts = urlsplit(url)
        self.assertEqual(parts.netloc, "hiring.cafe")
        query = dict(parse_qsl(parts.query))
        state = json.loads(query["searchState"])
        self.assertEqual(state["searchQuery"], "AI Engineer")
        self.assertEqual(state["dateFetchedPastNDays"], 3)
        self.assertEqual(state["roleYoeRange"], [0, 3])
        self.assertIn("Engineering", state["departments"])
        self.assertNotIn("page", query)

    def test_page_param_only_when_paged(self):
        url = HC._build_search_url("https://hiring.cafe/", "QA", 2, 3, 2)
        self.assertIn("page=2", urlsplit(url).query)


class TestSelectApplyUrl(unittest.TestCase):
    def test_prefers_apply_anchor(self):
        cands = [
            {"href": "https://hiringcafe.com/job/x", "text": "Job Posting"},
            {"href": "https://boards.greenhouse.io/acme/1", "text": "Life at Acme"},
            {"href": "https://careers.acme.com/1", "text": "Apply now"},
        ]
        self.assertEqual(HC._select_apply_url(cands), "https://careers.acme.com/1")

    def test_skips_forms_and_social(self):
        cands = [
            {"href": "https://forms.gle/abc", "text": "Apply here"},
            {"href": "https://www.linkedin.com/company/acme", "text": "LinkedIn"},
            {"href": "https://jobs.lever.co/acme/9", "text": "Jobs"},
        ]
        self.assertEqual(HC._select_apply_url(cands), "https://jobs.lever.co/acme/9")

    def test_none_when_nothing_qualifies(self):
        self.assertIsNone(HC._select_apply_url([]))
        self.assertIsNone(HC._select_apply_url([
            {"href": "/job/internal", "text": "Job Posting"},
            {"href": "https://docs.google.com/forms/d/e/x", "text": "Apply"},
        ]))


class TestCardPrefilter(unittest.TestCase):
    def test_reject_saves_a_navigation(self):
        self.assertFalse(HC._card_prefilter_pass(
            "Manager Oracle SOA Suite Pune Onsite", ["ai", "python", "react"]))

    def test_keep_on_any_match(self):
        self.assertTrue(HC._card_prefilter_pass(
            "Software Engineer Python Bangalore", ["ai", "python", "react"]))

    def test_fail_open_without_terms(self):
        self.assertTrue(HC._card_prefilter_pass("anything", []))


class TestPostedDays(unittest.TestCase):
    def test_hours_is_today(self):
        self.assertEqual(HC._posted_days_from_text("2h Save Manager"), 0)

    def test_days_parsed(self):
        self.assertEqual(HC._posted_days_from_text("3d Software Engineer"), 3)

    def test_unknown_is_none(self):
        self.assertIsNone(HC._posted_days_from_text("Save Hide"))


class _FakeLocator:
    first = None

    def __init__(self):
        self.first = self

    async def count(self):
        return 0

    async def is_visible(self):
        return False


class _AlwaysChallengedPage:
    """Every navigation lands on the Cloudflare interstitial."""

    url = "https://hiringcafe.com/?searchState=x"
    frames: list = []

    async def goto(self, *args, **kwargs):
        return None

    async def reload(self, *args, **kwargs):
        return None

    async def title(self):
        return "Just a moment..."

    def locator(self, *args, **kwargs):
        return _FakeLocator()

    async def wait_for_selector(self, *args, **kwargs):
        raise TimeoutError("no cards behind the wall")


async def _no_pause(*args, **kwargs):
    return None


class TestWallRaise(unittest.IsolatedAsyncioTestCase):
    """Run 470: the wall-raise path referenced a name the module no longer
    imported -> NameError instead of a clean platform pause."""

    def setUp(self):
        self._pause_patch = patch.object(hc_mod, "human_pause", _no_pause)
        self._pause_patch.start()
        self.addCleanup(self._pause_patch.stop)
        account = NaukriAccount("primary", "a@b.c", "x")
        policy = RunPolicy(dry_run=True, side_effects_enabled=False)
        self.platform = HC(_AlwaysChallengedPage(), account, artifacts=None,
                           policy=policy, config=None)

    def test_walled_error_name_resolves(self):
        self.assertIs(hc_mod.PlatformWalledError, PlatformWalledError)

    async def test_persistent_wall_raises_not_nameerror(self):
        self.platform._walls = 2  # third strike trips the abort
        with self.assertRaises(PlatformWalledError):
            await self.platform._collect_detail_urls(
                "https://hiring.cafe", "ai", 3, 3, 1, set())


if __name__ == "__main__":
    unittest.main()
