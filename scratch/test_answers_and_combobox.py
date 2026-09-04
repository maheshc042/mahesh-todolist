import sys
from naukri_agent.core.answers import AnswerEngine
from naukri_agent.core.models import ScreeningQuestion

def test_answers():
    engine = AnswerEngine(
        kb=[
            ("city you are currently residing or willing to relocate to", "Bengaluru"),
            ("how many years of experience in python", "2"),
        ],
        profile_answers={
            "notice period": "0 days",
        }
    )

    # Test 1: Availability intent
    q1 = ScreeningQuestion(text="L1 Bot Availability Date and Timing", kind="text", options=[])
    res1 = engine.resolve(q1)
    print("Test 1 (L1 Bot Availability):", res1)
    assert res1 is not None
    assert "Available on weekdays" in res1.value

    # Test 2: Availability with options
    q2 = ScreeningQuestion(
        text="Please select interview slot",
        kind="radio",
        options=["Weekday 10:00 AM - 1:00 PM", "Weekend Only", "Not available"]
    )
    res2 = engine.resolve(q2)
    print("Test 2 (Availability options):", res2)
    assert res2 is not None
    assert res2.value == "Weekday 10:00 AM - 1:00 PM"

    # Test 3: Location alias (Bengaluru vs Bangalore)
    q3 = ScreeningQuestion(
        text="city you are currently residing or willing to relocate to",
        kind="radio",
        options=["Bangalore", "Delhi", "Mumbai", "Hyderabad"]
    )
    res3 = engine.resolve(q3)
    print("Test 3 (Bengaluru/Bangalore alias):", res3)
    assert res3 is not None
    assert res3.value == "Bangalore"

    # Test 4: Willingness with location options
    q4 = ScreeningQuestion(
        text="Are you comfortable with the job location?",
        kind="radio",
        options=["Bengaluru", "Noida", "Pune"]
    )
    res4 = engine.resolve(q4)
    print("Test 4 (Willingness location):", res4)
    assert res4 is not None
    assert res4.value == "Bengaluru"

    # Test 5: General willingness
    q5 = ScreeningQuestion(
        text="Would you be willing to attend technical interviews?",
        kind="radio",
        options=["Yes", "No"]
    )
    res5 = engine.resolve(q5)
    print("Test 5 (General willingness):", res5)
    assert res5 is not None
    assert res5.value == "Yes"

    # Test 6: Real Run-214 Question (Experience designing APIs...)
    from naukri_agent.config import load_config
    from pathlib import Path
    cfg = load_config(Path('config/config.yaml'))
    profile = cfg.profiles[0]
    live_engine = AnswerEngine(
        kb=[(k, v) for k, v in cfg.answers.items()],
        profile_answers=profile.answers,
        experience=cfg.experience_for(profile),
    )
    q6 = ScreeningQuestion(
        text="Experience designing APIs and tools for agents, with attention to usability, reliability, permissions, and clear contracts.",
        kind="text",
        options=[],
    )
    res6 = live_engine.resolve(q6)
    print("Test 6 (Real run-214 API experience question):", res6)
    assert res6 is not None
    assert float(res6.value) >= 2.0

    # Test 7: Real Run-214 Question (Please select the city...)
    q7 = ScreeningQuestion(
        text="Please select the city you are currently residing or willing to relocate to",
        kind="combobox",
        options=["Bengaluru", "Mumbai", "Pune", "Delhi NCR"],
    )
    res7 = live_engine.resolve(q7)
    print("Test 7 (Real run-214 City relocation combobox):", res7)
    assert res7 is not None
    assert res7.value == "Bengaluru"

    print("ALL ANSWER ENGINE TESTS PASSED!")

if __name__ == "__main__":
    test_answers()
