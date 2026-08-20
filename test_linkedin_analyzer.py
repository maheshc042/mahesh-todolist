"""
Unit Tests for LinkedIn Post Analyzer (Disabled / Commented Out for Future Use).
"""

# from naukri_agent.linkedin.analyzer import (
#     classify_role,
#     extract_recruiter_emails,
#     is_experience_match,
# )


# def test_extract_recruiter_emails():
#     text = "We are hiring! Contact HR at recruiter@techcorp.com or jobs@careers.io."
#     emails = extract_recruiter_emails(text)
#     assert "recruiter@techcorp.com" in emails
#     assert "jobs@careers.io" in emails


# def test_is_experience_match():
#     assert is_experience_match("Hiring 1-2 years experience Python developer") is True
#     assert is_experience_match("Looking for Senior Lead Architect 8+ years exp") is False


# def test_classify_role():
#     assert classify_role("Hiring Python Engineer with LLM, FastAPI and RAG experience") == "AI / Python Engineer"
#     assert classify_role("Hiring Full Stack Developer with React, Node.js and MongoDB") == "Full Stack Engineer"
