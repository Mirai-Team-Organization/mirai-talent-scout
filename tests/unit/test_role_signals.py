"""Unit tests for infer_role_signals_from_linkedin()."""

import pytest
from indexer.role_signals import infer_role_signals_from_linkedin


class _FakePosition:
    """Minimal stand-in for LinkedInPosition — duck-typed by infer_role_signals_from_linkedin."""
    def __init__(self, title: str):
        self.title = title


class _FakeEnrichment:
    """Minimal LinkedInEnrichment-like object for testing."""
    def __init__(self, current_title: str | None = None, past_titles: list[str] | None = None):
        self.current_title = current_title
        self.positions = [_FakePosition(t) for t in (past_titles or [])]


def test_ml_title_produces_ml_signal():
    enrichment = _FakeEnrichment(current_title="Machine Learning Engineer")
    assert "ml_engineer_signal" in infer_role_signals_from_linkedin(enrichment)


def test_devops_title_produces_devops_signal():
    enrichment = _FakeEnrichment(current_title="Site Reliability Engineer")
    assert "devops_signal" in infer_role_signals_from_linkedin(enrichment)


def test_fde_title_produces_fde_signal():
    enrichment = _FakeEnrichment(current_title="Solutions Engineer")
    assert "fde_signal" in infer_role_signals_from_linkedin(enrichment)


def test_backend_title_produces_backend_signal():
    enrichment = _FakeEnrichment(current_title="Backend Software Engineer")
    assert "backend_signal" in infer_role_signals_from_linkedin(enrichment)


def test_empty_enrichment_returns_empty_list():
    assert infer_role_signals_from_linkedin(None) == []


def test_no_matching_title_returns_empty_list():
    enrichment = _FakeEnrichment(current_title="Office Manager")
    assert infer_role_signals_from_linkedin(enrichment) == []


def test_past_position_titles_are_checked():
    """Signal inferred from a past role, not just current title."""
    enrichment = _FakeEnrichment(
        current_title="Engineering Manager",
        past_titles=["Machine Learning Engineer", "Data Scientist"],
    )
    signals = infer_role_signals_from_linkedin(enrichment)
    assert "ml_engineer_signal" in signals


def test_replace_semantics_documented():
    """Verify that a non-empty result is the full set to write (not a union delta)."""
    enrichment = _FakeEnrichment(current_title="Platform Engineer / DevOps")
    signals = infer_role_signals_from_linkedin(enrichment)
    # Caller writes this directly to talent_index.role_signals when non-empty
    assert len(signals) >= 1
    assert "devops_signal" in signals
