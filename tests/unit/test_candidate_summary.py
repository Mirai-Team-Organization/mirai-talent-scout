"""
Unit tests for _build_candidate_summary() — fetched_at recency signal (E1).

Uses sys.modules patching to stub out the `strands` import that score_candidate_rubric
requires at module load time (strands is an agent framework, not available in test env).
"""

from __future__ import annotations

import sys
import types
import unittest


def _load_build_candidate_summary():
    """Import _build_candidate_summary with strands stubbed out."""
    # Build a minimal strands stub with the `tool` decorator (no-op)
    strands_stub = types.ModuleType("strands")
    strands_stub.tool = lambda fn: fn  # decorator no-op
    sys.modules.setdefault("strands", strands_stub)
    from agent.tools.score_candidate_rubric import _build_candidate_summary
    return _build_candidate_summary


_build_candidate_summary = _load_build_candidate_summary()


def _base_profile() -> dict:
    return {
        "profile": {"login": "testdev", "bio": "Engineer", "location": "Berlin"},
        "languages": [{"name": "Python", "size": 1}],
        "contributions": {},
        "activityHeatmap": {},
    }


class TestFetchedAt(unittest.TestCase):

    def test_fetched_at_present_in_summary(self):
        """fetched_at date should appear in the candidate summary."""
        profile = _base_profile()
        profile["linkedin"] = {
            "current_title": "Software Engineer",
            "current_company": "Acme",
            "fetched_at": "2026-05-01T10:00:00+00:00",
        }
        summary = _build_candidate_summary(profile)
        self.assertIn("2026-05-01", summary, "fetched_at date should appear in summary")

    def test_missing_fetched_at_no_crash(self):
        """Missing fetched_at is silently omitted, no crash."""
        profile = _base_profile()
        profile["linkedin"] = {"current_title": "Software Engineer"}
        summary = _build_candidate_summary(profile)
        self.assertNotIn("LinkedIn data fetched", summary)

    def test_no_linkedin_dict_no_crash(self):
        """Profile with no linkedin key at all should not crash."""
        profile = _base_profile()
        summary = _build_candidate_summary(profile)
        self.assertIsInstance(summary, str)
        self.assertGreater(len(summary), 0)
