"""LinkedIn post analyzer: abroad-onsite filter + existing pins. No browser."""
import unittest
import unittest.mock

from naukri_agent.linkedin.analyzer import (
    classify_role,
    company_domain_match,
    extract_company,
    extract_first_name,
    extract_recruiter_emails,
    is_abroad_onsite,
    is_experience_match,
    is_spam_or_unpaid,
)


class TestAbroadOnsite(unittest.TestCase):
    def test_rejects_authorization_posts(self):
        self.assertTrue(is_abroad_onsite(
            "Hiring Python devs! Must be US citizen. Email hr@x.com"))

    def test_rejects_dollar_pay(self):
        self.assertTrue(is_abroad_onsite(
            "Backend role $120k/yr onsite. Apply at jobs@x.com"))

    def test_rejects_onsite_foreign_country(self):
        self.assertTrue(is_abroad_onsite(
            "Hiring for onsite in London. Email jobs@x.com"))

    def test_passes_remote_and_silent(self):
        self.assertFalse(is_abroad_onsite(
            "Hiring React devs, remote India. Email hr@x.com"))
        self.assertFalse(is_abroad_onsite(
            "Hiring Python devs with 2 years experience. Email hr@x.com"))
        self.assertFalse(is_abroad_onsite(
            "Hybrid role in Bengaluru for MERN devs. Email hr@x.com"))

    def test_passes_india_onsite(self):
        self.assertFalse(is_abroad_onsite(
            "Onsite in Hyderabad, 2-3 years. Email hr@x.com"))

    def test_us_staffing_markers_rejected(self):
        from naukri_agent.linkedin.analyzer import ABROAD_ONSITE_REJECT_REGEX as R
        for txt in (
            "USA-BASED CANDIDATES ONLY, W2/C2C contract",
            "H1 candidates only, EAD accepted",
            "OPT & Stem Extension students welcome",
            "Location: Sunrise, FL Day 1 onsite",
            "Position Arlington Virginia onsite 3 days",
            "Onsite in Lisle, IL long term",
        ):
            self.assertTrue(bool(R.search(txt)), txt)

    def test_hiring_poster_gate(self):
        from naukri_agent.linkedin.analyzer import is_likely_hiring_poster
        self.assertTrue(is_likely_hiring_poster("Technical Recruiter @ Aumnitech"))
        self.assertTrue(is_likely_hiring_poster("Human Resources Executive"))
        self.assertTrue(is_likely_hiring_poster("Founder @ Zyraune"))
        self.assertTrue(is_likely_hiring_poster(""))
        self.assertFalse(is_likely_hiring_poster("Python Developer | FastAPI | Open to Opportunities"))
        self.assertFalse(is_likely_hiring_poster("SDE @ Coder Army | AI Creator"))

    def test_iit_only_rejected(self):
        from naukri_agent.linkedin.analyzer import IIT_ONLY_REJECT_REGEX as R
        self.assertTrue(bool(R.search("Graduates from IITs/NITs only")))
        self.assertFalse(bool(R.search("Hiring Python devs in Bengaluru")))

    def test_zero_tech_rejected(self):
        from naukri_agent.linkedin.analyzer import ZERO_TECH_REJECT_REGEX as R
        self.assertTrue(bool(R.search("Golang Developer, AWS, prior Capital One exp")))
        self.assertTrue(bool(R.search("Spring Boot microservices role")))
        self.assertFalse(bool(R.search("Python FastAPI backend role")))


class TestExistingPins(unittest.TestCase):
    def test_senior_rejected(self):
        self.assertFalse(is_experience_match("Need 5+ years Python, Senior Engineer"))

    def test_spam_rejected(self):
        self.assertTrue(is_spam_or_unpaid("Unpaid internship, 50+ openings, DM on whatsapp"))

    def test_role_classification(self):
        self.assertEqual(classify_role("Hiring LLM engineer, Python RAG"), "AI / Python Engineer")
        self.assertEqual(classify_role("Hiring React frontend MERN"), "Full Stack Engineer")
        self.assertIsNone(classify_role("Hiring PHP WordPress dev"))

    def test_email_extraction_ignores_junk(self):
        mails = extract_recruiter_emails("Contact hr@x.com or see pic.png, also a@b.jpg")
        self.assertEqual(mails, ["hr@x.com"])


class TestProfileSkillOverlap(unittest.TestCase):
    def test_match(self):
        from naukri_agent.linkedin.analyzer import has_profile_skill_overlap
        skills = {"python", "react", "full stack"}
        self.assertTrue(has_profile_skill_overlap("Hiring Python devs, email x", skills))
        self.assertTrue(has_profile_skill_overlap("Full Stack Developer remote", skills))

    def test_no_match_rejected(self):
        from naukri_agent.linkedin.analyzer import has_profile_skill_overlap
        skills = {"python", "react"}
        self.assertFalse(has_profile_skill_overlap("We're hiring developers! Email us", skills))
        self.assertFalse(has_profile_skill_overlap("", skills))

    def test_empty_skills_fail_open(self):
        from naukri_agent.linkedin.analyzer import has_profile_skill_overlap
        self.assertTrue(has_profile_skill_overlap("anything", set()))
        self.assertTrue(has_profile_skill_overlap("anything", None))

    def test_word_boundaries(self):
        from naukri_agent.linkedin.analyzer import has_profile_skill_overlap
        self.assertFalse(has_profile_skill_overlap("Looking for a reaction video editor", {"react"}))
        self.assertTrue(has_profile_skill_overlap("React developer needed", {"react"}))


class TestRateLimitAndPriority(unittest.TestCase):
    def test_rate_wall_detected(self):
        from naukri_agent.linkedin.analyzer import is_rate_limit_page
        self.assertTrue(is_rate_limit_page(
            "Error 1200", "https://www.linkedin.com/search/results/content/?keywords=x",
            "This website has been temporarily rate limited"))
        self.assertTrue(is_rate_limit_page("Search | LinkedIn", "https://x", "Too many requests"))

    def test_normal_page_passes(self):
        from naukri_agent.linkedin.analyzer import is_rate_limit_page
        self.assertFalse(is_rate_limit_page("Search | LinkedIn", "https://www.linkedin.com/feed/", ""))
        # Challenge pages are NOT rate walls (cheap to skip, must not stop the run).
        self.assertFalse(is_rate_limit_page("Just a moment...", "https://x", ""))

    def test_company_domains_first_stable(self):
        from naukri_agent.linkedin.analyzer import prioritize_leads
        leads = [
            {"email": "a@gmail.com"},
            {"email": "hr@acme.com"},
            {"email": "b@yahoo.in"},
            {"email": "jobs@startup.io"},
        ]
        ordered = [lead["email"] for lead in prioritize_leads(leads)]
        self.assertEqual(ordered, ["hr@acme.com", "jobs@startup.io", "a@gmail.com", "b@yahoo.in"])


class TestScrollUntilStall(unittest.IsolatedAsyncioTestCase):
    async def test_stops_after_stall(self):
        from naukri_agent.linkedin.scraper import LinkedInHunter

        counts = iter([3, 5, 5, 5])

        class FakeLocator:
            async def count(self):
                return next(counts, 5)

        class FakeMouse:
            async def wheel(self, x, y):
                return None

        class FakePage:
            locator = lambda self, sel: FakeLocator()
            mouse = FakeMouse()

        hunter = LinkedInHunter(headless=True)
        stats = await hunter._human_scroll(FakePage(), max_scrolls=20, stall_rounds=2)
        # 3 -> 5 (new) -> 5 (stall 1) -> 5 (stall 2, stop)
        self.assertEqual(stats, {"rounds": 3, "new_ids": 0, "exit": "stall"})

    async def test_cap_respected_on_rich_feed(self):
        from naukri_agent.linkedin.scraper import LinkedInHunter

        state = {"n": 0}

        class FakeLocator:
            async def count(self):
                state["n"] += 1
                return state["n"]

        class FakeMouse:
            async def wheel(self, x, y):
                return None

        class FakePage:
            locator = lambda self, sel: FakeLocator()
            mouse = FakeMouse()

        hunter = LinkedInHunter(headless=True)
        with unittest.mock.patch("asyncio.sleep", return_value=None):
            stats = await hunter._human_scroll(FakePage(), max_scrolls=7, stall_rounds=5)
        self.assertEqual(stats, {"rounds": 7, "new_ids": 0, "exit": "cap"})

    async def test_dead_page_ends_gracefully(self):
        from naukri_agent.linkedin.scraper import LinkedInHunter

        class DeadPage:
            def locator(self, sel):
                raise RuntimeError("dead")

            class mouse:
                @staticmethod
                async def wheel(x, y):
                    raise RuntimeError("dead")

        hunter = LinkedInHunter(headless=True)
        stats = await hunter._human_scroll(DeadPage(), max_scrolls=20)
        self.assertEqual(stats, {"rounds": 0, "new_ids": 0, "exit": "dead"})


class TestGotoSearch(unittest.IsolatedAsyncioTestCase):
    async def test_retry_then_success(self):
        from naukri_agent.linkedin.scraper import LinkedInHunter

        calls = []

        class FakePage:
            async def goto(self, url, wait_until=None, timeout=None):
                calls.append(wait_until)
                if len(calls) == 1:
                    raise TimeoutError("Timeout 45000ms exceeded")
                return None

        self.assertTrue(await LinkedInHunter._goto_search(FakePage(), "https://x"))
        self.assertEqual(calls, ["domcontentloaded", "commit"])

    async def test_double_failure_returns_false(self):
        from naukri_agent.linkedin.scraper import LinkedInHunter

        class DeadPage:
            async def goto(self, *a, **k):
                raise TimeoutError("nope")

        self.assertFalse(await LinkedInHunter._goto_search(DeadPage(), "https://x"))


class TestFreshnessPriority(unittest.TestCase):
    def test_age_parsing(self):
        from naukri_agent.linkedin.analyzer import parse_post_age_hours
        self.assertEqual(parse_post_age_hours("4m • Follow"), 4 / 60)
        self.assertEqual(parse_post_age_hours("13h • Edited • Follow"), 13.0)
        self.assertEqual(parse_post_age_hours("2d • Follow"), 48.0)
        self.assertEqual(parse_post_age_hours("just now"), 0.0)
        self.assertIsNone(parse_post_age_hours("Follow"))

    def test_fresh_first_within_tier(self):
        from naukri_agent.linkedin.analyzer import prioritize_leads
        leads = [
            {"email": "old@acme.com", "post_age_hours": 20.0},
            {"email": "new@gmail.com", "post_age_hours": 1.0},
            {"email": "fresh@acme.com", "post_age_hours": 2.0},
        ]
        ordered = [lead["email"] for lead in prioritize_leads(leads)]
        self.assertEqual(ordered, ["fresh@acme.com", "old@acme.com", "new@gmail.com"])

    def test_unknown_age_sinks(self):
        from naukri_agent.linkedin.analyzer import prioritize_leads
        leads = [
            {"email": "x@acme.com"},
            {"email": "y@acme.com", "post_age_hours": 3.0},
        ]
        ordered = [lead["email"] for lead in prioritize_leads(leads)]
        self.assertEqual(ordered, ["y@acme.com", "x@acme.com"])


class TestOverlapMeasurement(unittest.TestCase):
    def test_hash_stable_and_insensitive(self):
        from naukri_agent.linkedin.analyzer import post_hash
        self.assertEqual(post_hash("Hiring  Python  Devs"), post_hash("hiring python devs"))
        self.assertNotEqual(post_hash("Hiring Python devs"), post_hash("Hiring Java devs"))

    def test_report_math(self):
        from naukri_agent.linkedin.analyzer import overlap_report
        seen = {
            "a": ["AI Engineer"],
            "b": ["AI Engineer", "GenAI Engineer"],
            "c": ["GenAI Engineer"],
        }
        rep = overlap_report(seen)
        self.assertEqual(rep["unique_posts"], 3)
        self.assertEqual(rep["total_reads"], 4)
        self.assertEqual(rep["duplicate_reads"], 1)
        self.assertEqual(rep["multi_keyword_posts"], 1)
        self.assertEqual(rep["novel_per_keyword"], {"AI Engineer": 1, "GenAI Engineer": 1})

    def test_empty(self):
        from naukri_agent.linkedin.analyzer import overlap_report
        rep = overlap_report({})
        self.assertEqual(rep["unique_posts"], 0)
        self.assertEqual(rep["duplicate_reads"], 0)


class TestAddressable(unittest.TestCase):
    def test_company_domain_always_ok(self):
        from naukri_agent.linkedin.analyzer import lead_is_addressable
        self.assertTrue(lead_is_addressable({"email": "hr@acme.com"}))

    def test_free_mail_needs_name_or_company(self):
        from naukri_agent.linkedin.analyzer import lead_is_addressable
        self.assertTrue(lead_is_addressable(
            {"email": "x@gmail.com", "first_name": "Ritu", "company": ""}))
        self.assertTrue(lead_is_addressable(
            {"email": "x@gmail.com", "first_name": "", "company": "Acme"}))
        self.assertFalse(lead_is_addressable(
            {"email": "hrritusingh32@gmail.com", "first_name": "", "company": ""}))


class TestPosterFallback(unittest.TestCase):
    def test_name_and_headline_from_snippet(self):
        from naukri_agent.linkedin.analyzer import extract_poster
        text = ("Feed post\n\nNaman Chandra\n\n \n • 3rd+\n\n"
                "Data Analyst and Helping team to hire OPT students")
        author, headline = extract_poster(text)
        self.assertEqual(author, "Naman Chandra")
        self.assertIn("Data Analyst", headline)

    def test_no_shape_no_guess(self):
        from naukri_agent.linkedin.analyzer import extract_poster
        self.assertEqual(extract_poster("Just some random post text here"), ("", ""))
        self.assertEqual(extract_poster(""), ("", ""))

    def test_skips_feed_post_label(self):
        from naukri_agent.linkedin.analyzer import extract_poster
        author, _ = extract_poster("Feed post\n\n • 1st\n\nHiring now")
        self.assertEqual(author, "")


class TestCampaignSources(unittest.TestCase):
    """Pin the campaign contract: feed posts ONLY (relevance inside the
    24h bound — billions of posts, best-first before scroll depth runs
    out). No jobs-tab source: the apply flow already covers those exact
    listings, so re-scraping them duplicated coverage and exposure."""

    def test_posts_relevance_with_24h_bound(self):
        from naukri_agent.linkedin.campaign import SEARCH_URLS
        self.assertTrue(SEARCH_URLS)
        for url in SEARCH_URLS:
            self.assertIn("relevance", url)
            self.assertIn("past-24h", url)
            self.assertIn("/search/results/content/", url)

    def test_no_jobs_source(self):
        import naukri_agent.linkedin.campaign as campaign_mod
        import naukri_agent.linkedin.scraper as scraper_mod
        self.assertFalse(hasattr(campaign_mod, "JOBS_URLS"))
        self.assertFalse(hasattr(scraper_mod.LinkedInHunter, "hunt_job_posts"))


class TestAuthorCompany(unittest.TestCase):
    def test_company_from_headline(self):
        self.assertEqual(
            extract_company("Hiring!", "Technical Recruiter at Infosys"), "Infosys")

    def test_company_skips_tech_words(self):
        self.assertEqual(extract_company("We are great at Python, join us", ""), "")

    def test_company_from_hiring_phrase(self):
        self.assertEqual(
            extract_company("We are hiring at Razorpay for backend roles", "Founder"), "Razorpay")

    def test_company_unknown_is_empty(self):
        self.assertEqual(extract_company("Hiring AI engineers, DM me", "Engineer"), "")

    def test_first_name(self):
        self.assertEqual(extract_first_name("Chandana Rao • 1st"), "Chandana")
        self.assertEqual(extract_first_name(""), "")
        self.assertEqual(extract_first_name("12345"), "")


class TestCompanyDomainMatch(unittest.TestCase):
    """Narrowed #2: careers@/jobs@ kept ONLY on company-domain match."""

    def test_direct_match(self):
        self.assertTrue(company_domain_match(
            "careers@acmecorp.com", "Acme Corp Pvt Ltd"))

    def test_abbreviation_match(self):
        self.assertTrue(company_domain_match(
            "jobs@tcs.com", "Tata Consultancy Services"))

    def test_mismatch_rejected(self):
        self.assertFalse(company_domain_match(
            "careers@randomplc.com", "Acme Corp Pvt Ltd"))

    def test_short_names_rejected(self):
        self.assertFalse(company_domain_match("careers@hr.com", "HR"))
        self.assertFalse(company_domain_match("jobs@x.io", ""))

    def test_malformed_safe(self):
        self.assertFalse(company_domain_match("not-an-email", "Acme"))
        self.assertFalse(company_domain_match("", ""))


class TestHumanScroll(unittest.IsolatedAsyncioTestCase):
    """Scroll depth: identity growth (not node counts) drives the stall."""

    def _loc(self, script, text=""):
        from unittest.mock import AsyncMock, MagicMock

        # NOTE: no copy — all locator instances share one backing list,
        # like successive reads against a single live DOM.
        loc = MagicMock()
        loc.count = AsyncMock(side_effect=lambda: script.pop(0) if len(script) > 1 else script[0])
        loc.inner_text = AsyncMock(return_value=text)
        loc.first = loc
        return loc

    def _page(self, count_script, body_text=""):
        from unittest.mock import AsyncMock, MagicMock

        page = MagicMock()
        page.locator = MagicMock(side_effect=lambda sel: (
            self._loc([1], body_text) if sel == "body"
            else self._loc(count_script)))
        page.mouse = MagicMock()
        page.mouse.wheel = AsyncMock()
        page.title = AsyncMock(return_value="Search")
        page.url = "https://www.linkedin.com/search/results/content/?keywords=x"
        return page

    def _hunt(self):
        from naukri_agent.linkedin.scraper import LinkedInHunter

        return LinkedInHunter

    async def _scroll(self, page, **kw):
        from unittest.mock import AsyncMock, patch

        import asyncio as _aio

        from naukri_agent.linkedin import scraper as sc_mod

        kw.setdefault("wall_check", None)
        with patch.object(sc_mod.asyncio, "sleep", new=AsyncMock()):
            return await self._hunt()._human_scroll(None, page, **kw)

    async def test_identity_drives_stall_despite_flat_counts(self):
        # Virtualized list: node count frozen at 10 while fresh posts stream.
        from unittest.mock import AsyncMock

        page = self._page([10] * 40)
        ids = [{"a"}, {"a", "b"}, {"a", "b"}, {"a", "b"}, {"a", "b"}, {"a", "b"}]
        stats = await self._scroll(
            page, identity_fn=AsyncMock(side_effect=ids), stall_rounds=3)
        self.assertEqual(stats["exit"], "stall")
        self.assertEqual(stats["rounds"], 4)
        self.assertEqual(stats["new_ids"], 1)

    async def test_legacy_count_path_unchanged(self):
        page = self._page([5, 8, 8, 8, 8, 8])
        stats = await self._scroll(page, stall_rounds=3)
        self.assertEqual(stats["exit"], "stall")
        self.assertEqual(stats["rounds"], 4)
        self.assertEqual(stats["new_ids"], 0)

    async def test_collapse_aborts_as_wall(self):
        page = self._page([20, 20, 2, 2])
        stats = await self._scroll(page, stall_rounds=5)
        self.assertEqual(stats["exit"], "wall")
        self.assertEqual(stats["rounds"], 2)

    async def test_end_marker_exits_cleanly(self):
        page = self._page([10, 12, 14], body_text="You have seen all new results")
        stats = await self._scroll(page, stall_rounds=5)
        self.assertEqual(stats["exit"], "end_marker")
        self.assertEqual(stats["rounds"], 1)

    async def test_end_marker_miss_falls_back_to_stall(self):
        page = self._page([10, 10, 10, 10], body_text="unrelated footer text")
        stats = await self._scroll(page, stall_rounds=3)
        self.assertEqual(stats["exit"], "stall")

    async def test_wall_check_aborts_immediately(self):
        from unittest.mock import AsyncMock

        page = self._page([10, 15, 20, 25])
        stats = await self._scroll(page, stall_rounds=5,
                                   wall_check=AsyncMock(return_value=True))
        self.assertEqual(stats["exit"], "wall")
        self.assertEqual(stats["rounds"], 1)

    async def test_dead_page_never_raises(self):
        from unittest.mock import MagicMock

        page = MagicMock()
        page.locator = MagicMock(side_effect=RuntimeError("closed"))
        page.mouse = MagicMock()
        stats = await self._scroll(page)
        self.assertEqual(stats["exit"], "dead")

    async def test_jitter_bounds(self):
        from unittest.mock import AsyncMock, patch

        import asyncio as _aio

        from naukri_agent.linkedin import scraper as sc_mod

        page = self._page([10, 10, 10, 10, 10])
        delays = []

        async def fake_sleep(d):
            delays.append(d)

        with patch.object(sc_mod.asyncio, "sleep", new=fake_sleep):
            await self._hunt()._human_scroll(None, page, stall_rounds=3)
        for call in page.mouse.wheel.await_args_list:
            self.assertGreaterEqual(call.args[1], 600)
            self.assertLessEqual(call.args[1], 1400)
        for d in delays:
            self.assertGreaterEqual(d, 1.8)
            self.assertLessEqual(d, 3.5)

    def test_return_shape(self):
        import asyncio

        from naukri_agent.linkedin.scraper import LinkedInHunter
        import unittest.mock as mock

        page = mock.MagicMock()
        page.locator = mock.MagicMock(side_effect=RuntimeError("closed"))
        page.mouse = mock.MagicMock()
        stats = asyncio.get_event_loop().run_until_complete(
            LinkedInHunter._human_scroll(None, page))
        self.assertEqual(set(stats), {"rounds", "new_ids", "exit"})


class TestObfuscatedEmails(unittest.TestCase):
    """Recruiters publish '[at]/[dot]' addresses to dodge scrapers —
    decoding them recovers leads the plain regex drops."""

    def test_bracket_form(self):
        from naukri_agent.linkedin.analyzer import extract_recruiter_emails

        self.assertEqual(
            extract_recruiter_emails("reach me at shivani [at] scoutforu [dot] com"),
            ["shivani@scoutforu.com"],
        )

    def test_plain_at_dot_words(self):
        from naukri_agent.linkedin.analyzer import extract_recruiter_emails

        self.assertEqual(
            extract_recruiter_emails("Mail your resume at jobs at acme dot io today"),
            ["jobs@acme.io"],
        )

    def test_at_only_form_never_decoded(self):
        # "apply at careers.google.com" is a website instruction — decoding
        # it would fabricate a mailbox that was never published.
        from naukri_agent.linkedin.analyzer import extract_recruiter_emails

        self.assertEqual(
            extract_recruiter_emails("apply at careers.google.com for this role"),
            [],
        )

    def test_plain_emails_unaffected(self):
        from naukri_agent.linkedin.analyzer import extract_recruiter_emails

        self.assertEqual(
            extract_recruiter_emails("write to jane.doe@acme.com now"),
            ["jane.doe@acme.com"],
        )

    def test_prose_not_an_address(self):
        from naukri_agent.linkedin.analyzer import extract_recruiter_emails

        for prose in [
            "meet me at 5 dot ballroom for coffee",
            "look at the store for details",
            "great role at a funded startup, DM me",
            "",
        ]:
            self.assertEqual(extract_recruiter_emails(prose), [], prose)

    def test_excluded_domains_still_excluded(self):
        from naukri_agent.linkedin.analyzer import extract_recruiter_emails

        self.assertEqual(
            extract_recruiter_emails("contact at test [at] example [dot] com"),
            [],
        )


if __name__ == "__main__":
    unittest.main()
