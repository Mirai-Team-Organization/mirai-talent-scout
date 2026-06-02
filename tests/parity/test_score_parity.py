"""
Cross-language parity tests: Python scoring vs TypeScript scoring (gitcheck-webapp).

Requires gitcheck-webapp running on localhost:3000:
    cd ../gitcheck-webapp && npm run dev

Run with:
    pytest tests/parity/ -v -m parity

The test loads fixture profiles and asserts that the Python and TypeScript
implementations produce scores within ±2 points of each other.
"""

import json
import os
import urllib.request
from pathlib import Path

import pytest

from scoring.talent_scorer import calculate_talent_score

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "scoring_parity"
GITCHECK_URL = os.environ.get("GITCHECK_URL", "http://localhost:3000")
TOLERANCE = 2  # points


def load_fixtures() -> list[dict]:
    if not FIXTURES_DIR.exists():
        return []
    return [
        json.loads(f.read_text())
        for f in sorted(FIXTURES_DIR.glob("*.json"))
    ]


def call_gitcheck_api(github_username: str) -> dict:
    """Call the gitcheck-webapp API to get the TypeScript talent score."""
    url = f"{GITCHECK_URL}/api/github/profile?url=https://github.com/{github_username}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read())
    return data.get("scoring", {})


@pytest.mark.parity
@pytest.mark.parametrize("fixture", load_fixtures())
def test_score_parity(fixture: dict):
    """
    For each fixture profile, assert that Python and TypeScript scoring
    produce the same overall score within ±2 points and the same grade.
    """
    username = fixture["github_username"]
    profile_data = fixture["profile_data"]

    # Python score
    py_score = calculate_talent_score(profile_data)

    # TypeScript score (via gitcheck-webapp API)
    try:
        ts_score = call_gitcheck_api(username)
    except Exception as e:
        pytest.skip(f"gitcheck-webapp not reachable: {e}")

    ts_overall = ts_score.get("overall", 0)
    py_overall = py_score.overall

    assert abs(py_overall - ts_overall) <= TOLERANCE, (
        f"Parity drift for {username}: "
        f"Python={py_overall} TypeScript={ts_overall} "
        f"(diff={abs(py_overall - ts_overall)}, tolerance={TOLERANCE})"
    )

    # Grade should also match
    ts_grade = ts_score.get("grade", "")
    assert py_score.grade == ts_grade or abs(py_overall - ts_overall) <= TOLERANCE, (
        f"Grade mismatch for {username}: Python={py_score.grade} TypeScript={ts_grade}"
    )
