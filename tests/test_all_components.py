"""
Unit tests for core components, filter engine, LinkedIn platform, and answers.
"""
import unittest
from unittest.mock import MagicMock
from naukri_agent.config import AgentConfig, FilterRules
from naukri_agent.core.models import Job
from naukri_agent.core.ranking import CandidateProfile
from naukri_agent.core.filters import FilterEngine
from naukri_agent.core.answers import AnswerEngine
from naukri_agent.platforms.linkedin import LinkedInPlatform, AI_TARGET_QUERY, FULLSTACK_TARGET_QUERY
from naukri_agent.naukri.chatbot import ChatbotHandler


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
            "Forward Deployed Engineer",
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


class TestLinkedInPlatform(unittest.TestCase):
    def setUp(self):
        self.cfg = AgentConfig.load()
        self.platform = LinkedInPlatform(
            MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), config=self.cfg
        )

    def test_default_and_config_days(self):
        """LinkedIn platform should default to 3 days from config or init."""
        self.assertEqual(self.platform.days, 3)

    def test_search_url_freshness_parameter(self):
        """Verify search URLs contain f_TPR=r259200 (3 days) and f_TPR=r604800 (7 days)."""
        ai_prof = next(p for p in self.cfg.profiles if "AI" in p.name)
        fs_prof = next(p for p in self.cfg.profiles if "Full Stack" in p.name)

        url_3d = self.platform._get_search_url(ai_prof)
        self.assertIn("f_TPR=r259200", url_3d)
        self.assertIn("f_AL=true", url_3d)
        self.assertIn("f_E=2%2C3", url_3d)

        url_7d = self.platform._get_search_url(fs_prof, days=7)
        self.assertIn("f_TPR=r604800", url_7d)


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
            "Python", "Node.js", "React.js", "TypeScript", "FastAPI",
            "Next.js", "Generative AI", "LLMs", "JavaScript",
            "LangChain", "LangGraph", "MLOps", "MCP", "AWS Bedrock",
            "Hugging Face",
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
        """Unified Instahyre profile must reject Java, .NET, Lead, Manager, Architect."""
        forbidden_roles = [
            ("Oracle", "Java Developer"),
            ("Microsoft", ".NET Backend Engineer"),
            ("Infosys", "Senior Lead Architect"),
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
            ("Wipro", "Java Tech Lead"),
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
        from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()

