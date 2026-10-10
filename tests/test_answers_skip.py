"""
Skip-only option sets (run 469 reviews #31/#34): a radio whose only option
is "Skip this question" must resolve to Skip, not queue a human review.
No browser or DB needed.
"""
import unittest

from naukri_agent.core.answers import AnswerEngine, ExperienceAnswers
from naukri_agent.core.models import ScreeningQuestion


def _engine() -> AnswerEngine:
    return AnswerEngine(
        kb=[],
        profile_answers={},
        strict=True,
        experience=ExperienceAnswers(
            total_years=3.0,
            default_years=2.0,
            multi_skill_strategy="max",
            skills={"full stack": 3.0, "python": 2.0, "django": 2.0,
                    "fastapi": 2.0, "java": 0.0, "dsa": 3.0,
                    "data structures": 3.0, "algorithms": 3.0,
                    "langgraph": 2.0, "rag": 2.0, "llm": 2.0},
        ),
    )


class TestSkipOnlyOptions(unittest.TestCase):
    def test_tenure_question_with_only_skip_resolves(self):
        """Exact shape of review #34."""
        q = ScreeningQuestion(
            text="How many year of experience you have as Full Stack Developer?",
            kind="radio",
            options=["Skip this question"],
        )
        res = _engine().resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Skip this question")
        self.assertEqual(res.source, "option-match-skip")

    def test_real_options_still_win_over_skip(self):
        q = ScreeningQuestion(
            text="How many years of experience do you have in Python?",
            kind="radio",
            options=["0-1 years", "2-3 years", "Skip this question"],
        )
        res = _engine().resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "2-3 years")

    def test_none_of_the_above_is_not_skip(self):
        """'None of the above' asserts tenure facts — never auto-picked: the
        unlisted-skill 1y default fits the real range instead."""
        q = ScreeningQuestion(
            text="How many years of experience do you have in Cobol?",
            kind="radio",
            options=["0-1 years", "None of the above"],
        )
        res = _engine().resolve(q)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "0-1 years")


class TestTenureFramings(unittest.TestCase):
    """Sweep 2026-09-30: recruiter reframings must resolve from the map —
    no per-phrasing config entries needed."""

    def _resolve(self, text, options=None):
        return _engine().resolve(ScreeningQuestion(
            text=text, kind="radio", options=options or []))

    def test_how_long_worked_known_skill(self):
        res = self._resolve("How long have you worked with FastAPI?")
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "2")

    def test_how_long_worked_unknown_skill_reviews(self):
        res = self._resolve("How long have you worked with Cobol?")
        self.assertIsNone(res)

    def test_number_of_years_framing(self):
        res = self._resolve("Number of years working as a Python developer?")
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "2")

    def test_experienced_in_known_skill_yes(self):
        res = self._resolve("Are you experienced in Django?", ["Yes", "No"])
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Yes")

    def test_experienced_in_unknown_skill_reviews(self):
        res = self._resolve("Are you experienced in Cobol?", ["Yes", "No"])
        self.assertIsNone(res)

    def test_experienced_in_zero_skill_no(self):
        res = self._resolve("Are you experienced in Java?", ["Yes", "No"])
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "No")

    def test_bare_years_unknown_skill_reviews(self):
        """No experience-word + unknown skill: never inherit a default."""
        res = self._resolve("3 years with Cobol?")
        self.assertIsNone(res)

    def test_built_langgraph_yes(self):
        """Run 504 shape: built/deployed framings resolve from the map."""
        res = _engine().resolve(ScreeningQuestion(
            text="Have you built and deployed multi-step AI agent workflows using LangGraph in production?",
            kind="radio", options=["Yes", "No"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Yes")

    def test_built_unknown_skill_reviews(self):
        res = _engine().resolve(ScreeningQuestion(
            text="Have you built data pipelines with Cobol?",
            kind="radio", options=["Yes", "No"]))
        self.assertIsNone(res)

    def test_built_zero_skill_no(self):
        res = _engine().resolve(ScreeningQuestion(
            text="Have you deployed Java microservices?",
            kind="radio", options=["Yes", "No"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "No")


class TestTenureShortfall(unittest.TestCase):
    """Review #38: 3y vs options 4/4+ — no honest selection exists, so the
    JOB skips instead of queueing a human to pick a lie."""

    def _engine(self):
        return AnswerEngine(
            kb=[],
            profile_answers={"relevant experience": "3"},
            strict=True,
            experience=ExperienceAnswers(
                total_years=3.0, default_years=2.0,
                skills={"full stack": 3.0},
            ),
        )

    def _q(self, text, options):
        from naukri_agent.core.models import ScreeningQuestion
        return ScreeningQuestion(text=text, kind="radio", options=options)

    def test_shortfall_detected(self):
        eng = self._engine()
        detail = eng.tenure_shortfall(self._q(
            "How many year of experience you have as Full Stack Developer?",
            ["4 Yrs", "4+ Yrs"]))
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertIn("4", detail)

    def test_no_shortfall_when_option_fits(self):
        eng = self._engine()
        self.assertIsNone(eng.tenure_shortfall(self._q(
            "How many years of experience in Full Stack?",
            ["0-1 years", "2-4 years"])))

    def test_no_shortfall_without_bounds(self):
        eng = self._engine()
        self.assertIsNone(eng.tenure_shortfall(self._q(
            "How many years of experience in Full Stack?", ["Fresher", "Experienced"])))

    def test_no_shortfall_with_skip_option(self):
        eng = self._engine()
        self.assertIsNone(eng.tenure_shortfall(self._q(
            "How many years of experience in Full Stack?",
            ["4 Yrs", "Skip this question"])))

    def test_no_shortfall_overqualified(self):
        eng = self._engine()
        self.assertIsNone(eng.tenure_shortfall(self._q(
            "How many years of experience in Full Stack?", ["0-1", "1-2"])))

    def test_chatbot_result_has_unfit(self):
        from naukri_agent.naukri.chatbot import ChatbotResult
        self.assertIsNone(ChatbotResult().unfit)

    def test_motivation_names_company_no_invention(self):
        res = _engine().resolve(ScreeningQuestion(
            text="What interests you about this opportunity at Vena Solutions?",
            kind="text", options=[]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("Vena Solutions", res.value)
        self.assertIn("3", res.value)
        self.assertNotIn("fintech", res.value.lower())

    def test_motivation_without_company(self):
        res = _engine().resolve(ScreeningQuestion(
            text="Why are you a good fit for this role?",
            kind="text", options=[]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertNotIn("  ", res.value)

    def test_motivation_skips_job_change(self):
        res = _engine().resolve(ScreeningQuestion(
            text="Why are you looking for a change?", kind="text", options=[]))
        self.assertIsNone(res)

    def test_motivation_skips_learning_question(self):
        res = _engine().resolve(ScreeningQuestion(
            text="Why do you want to learn Python?", kind="text", options=[]))
        self.assertIsNone(res)

    def test_serving_status_yes_not_duration_no(self):
        """'Are you SERVING …' is status (Yes), never duration-derived No."""
        eng = AnswerEngine(
            kb=[], profile_answers={"serving notice": "Yes", "notice period": "0 days"},
            strict=True, experience=ExperienceAnswers(total_years=3.0, default_years=2.0),
        )
        res = eng.resolve(ScreeningQuestion(
            text="Are you serving your notice period?",
            kind="radio", options=["Yes", "No"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Yes")

    def test_notice_duration_dropdown_immediate(self):
        eng = AnswerEngine(
            kb=[], profile_answers={"serving notice": "Yes", "notice period": "0 days"},
            strict=True, experience=ExperienceAnswers(total_years=3.0, default_years=2.0),
        )
        res = eng.resolve(ScreeningQuestion(
            text="What is your current notice period?", kind="dropdown",
            options=["Immediate Joiner", "15 Days", "30 Days"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Immediate Joiner")

    def test_named_role_tenure_reviews(self):
        """SRE-style named roles with no map hit: review, never a default."""
        res = _engine().resolve(ScreeningQuestion(
            text="How many years of experience do you have as a Site Reliability Engineer/Developer?",
            kind="radio", options=[]))
        self.assertIsNone(res)

    def test_non_home_prefers_relocate_option(self):
        """Delhi job offering both claims: relocate wins (run 480)."""
        res = _engine().resolve(ScreeningQuestion(
            text="The location of this job will be Delhi. Are you okay with this?",
            kind="radio",
            options=["I am currently in this location and okay with it",
                     "I am willing to relocate to this location"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("relocate", res.value.lower())

    def test_home_keeps_current_location(self):
        res = _engine().resolve(ScreeningQuestion(
            text="The location of this job will be Bengaluru. Are you okay with this?",
            kind="radio",
            options=["I am currently in this location and okay with it",
                     "I am willing to relocate to this location"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("currently in this location", res.value.lower())

    def test_non_home_no_relocate_falls_back(self):
        """No relocate option: here-claim still carries the (true) Yes."""
        res = _engine().resolve(ScreeningQuestion(
            text="The location of this job will be Delhi. Are you okay with this?",
            kind="radio",
            options=["I am currently in this location and okay with it", "No"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("currently in this location", res.value.lower())

    def test_location_pick_home_city(self):
        """'Which of these locations' with home present: pick home."""
        res = _engine().resolve(ScreeningQuestion(
            text="Which of these locations are you willing to relocate to?",
            kind="checkbox",
            options=["Bengaluru", "Mumbai", "Delhi", "Pune", "No"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Bengaluru")

    def test_location_pick_relocate_city(self):
        """No home city: first non-negative city (relocate-willing policy)."""
        res = _engine().resolve(ScreeningQuestion(
            text="Which of these locations are you willing to relocate to?",
            kind="checkbox",
            options=["Mumbai", "Delhi", "Pune"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Mumbai")

    def test_take_up_interview_yes(self):
        """Mined gap: 'take up an interview' never reaches review."""
        res = _engine().resolve(ScreeningQuestion(
            text="Are you available to take up an interview(virtual)?",
            kind="radio", options=["Yes", "No", "Skip this question"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Yes")


class TestFluencyAndRating(unittest.TestCase):
    """Mined gaps: fluency questions and 1-10 self-ratings."""

    def test_fluent_known_skill_yes(self):
        res = _engine().resolve(ScreeningQuestion(
            text="Are you fluent in Python Programming?",
            kind="radio", options=["Yes", "No", "Skip this question"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "Yes")

    def test_fluent_zero_skill_no(self):
        res = _engine().resolve(ScreeningQuestion(
            text="Are you fluent in Java?", kind="radio",
            options=["Yes", "No"]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "No")

    def test_fluent_unknown_skill_reviews(self):
        res = _engine().resolve(ScreeningQuestion(
            text="Are you fluent in Cobol?", kind="radio",
            options=["Yes", "No"]))
        self.assertIsNone(res)

    def test_self_rating_known_skill_eight(self):
        res = _engine().resolve(ScreeningQuestion(
            text="Rate yourself from 1-10 in Data Structures and Algorithms?",
            kind="text", options=[]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "8")

    def test_self_rating_unknown_skill_reviews(self):
        res = _engine().resolve(ScreeningQuestion(
            text="Rate yourself 1 to 10 in Cobol?", kind="text", options=[]))
        self.assertIsNone(res)

    def test_rating_never_types_yes(self):
        """Willingness must not answer a rating box with 'Yes'."""
        res = _engine().resolve(ScreeningQuestion(
            text="How would you rate your Python skills on a scale of 1-10?",
            kind="text", options=[]))
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.value, "8")


if __name__ == "__main__":
    unittest.main()
