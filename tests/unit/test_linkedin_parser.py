"""
Unit tests for parse_harvestapi_response() and compute_career_signals().

Uses tests/unit/fixtures/harvestapi_sample.json as the canonical harvestapi payload.
"""

import json
from pathlib import Path

import pytest

from scoring.linkedin_analyzer import parse_harvestapi_response, compute_career_signals

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "harvestapi_sample.json").read_text()
)


# ── parse_harvestapi_response ─────────────────────────────────────────────────────

def test_parser_returns_tuple():
    enrichment, about_text = parse_harvestapi_response("jane-doe", FIXTURE)
    assert enrichment is not None
    assert isinstance(about_text, str)


def test_parser_full_name():
    enrichment, _ = parse_harvestapi_response("jane-doe", FIXTURE)
    assert enrichment.full_name == "Jane Doe"


def test_parser_location():
    enrichment, _ = parse_harvestapi_response("jane-doe", FIXTURE)
    assert enrichment.location == "San Francisco, CA, United States"


def test_parser_linkedin_url():
    enrichment, _ = parse_harvestapi_response("jane-doe", FIXTURE)
    assert enrichment.linkedin_url == "https://www.linkedin.com/in/jane-doe"


def test_parser_current_role():
    enrichment, _ = parse_harvestapi_response("jane-doe", FIXTURE)
    assert enrichment.current_title == "Senior Software Engineer"
    assert enrichment.current_company == "TechCorp"


def test_parser_positions_count():
    enrichment, _ = parse_harvestapi_response("jane-doe", FIXTURE)
    assert len(enrichment.positions) == 3


def test_parser_current_position_flag():
    enrichment, _ = parse_harvestapi_response("jane-doe", FIXTURE)
    current = next(p for p in enrichment.positions if p.is_current)
    assert current.title == "Senior Software Engineer"
    assert current.end_date is None


def test_parser_past_position_dates():
    enrichment, _ = parse_harvestapi_response("jane-doe", FIXTURE)
    past = [p for p in enrichment.positions if not p.is_current]
    assert all(p.start_date is not None for p in past)
    assert all(p.end_date is not None for p in past)


def test_parser_education():
    enrichment, _ = parse_harvestapi_response("jane-doe", FIXTURE)
    assert len(enrichment.education) == 1
    edu = enrichment.education[0]
    assert edu.school == "Stanford University"
    assert edu.degree == "Bachelor of Science"
    assert edu.year == 2018


def test_parser_about_not_stored_in_model():
    """about text must NOT appear in the enrichment model — scoring-only (D3)."""
    enrichment, about_text = parse_harvestapi_response("jane-doe", FIXTURE)
    dumped = enrichment.model_dump()
    assert "about" not in dumped
    assert "summary" not in dumped
    # but about_text is returned separately
    assert "50k" in about_text or "40%" in about_text


def test_parser_languages_spoken():
    enrichment, _ = parse_harvestapi_response("jane-doe", FIXTURE)
    assert enrichment.languages_spoken == ["English", "German"]


def test_parser_languages_missing_gracefully():
    data = {k: v for k, v in FIXTURE.items() if k != "languages"}
    enrichment, _ = parse_harvestapi_response("jane-doe", data)
    assert enrichment.languages_spoken == []


def test_parser_open_to_work_false():
    enrichment, _ = parse_harvestapi_response("jane-doe", FIXTURE)
    assert enrichment.open_to_work is False


def test_parser_open_to_work_true():
    data = dict(FIXTURE, openToWork=True)
    enrichment, _ = parse_harvestapi_response("jane-doe", data)
    assert enrichment.open_to_work is True


def test_parser_handles_missing_fields_gracefully():
    minimal = {"firstName": "Ghost", "lastName": ""}
    enrichment, about_text = parse_harvestapi_response("ghost", minimal)
    assert enrichment.full_name == "Ghost"
    assert enrichment.positions == []
    assert enrichment.education == []
    assert about_text == ""


# ── compute_career_signals ────────────────────────────────────────────────────

def test_career_signals_years_of_experience():
    enrichment, about_text = parse_harvestapi_response("jane-doe", FIXTURE)
    signals = compute_career_signals(enrichment, about_text)
    # Career spans ~2018-09 to today — at least 7 years
    assert signals.years_of_experience is not None
    assert signals.years_of_experience >= 7.0


def test_career_signals_seniority_senior():
    enrichment, about_text = parse_harvestapi_response("jane-doe", FIXTURE)
    signals = compute_career_signals(enrichment, about_text)
    assert signals.seniority_level == "senior"


def test_career_signals_seniority_founder_is_exec():
    from agent.models import LinkedInEnrichment
    enrichment = LinkedInEnrichment(github_username="founder", current_title="Co-Founder")
    signals = compute_career_signals(enrichment)
    assert signals.seniority_level == "exec"


def test_career_signals_trajectory_ascending():
    """Junior → Engineer → Senior Engineer = ascending trajectory."""
    enrichment, about_text = parse_harvestapi_response("jane-doe", FIXTURE)
    signals = compute_career_signals(enrichment, about_text)
    assert signals.career_trajectory == "ascending"


def test_career_signals_has_quantified_outcomes_true():
    """about text with '40%' and '50k users' should trigger quantified outcomes."""
    enrichment, about_text = parse_harvestapi_response("jane-doe", FIXTURE)
    signals = compute_career_signals(enrichment, about_text)
    assert signals.has_quantified_outcomes is True


def test_career_signals_no_quantified_outcomes_without_about():
    enrichment, _ = parse_harvestapi_response("jane-doe", FIXTURE)
    signals = compute_career_signals(enrichment, "")
    assert signals.has_quantified_outcomes is False


def test_career_signals_empty_enrichment_never_raises():
    from agent.models import LinkedInEnrichment
    enrichment = LinkedInEnrichment(github_username="ghost")
    signals = compute_career_signals(enrichment)
    assert signals is not None
    assert signals.years_of_experience is None
    assert signals.seniority_level is None
    assert signals.career_trajectory is None
    assert signals.has_quantified_outcomes is False


def test_career_signals_insufficient_data_trajectory():
    """Fewer than 3 positions → insufficient_data trajectory."""
    from agent.models import LinkedInEnrichment, LinkedInPosition
    enrichment = LinkedInEnrichment(
        github_username="sparse",
        current_title="Engineer",
        positions=[
            LinkedInPosition(title="Engineer", company="Co", start_date="2023-01", is_current=True),
        ],
    )
    signals = compute_career_signals(enrichment)
    assert signals.career_trajectory == "insufficient_data"


# ── openToWork hard floor (D5) ────────────────────────────────────────────────

def test_open_to_work_floors_mobility_at_85():
    from agent.models import LinkedInEnrichment, LinkedInPosition
    from scoring.linkedin_analyzer import detect_move_signals

    # 3-month tenure → would normally score very low
    from datetime import date, timedelta
    recent_start = (date.today() - timedelta(days=90)).strftime("%Y-%m")
    enrichment = LinkedInEnrichment(
        github_username="eager",
        open_to_work=True,
        positions=[
            LinkedInPosition(title="Engineer", company="Co", start_date=recent_start, is_current=True),
        ],
    )
    result = detect_move_signals(enrichment)
    assert result.mobility_score is not None
    assert result.mobility_score >= 85


def test_open_to_work_false_no_floor():
    from agent.models import LinkedInEnrichment, LinkedInPosition
    from scoring.linkedin_analyzer import detect_move_signals
    from datetime import date, timedelta

    recent_start = (date.today() - timedelta(days=90)).strftime("%Y-%m")
    enrichment = LinkedInEnrichment(
        github_username="stable",
        open_to_work=False,
        positions=[
            LinkedInPosition(title="Engineer", company="Co", start_date=recent_start, is_current=True),
        ],
    )
    result = detect_move_signals(enrichment)
    assert result.mobility_score is not None
    assert result.mobility_score < 85
