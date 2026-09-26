"""
Unit tests for core components, filter engine, LinkedIn platform, and answers.
"""
import unittest
from unittest.mock import MagicMock

from naukri_agent.config import AgentConfig
from naukri_agent.core.filters import FilterEngine
from naukri_agent.core.models import Job
from naukri_agent.naukri.chatbot import ChatbotHandler
from naukri_agent.platforms.linkedin import (
    LinkedInPlatform,
)


class TestFilterEngine(unittest.TestCase):
    def setUp(self):
        self.cfg = AgentConfig.load()
        self.fs_profile = next(p for p in self.cfg.profiles if "Full Stack" in p.name)
        self.ai_profile = next(p for p in self.cfg.profiles if "AI" in p.name)

    def test_title_hyphen_normalization(self):
        """Verify that titles with spaces around hyphens match target rules."""
        engine = FilterEngine(self.fs_profile.filters_for("recommended"))
        job = Job(
            job_id="test-1",
            title="Full - Stack Engineer",
            company="Test Corp",
            location="Bengaluru",
            url="http://test.com",
            platform="instahyre",
        )
        decision = engine.evaluate_card(job)
        self.assertTrue(decision.passed, f"Failed with reason: {decision.reason}")

    def test_software_development_engineer_equivalence(self):
        """Verify SDE 1 and SDE 2 titles pass filter gates."""
        engine = FilterEngine(self.fs_profile.filters_for("recommended"))
        for title in [
            "Software Development Engineer 1",
            "Software Development Engineer 2",
            "Software Development Engineer in Test",
            "Quality Engineer",
            "Implementation Engineer",
        ]:

            job = Job(
                job_id=f"test-{title}",
                title=title,
                company="Amazon",
                location="Bengaluru",
                url="http://test.com",
                platform="instahyre",
            )
            decision = engine.evaluate_card(job)
            self.assertTrue(decision.passed, f"{title} failed with reason: {decision.reason}")

    def test_disjoint_specialization_blocks(self):
        """Verify that strictly non-relevant roles remain blocked."""
        engine = FilterEngine(self.fs_profile.filters_for("recommended"))
        for blocked_title in [
            "Data Scientist",
            "Fullstack Developer - .NET",
            "SDET II (ETL Testing)",
            "SDET - 1 (Java)",
            "Security Engineer I",
        ]:
            job = Job(
                job_id=f"test-blocked-{blocked_title}",
                title=blocked_title,
                company="Block Corp",
                location="Bengaluru",
                url="http://test.com",
                platform="instahyre",
            )
            decision = engine.evaluate_card(job)
            self.assertFalse(decision.passed, f"{blocked_title} should have been blocked!")

    def test_dotnet_family_blocks(self):
        """Dotnet-family frameworks (Blazor/Razor/ASP.NET) must hit the same
        block as '.NET' — run 377 applied to a Blazor senior role."""
        engine = FilterEngine(self.fs_profile.filters_for("recommended"))
        for blocked_title in [
            "Senior Software Engineer - Blazor",
            "Backend Developer (ASP.NET Core)",
            "Software Engineer - Razor Components",
        ]:
            job = Job(
                job_id=f"test-dotnet-{blocked_title}",
                title=blocked_title,
                company="Block Corp",
                location="Bengaluru",
                url="http://test.com",
                platform="naukri",
            )
            decision = engine.evaluate_card(job)
            self.assertFalse(decision.passed, f"{blocked_title} should have been blocked!")

    def test_universal_junior_family_cap(self):
        """QA/DevOps/support roles above 2y must fail on EVERY profile's rules.

        All platforms share FilterEngine, so the junior-family cap holds
        everywhere by construction (run 388: Manual QA applied only because
        its card carried no experience signal, never a rule gap).
        """
        for prof in (self.fs_profile, self.ai_profile):
            engine = FilterEngine(prof.filters_for("recommended"))
            job = Job(
                job_id="test-junior-cap",
                title="Manual QA Engineer",
                company="Test Corp",
                location="Bengaluru",
                url="http://test.com",
                platform="linkedin",
                min_experience=3.0,
                max_experience=5.0,
            )
            decision = engine.evaluate_card(job)
            self.assertFalse(
                decision.passed,
                f"{prof.name}: Manual QA at 3-5y must be rejected, got {decision.reason}",
            )

    def test_strategist_titles_blocked(self):
        """Strategy/advisory titles are not engineering even with an AI
        prefix (run 430 applied to an AI Activation Strategist)."""
        for prof in (self.fs_profile, self.ai_profile):
            engine = FilterEngine(prof.filters_for("recommended"))
            job = Job(
                job_id="test-strategist",
                title="AI Activation Strategist",
                company="Test Corp",
                location="Bengaluru",
                url="http://test.com",
                platform="naukri",
            )
            decision = engine.evaluate_card(job)
            self.assertFalse(
                decision.passed,
                f"{prof.name}: Strategist title must be rejected, got {decision.reason}",
            )


    def test_creative_roles_blocked(self):
        """Graphics/game/creator roles hire portfolios, not engineering tenure
        (run 391 applied to a 2d/3d senior role and an AI Artist posting)."""
        for prof in (self.fs_profile, self.ai_profile):
            engine = FilterEngine(prof.filters_for("recommended"))
            for blocked_title in [
                "Software Developer 2d/3d - Senior",
                "AI Artist",
                "Unity Developer",
            ]:
                job = Job(
                    job_id=f"test-creative-{blocked_title}",
                    title=blocked_title,
                    company="Block Corp",
                    location="Bengaluru",
                    url="http://test.com",
                    platform="naukri",
                )
                decision = engine.evaluate_card(job)
                self.assertFalse(
                    decision.passed,
                    f"{prof.name}: {blocked_title} should have been blocked!",
                )


class TestDescriptionMetadata(unittest.TestCase):
    def test_generic_intake_inboxes_ignored(self):
        """hrintern/careers/jobs inboxes never convert; named recruiters pass."""
        from naukri_agent.core.models import extract_description_metadata

        links, emails = extract_description_metadata(
            "Apply now. Contact hrintern@corp.com or careers@corp.com or "
            "jobs@corp.com. Recruiter: pooja.roy@esolglobal.com"
        )
        self.assertEqual(emails, ["pooja.roy@esolglobal.com"])


class TestRunSummary(unittest.TestCase):
    def test_platform_only_breakdown_with_forensics(self):
        """Summary shows per-platform (never per-profile) lines carrying
        scraped + plan-rejected counts, so a 0/0/0 platform is self-explanatory."""
        from naukri_agent.core.models import RunStats
        from naukri_agent.notify.notifier import format_run_summary

        stats = RunStats()
        stats.bump("AI / Python Engineer", "scraped", 10, platform="naukri")
        stats.bump("AI / Python Engineer", "plan_rejected", 6, platform="naukri")
        stats.bump("AI / Python Engineer", "applied", 3, platform="naukri")
        body = format_run_summary(stats, duration_s=60, run_id=1)
        self.assertNotIn("PER-PROFILE", body)
        self.assertIn("PER-PLATFORM", body)
        self.assertIn("10 scraped", body)
        self.assertIn("6 plan-rejected", body)


class TestLinkedInPlatform(unittest.TestCase):
    def setUp(self):
        self.cfg = AgentConfig.load()
        self.platform = LinkedInPlatform(
            MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), config=self.cfg
        )

    def test_default_and_config_days(self):
        """LinkedIn platform should default to 1 day (24h freshness) from config."""
        self.assertEqual(self.platform.days, 1)

    def test_search_url_freshness_parameter(self):
        """Verify search URLs contain f_TPR=r86400 (1 day default) and f_TPR=r604800 (7 days)."""
        ai_prof = next(p for p in self.cfg.profiles if "AI" in p.name)
        fs_prof = next(p for p in self.cfg.profiles if "Full Stack" in p.name)

        url_1d = self.platform._get_search_url(ai_prof)
        self.assertIn("f_TPR=r86400", url_1d)
        self.assertIn("f_AL=true", url_1d)
        self.assertIn("f_E=2%2C3", url_1d)

        url_7d = self.platform._get_search_url(fs_prof, days=7)
        self.assertIn("f_TPR=r604800", url_7d)

    def test_suitability_requires_stack_overlap(self):
        """A bare 'Software Engineer' title with zero candidate-stack overlap
        must be rejected even though the role name matches (run 377 filler)."""
        from naukri_agent.core.models import Job

        bare = Job(
            job_id="test-bare-swe",
            title="Software Engineer",
            company="Generic Corp",
            url="https://www.linkedin.com/jobs/view/1/",
            location="India (Remote)",
            description="We are looking for a great engineer to join our team.",
            platform="linkedin",
        )
        suitable, _, _ = self.platform.evaluate_job_suitability(bare)
        self.assertFalse(suitable, "stack-empty generic title should be rejected")

        stacked = Job(
            job_id="test-stacked-swe",
            title="Software Engineer",
            company="Stack Corp",
            url="https://www.linkedin.com/jobs/view/2/",
            location="India (Remote)",
            description="Build backend services with Python, FastAPI and PostgreSQL. REST APIs on AWS.",
            platform="linkedin",
        )
        suitable, _, score = self.platform.evaluate_job_suitability(stacked)
        self.assertTrue(suitable, "stack-matching title should pass")
        self.assertGreaterEqual(score, 60)


class TestNaukriChatbotFuzzyMatching(unittest.TestCase):
    def test_match_option_text_affirmative(self):
        """Affirmative choices should match 'yes', 'agree', 'willing'."""
        self.assertTrue(ChatbotHandler._match_option_text("Yes, I am willing to relocate", "yes"))
        self.assertTrue(ChatbotHandler._match_option_text("Comfortable with Bengaluru", "yes"))
        self.assertFalse(ChatbotHandler._match_option_text("No, not interested", "yes"))

    def test_match_option_text_city(self):
        """City options should match Bengaluru / Bangalore fuzzy."""
        self.assertTrue(ChatbotHandler._match_option_text("Bengaluru / Bangalore", "Bengaluru"))
        self.assertTrue(ChatbotHandler._match_option_text("Bangalore / Bengaluru", "Bengaluru"))
        self.assertFalse(ChatbotHandler._match_option_text("Delhi / NCR", "Bengaluru"))


class TestInstahyreUnifiedConfiguration(unittest.TestCase):
    def setUp(self):
        self.cfg = AgentConfig.load()
        self.unified_profile = self.cfg.get_unified_instahyre_profile()
        self.engine = FilterEngine(self.unified_profile.filters)

    def test_instahyre_search_skills_and_exp(self):
        """Instahyre configuration must contain the exact 15 skills and 2 years experience."""
        expected_skills = [
            "SDET", "Python", "Node.js", "React.js", "TypeScript",
            "FastAPI", "Next.js", "Generative AI", "JavaScript",
            "LangChain", "LangGraph", "MLOps", "AWS Bedrock",
            "API Testing", "Quality Assurance",
        ]
        self.assertEqual(self.cfg.instahyre.skills, expected_skills)
        self.assertEqual(self.cfg.instahyre.experience_years, 2)

    def test_unified_profile_accepts_both_ai_and_fullstack(self):
        """Unified Instahyre profile must accept both AI and Full Stack roles without cross-profile rejection."""
        roles = [
            ("KuKu FM", "AI Engineer"),
            ("Swiggy", "React.js Developer"),
            ("Impact Analytics", "Node.js Developer"),
            ("Tranzact", "Full Stack Developer - Python / Django / React.js"),
            ("Amazon", "Software Development Engineer 2"),
            ("Quince", "SDET II"),
            ("Gravity", "Lead React/Next.js Developer"),
        ]
        for company, title in roles:
            job = Job(
                job_id=f"test-{company}-{title}",
                title=title,
                company=company,
                location="Bengaluru",
                url="https://www.instahyre.com/candidate/opportunities/",
                platform="instahyre",
            )
            decision = self.engine.evaluate_card(job)
            self.assertTrue(decision.passed, f"Job {company} - {title} failed: {decision.reason}")

    def test_unified_profile_rejects_forbidden_roles(self):
        """Unified Instahyre profile must reject Java, .NET, Manager, Architect (lead is allowed)."""
        forbidden_roles = [
            ("Oracle", "Java Developer"),
            ("Microsoft", ".NET Backend Engineer"),
            ("Infosys", "Senior Architect"),
            ("Accenture", "Engineering Manager"),
            ("TCS", "BPO Process Associate"),
        ]
        for company, title in forbidden_roles:
            job = Job(
                job_id=f"test-{company}-{title}",
                title=title,
                company=company,
                location="Bengaluru",
                url="https://www.instahyre.com/candidate/opportunities/",
                platform="instahyre",
            )
            decision = self.engine.evaluate_card(job)
            self.assertFalse(decision.passed, f"Job {company} - {title} should have been rejected!")

    def test_unified_profile_allows_devops_cloud_support_desktop(self):
        """Unified Instahyre profile must allow DevOps, Cloud, Support, and Desktop Support roles."""
        allowed_roles = [
            ("Deutsche Bank", "DevOps Engineer"),
            ("Razorpay", "Cloud Engineer"),
            ("Manifest", "Technical Support Engineer"),
            ("Zendesk", "Support Engineer"),
            ("Wipro", "Desktop Support Engineer"),
        ]
        for company, title in allowed_roles:
            job = Job(
                job_id=f"test-{company}-{title}",
                title=title,
                company=company,
                location="Bengaluru",
                url="https://www.instahyre.com/candidate/opportunities/",
                platform="instahyre",
            )
            decision = self.engine.evaluate_card(job)
            self.assertTrue(decision.passed, f"Job {company} - {title} failed: {decision.reason}")

    def test_unified_profile_rejects_mobile_sre_phd(self):
        """Unified Instahyre profile must reject Mobile (React Native/Android/iOS/Flutter), pure SRE, and PhD."""
        forbidden_roles = [
            ("Bhanzu", "App Developer - React Native"),
            ("PhonePe", "Android Developer"),
            ("CRED", "iOS Engineer"),
            ("Groww", "Flutter Developer"),
            ("Uber", "Site Reliability Engineer"),
            ("Google", "SRE"),
            ("Microsoft Research", "PhD Research Scientist"),
        ]
        for company, title in forbidden_roles:
            job = Job(
                job_id=f"test-{company}-{title}",
                title=title,
                company=company,
                location="Bengaluru",
                url="https://www.instahyre.com/candidate/opportunities/",
                platform="instahyre",
            )
            decision = self.engine.evaluate_card(job)
            self.assertFalse(decision.passed, f"Job {company} - {title} should have been rejected!")


class TestCutshortUnifiedConfiguration(unittest.TestCase):
    def setUp(self):
        self.cfg = AgentConfig.load()
        self.unified_profile = self.cfg.get_unified_cutshort_profile()
        self.engine = FilterEngine(self.unified_profile.filters)

    def test_cutshort_skills_constants(self):
        """Verify Cutshort divided skills lists contain verified autocomplete targets."""
        from naukri_agent.platforms.cutshort import (
            CUTSHORT_AI_SKILLS,
            CUTSHORT_FULLSTACK_SKILLS,
            CUTSHORT_UNIFIED_SKILLS,
        )
        # AI Skills
        ai_exact = [s[1] if isinstance(s, tuple) else s for s in CUTSHORT_AI_SKILLS]
        for expected in [
            "Python", "Generative AI", "Agentic AI", "Large Language Models (LLM)",
            "Retrieval Augmented Generation (RAG)", "Artificial Intelligence (AI)",
            "MLOps", "Large Language Models (LLM) tuning", "AWS Bedrock", "FastAPI",
            "LangGraph", "Prompt engineering", "NodeJS (Node.js)", "TypeScript", "Javascript"
        ]:
            self.assertIn(expected, ai_exact)

        # Full Stack Skills
        fs_exact = [s[1] if isinstance(s, tuple) else s for s in CUTSHORT_FULLSTACK_SKILLS]
        for expected in [
            "React.js", "NextJs (Next.js)", "Javascript", "NodeJS (Node.js)",
            "TypeScript", "Python", "FastAPI"
        ]:
            self.assertIn(expected, fs_exact)

        # Unified Skills
        unified_exact = [s[1] if isinstance(s, tuple) else s for s in CUTSHORT_UNIFIED_SKILLS]
        for expected in [
            "Python", "React.js", "NextJs (Next.js)", "Generative AI", "Javascript",
            "NodeJS (Node.js)", "Agentic AI", "Large Language Models (LLM)", "TypeScript",
            "FastAPI", "Retrieval Augmented Generation (RAG)", "Artificial Intelligence (AI)",
            "MLOps", "Large Language Models (LLM) tuning", "AWS Bedrock", "LangGraph",
            "Prompt engineering"
        ]:
            self.assertIn(expected, unified_exact)


    def test_cutshort_unified_accepts_roles(self):
        """Verify unified profile accepts both AI and Full Stack roles gathered on Cutshort."""
        roles = [
            ("Techno Wise", "Python Full Stack Developer"),
            ("Shuling technology", "Senior Node.js Backend Engineer"),
            ("ChicMic Studios", "React Js Developer"),
            ("BigThinkCode Technologies", "Junior Node.js Developer"),
            ("TopGrep Tech Private Limi", "FS MERN Developer"),
            ("CLOUDSUFI", "CloudSufi is Hiring! SSE-AI Full Stack"),
            ("IntelliSavvy", "Fullstack Developer"),
            ("Gravity Engineering Services Pvt Ltd", "Lead React/Next.js Developer"),
        ]
        for company, title in roles:
            job = Job(
                job_id=f"test-cutshort-{company}-{title}",
                title=title,
                company=company,
                location="Bengaluru",
                url="https://cutshort.io/profile/all-jobs",
                platform="cutshort",
            )
            decision = self.engine.evaluate_card(job)
            self.assertTrue(decision.passed, f"Job {company} - {title} failed: {decision.reason}")

    def test_cutshort_unified_rejects_presales_and_forbidden(self):
        """Verify Cutshort unified profile rejects presales, pre-sales, bpo, java, .net."""
        forbidden = [
            ("SaaSify", "Pre-Sales Consultant"),
            ("InfraSys", "Presales Solution Specialist"),
            ("Wipro", "Java Developer"),
            ("TechM", ".NET Core Backend Developer"),
            ("Accenture", "Sales Manager"),
        ]
        for company, title in forbidden:
            job = Job(
                job_id=f"test-cutshort-{company}-{title}",
                title=title,
                company=company,
                location="Bengaluru",
                url="https://cutshort.io/profile/all-jobs",
                platform="cutshort",
            )
            decision = self.engine.evaluate_card(job)
            self.assertFalse(decision.passed, f"Job {company} - {title} should have been rejected!")

    def test_dynamic_resume_selection_logic(self):
        """Verify resume selection maps AI jobs to AI CV and Full Stack to Full Stack CV."""
        from naukri_agent.config import PROJECT_ROOT

        ai_titles = [
            "AI Engineer",
            "Generative AI Specialist",
            "LLM Application Developer",
            "Agentic AI Engineer",
            "Python Backend / FastAPI Engineer",
        ]
        for title in ai_titles:
            text = f"{title}".lower()
            is_ai = any(k in text for k in ["ai", "llm", "genai", "gpt", "machine learning", "ml", "rag", "agent", "prompt", "nlp", "chatbot", "fastapi"])
            self.assertTrue(is_ai, f"{title} should be recognized as AI job")
            rel_path = "resumes/CV_Mahesh_Chitakoti_2026.pdf" if is_ai else "resumes/CV_Mahesh_Chitakoti_2026_1_.pdf"
            self.assertEqual(rel_path, "resumes/CV_Mahesh_Chitakoti_2026.pdf")
            self.assertTrue((PROJECT_ROOT / rel_path).exists())

        fs_titles = [
            "React Js Developer",
            "Fullstack Developer",
            "Node.js Backend Engineer",
            "Frontend Web Developer",
            "MERN Stack Developer",
        ]
        for title in fs_titles:
            text = f"{title}".lower()
            is_ai = any(k in text for k in ["ai", "llm", "genai", "gpt", "machine learning", "ml", "rag", "agent", "prompt", "nlp", "chatbot", "fastapi"])
            self.assertFalse(is_ai, f"{title} should be recognized as Full Stack job")
            rel_path = "resumes/CV_Mahesh_Chitakoti_2026.pdf" if is_ai else "resumes/CV_Mahesh_Chitakoti_2026_1_.pdf"
            self.assertEqual(rel_path, "resumes/CV_Mahesh_Chitakoti_2026_1_.pdf")
            self.assertTrue((PROJECT_ROOT / rel_path).exists())

    def test_linkedin_unified_profile(self):
        """Verify LinkedIn unified profile combines AI and Full Stack queries and filters."""
        from unittest.mock import MagicMock

        from naukri_agent.platforms.linkedin import LinkedInPlatform

        p = self.cfg.get_unified_linkedin_profile()
        self.assertEqual(p.name, "LinkedIn Unified (AI & Full Stack)")
        self.assertEqual(p.account, "primary")
        self.assertEqual(p.platform_limits.get("linkedin"), 35)

        # Mock platform to test _get_search_url query selection
        platform = LinkedInPlatform(MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), config=self.cfg)
        search_url = platform._get_search_url(p)
        self.assertIn("Full%20Stack%20Developer", search_url)
        self.assertIn("AI%20Engineer", search_url)

    def test_wellfound_unified_profile(self):
        """Verify Wellfound unified profile enforces 7-day freshness and combined filters."""
        from naukri_agent.platforms.wellfound import MAX_POSTED_DAYS, _parse_posted_days

        p = self.cfg.get_unified_wellfound_profile()
        self.assertEqual(p.name, "Wellfound Unified (AI & Full Stack)")
        self.assertEqual(p.account, "primary")
        self.assertEqual(p.platform_limits.get("wellfound"), 20)
        self.assertEqual(p.filters.max_posted_days, 7)
        self.assertEqual(MAX_POSTED_DAYS, 7)

        # Freshness parsing checks
        self.assertEqual(_parse_posted_days("Posted 2d ago"), 2)
        self.assertEqual(_parse_posted_days("Active 1w ago"), 7)
        self.assertEqual(_parse_posted_days("just now"), 0)
        self.assertEqual(_parse_posted_days("Posted 2 weeks ago"), 14)
        self.assertGreater(_parse_posted_days("Posted 2 weeks ago"), MAX_POSTED_DAYS)


class TestGeminiBreaker(unittest.TestCase):
    def _http_503(self, *args, **kwargs):
        import urllib.error

        raise urllib.error.HTTPError(
            "https://generativelanguage.googleapis.com/", 503,
            "Service Unavailable", {}, None,
        )

    def test_three_503_trips_breaker(self):
        """Three straight 503s must trip the breaker: the 4th call returns
        None without touching the network (run 398 burned ~20s per job)."""
        from unittest.mock import patch

        from naukri_agent.core.gemini_writer import GeminiWriter

        writer = GeminiWriter(api_key="test-key", model="test-model")
        with patch("urllib.request.urlopen", side_effect=self._http_503) as mock_open:
            for _ in range(3):
                self.assertIsNone(writer.generate_email_body("Dev", "desc", "Co"))
            self.assertTrue(writer._model_broken)
            self.assertIsNone(writer.generate_email_body("Dev", "desc", "Co"))
            self.assertEqual(mock_open.call_count, 3)

    def test_success_resets_503_count(self):
        """A success between failures must reset the consecutive count."""
        import io
        import json
        from unittest.mock import patch

        from naukri_agent.core.gemini_writer import GeminiWriter

        body = json.dumps(
            {"candidates": [{"content": {"parts": [{"text": "Hello world pitch text here"}]}}]}
        ).encode()
        writer = GeminiWriter(api_key="test-key", model="test-model")
        with patch("urllib.request.urlopen", side_effect=self._http_503):
            writer.generate_email_body("Dev", "desc", "Co")
            writer.generate_email_body("Dev", "desc", "Co")
        self.assertEqual(writer._server_errors, 2)
        self.assertFalse(writer._model_broken)

        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = body
        resp.__enter__.return_value = resp
        with patch("urllib.request.urlopen", return_value=resp):
            out = writer.generate_email_body("Dev", "desc", "Co")
        self.assertTrue(out)
        self.assertEqual(writer._server_errors, 0)


class TestMailerTrackRouting(unittest.TestCase):
    def test_ai_track_word_boundaries(self):
        """AI-track routing must use word boundaries: Retail/Training are
        full-stack roles, not AI roles. Gemini forced off so the template
        routing itself is pinned."""
        from unittest.mock import patch

        from naukri_agent.core.mailer import ColdEmailer

        mailer = ColdEmailer(sender_email="a@b.com", app_password="x")
        with patch(
            "naukri_agent.core.gemini_writer.GeminiWriter.generate_email_body",
            return_value=None,
        ):
            self.assertIn("Python, FastAPI", mailer._generate_body("AI Engineer", "", "Co"))
            self.assertIn("Python, FastAPI", mailer._generate_body("ML Engineer", "", "Co"))
            self.assertIn("React, Node.js", mailer._generate_body("Retail Associate", "", "Co"))
            self.assertIn("React, Node.js", mailer._generate_body("Training Manager", "", "Co"))
            self.assertIn("React, Node.js", mailer._generate_body("Full Stack Developer", "", "Co"))


class TestExperienceGates(unittest.TestCase):
    def _engine(self, profile_name="AI / Python Engineer", years=2.5):
        from naukri_agent.core.ranking import CandidateProfile

        cfg = AgentConfig.load()
        prof = next(p for p in cfg.profiles if profile_name in p.name)
        cand = CandidateProfile(
            title_keywords=[], core_skills=[], secondary_skills=[],
            target_experience_years=years,
        )
        return FilterEngine(prof.filters_for("recommended"), candidate=cand)

    def _job(self, title, company="C", min_exp=None, max_exp=None):
        return Job(
            job_id="t-%s" % title[:10], title=title, company=company,
            location="Bengaluru", url="http://t", platform="naukri",
            min_experience=min_exp, max_experience=max_exp,
        )

    def test_fresher_only_band_rejected(self):
        """0-0 fresher requisitions reject at 2y+ tenure (run 424)."""
        engine = self._engine()
        d = engine.evaluate_card(self._job("AI Engineer", min_exp=0.0, max_exp=0.0))
        self.assertFalse(d.passed)
        self.assertEqual(d.reason.value, "filter_experience")

    def test_narrow_bands_stay_eligible(self):
        """0-1/0-2 bands stay eligible — only pure 0-0 is cut."""
        engine = self._engine()
        self.assertTrue(engine.evaluate_card(self._job("AI Engineer", min_exp=0.0, max_exp=1.0)).passed)
        self.assertTrue(engine.evaluate_card(self._job("AI Engineer", min_exp=0.0, max_exp=2.0)).passed)

    def test_bare_net_blocked_tech_adjacent(self):
        """'TypeScript NET' is .NET (run 391 Neu Edge miss)."""
        engine = self._engine("Full Stack / Web Developer", years=3.0)
        d = engine.evaluate_card(self._job("Full Stack Engineer React Angular TypeScript NET"))
        self.assertFalse(d.passed)

    def test_net_company_not_blocked(self):
        """A company literally named 'Net Solutions' must not trip the
        dotnet company gate — the NET rule is tech-adjacent only."""
        engine = self._engine("Full Stack / Web Developer", years=3.0)
        d = engine.evaluate_card(self._job("Software Engineer", company="Net Solutions"))
        self.assertTrue(d.passed, f"wrongly blocked: {d.detail}")

    def test_impute_seniority(self):
        """Missing exp fills from title markers for every platform (run 424:
        SDE-4/Sr Lead applied with no stated range)."""
        from naukri_agent.core.filters import impute_seniority

        j = self._job("SDE 4 - Backend")
        self.assertTrue(impute_seniority(j))
        self.assertEqual((j.min_experience, j.max_experience), (5.0, 10.0))
        self.assertTrue(j.experience_imputed)

        stated = self._job("SDE 4 - Backend", min_exp=2.0, max_exp=7.0)
        self.assertFalse(impute_seniority(stated))

        junior_band = self._job("SDE 2 - Backend")
        self.assertFalse(impute_seniority(junior_band))

        roman = self._job("SDE - III @ Arintra")
        self.assertTrue(impute_seniority(roman))

        plain = self._job("Software Engineer")
        self.assertFalse(impute_seniority(plain))
        self.assertIsNone(plain.min_experience)

    def test_imputed_senior_rejected_by_ceiling(self):
        """Imputed 5-10y seniority must then fail the 3.5y ceiling gate."""
        from naukri_agent.core.filters import impute_seniority

        engine = self._engine()
        j = self._job("Sr. Lead AI Engineer")
        self.assertTrue(impute_seniority(j))
        d = engine.evaluate_card(j)
        self.assertFalse(d.passed)


if __name__ == "__main__":
    unittest.main()



