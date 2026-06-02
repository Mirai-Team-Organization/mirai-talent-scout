"""Unit tests for detect_move_signals() — all edge cases."""

import pytest
from datetime import date, timedelta

from agent.models import LinkedInEnrichment, LinkedInPosition
from scoring.mobility_scorer import detect_move_signals


def _months_ago(n: int) -> str:
    d = date.today() - timedelta(days=n * 30)
    return d.strftime("%Y-%m")


def _make_enrichment(positions: list[dict]) -> LinkedInEnrichment:
    return LinkedInEnrichment(
        github_username="testuser",
        positions=[LinkedInPosition(**p) for p in positions],
    )


# ── Happy path ────────────────────────────────────────────────────────────────

def test_peak_mobility_window():
    """Developer 24 months into current role → high mobility score."""
    enrichment = _make_enrichment([
        {"title": "Senior Engineer", "company": "AcmeCorp",
         "start_date": _months_ago(24), "is_current": True},
        {"title": "Engineer", "company": "OldCorp",
         "start_date": _months_ago(48), "end_date": _months_ago(24)},
    ])
    result = detect_move_signals(enrichment)

    assert result.mobility_score is not None
    assert result.mobility_score >= 50, "24-month tenure should score in peak mobility window"
    assert result.data_completeness > 0.5


def test_recent_joiner_low_score():
    """Developer 6 months into role → low mobility (recency lock-in)."""
    enrichment = _make_enrichment([
        {"title": "Engineer", "company": "NewCo",
         "start_date": _months_ago(6), "is_current": True},
    ])
    result = detect_move_signals(enrichment)

    assert result.mobility_score is not None
    assert result.mobility_score < 40, "6-month tenure should indicate low mobility"


def test_stagnant_career_high_score():
    """Same title for 5 years → high velocity signal (no promotions)."""
    enrichment = _make_enrichment([
        {"title": "Software Engineer", "company": "BigCorp",
         "start_date": _months_ago(60), "is_current": True},
        {"title": "Software Engineer", "company": "OldCorp",
         "start_date": _months_ago(84), "end_date": _months_ago(60)},
    ])
    result = detect_move_signals(enrichment)

    assert result.signals.velocity is not None
    assert result.signals.velocity.promotions == 0
    # Stagnant career should be a positive mobility signal
    assert result.signals.velocity.score >= 60


def test_company_health_layoffs():
    """Company with known layoffs → high company health signal."""
    enrichment = _make_enrichment([
        {"title": "Engineer", "company": "TechCo",
         "start_date": _months_ago(30), "is_current": True},
    ])
    result = detect_move_signals(enrichment, company_health_override="layoffs")

    assert result.signals.company_health is not None
    assert result.signals.company_health.score >= 80
    assert result.signals.company_health.signal == "layoffs_detected"


# ── Partial data ──────────────────────────────────────────────────────────────

def test_partial_signals_completeness():
    """Only tenure signal available → completeness = 0.25, score still returned."""
    enrichment = _make_enrichment([
        {"title": "Engineer", "company": "AcmeCorp",
         "start_date": _months_ago(24), "is_current": True},
        # Only one position — velocity and frequency can't be computed
    ])
    result = detect_move_signals(enrichment)

    assert result.mobility_score is not None, "Should return a score even with partial data"
    assert result.data_completeness < 1.0
    assert "velocity" in result.missing_signals or "frequency" in result.missing_signals


def test_all_signals_missing_returns_none():
    """Empty positions → mobility_score is None (not 0)."""
    enrichment = _make_enrichment([])
    result = detect_move_signals(enrichment)

    assert result.mobility_score is None, \
        "No data should return None, not 0 — these are semantically different"
    assert result.data_completeness == 0.0
    assert len(result.missing_signals) == 4
    assert set(result.missing_signals) == {"tenure", "velocity", "frequency", "company_health"}


def test_no_start_dates_returns_none():
    """Positions without start dates → can't compute tenure → None score."""
    enrichment = _make_enrichment([
        {"title": "Engineer", "company": "AcmeCorp", "is_current": True},
    ])
    result = detect_move_signals(enrichment)

    assert result.mobility_score is None
    assert "tenure" in result.missing_signals


# ── Never raises ──────────────────────────────────────────────────────────────

def test_does_not_raise_on_empty_enrichment():
    """detect_move_signals must never raise — always returns a MobilityScore."""
    enrichment = LinkedInEnrichment(github_username="ghost")
    result = detect_move_signals(enrichment)  # should not raise
    assert result is not None


def test_does_not_raise_on_malformed_dates():
    """Malformed date strings are handled gracefully."""
    enrichment = _make_enrichment([
        {"title": "Engineer", "company": "AcmeCorp",
         "start_date": "not-a-date", "is_current": True},
    ])
    result = detect_move_signals(enrichment)  # should not raise
    assert result is not None
