"""LinkedIn post analyzer: abroad-onsite filter + existing pins. No browser."""
import unittest
import unittest.mock

from naukri_agent.linkedin.analyzer import (
    classify_role,
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
        done = await hunter._human_scroll(FakePage(), max_scrolls=20, stall_rounds=2)
        # 3 -> 5 (new) -> 5 (stall 1) -> 5 (stall 2, stop)
        self.assertEqual(done, 3)

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
            done = await hunter._human_scroll(FakePage(), max_scrolls=7, stall_rounds=5)
        self.assertEqual(done, 7)

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
        done = await hunter._human_scroll(DeadPage(), max_scrolls=20)
        self.assertEqual(done, 0)


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


if __name__ == "__main__":
    unittest.main()
