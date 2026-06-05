"""
Rubric scoring prompt evaluation tests.

These are *integration* tests — they make real Bedrock calls.
Run them with:

    pytest tests/prompts/ -v -m prompt_eval

Or with live logging to see per-criterion reasoning:

    pytest tests/prompts/ -v -m prompt_eval -s

To run only specific test cases:

    pytest tests/prompts/ -v -m prompt_eval -k "backend_strong_match"

Requires: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY env vars (or IAM role).
          BEDROCK_HAIKU_MODEL defaults to eu.anthropic.claude-haiku-4-5-20251001-v1:0.

Adding new test cases:
  Edit tests/prompts/fixtures/cases.json. Each case needs:
    id, description, dealbreakers (semicolon-separated string), criteria[],
    candidate_summary, expected.dealbreaker (bool),
    expected.verdicts {criterion → YES|NO}

  Ideally pull real candidates from your Supabase `company_job_postings` /
  `internal_pool` tables: run the agent on a known role, capture the candidate
  summary (log _build_candidate_summary output), record the correct verdict,
  add it here. That closes the loop between real applications and prompt quality.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

# ── Fixtures ───────────────────────────────────────────────────────────────────

FIXTURES_PATH = Path(__file__).parent / "fixtures" / "cases.json"


def load_cases() -> list[dict]:
    return json.loads(FIXTURES_PATH.read_text())


def pytest_generate_tests(metafunc):
    """Parametrize test_case_* functions over every entry in cases.json."""
    if "case" in metafunc.fixturenames:
        cases = load_cases()
        metafunc.parametrize(
            "case",
            cases,
            ids=[c["id"] for c in cases],
        )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_dealbreakers(dealbreaker_text: str) -> list[str]:
    """Split dealbreaker string on semicolons or commas, drop fragments < 3 words."""
    if not dealbreaker_text:
        return []
    parts = re.split(r"[;,]", dealbreaker_text)
    return [s.strip() for s in parts if s.strip() and len(s.strip().split()) >= 3]


def _verdicts_from_result(result: dict) -> dict[str, str]:
    """Return {criterion_text: YES|NO} from _evaluate_candidate dict output."""
    verdicts: dict[str, str] = {}
    for item in result.get("must_haves_met") or []:
        verdicts[item] = "YES"
    for item in result.get("must_haves_gap") or []:
        verdicts[item] = "NO"
    return verdicts


def _dealbreaker_hit(result: dict) -> bool:
    detail = result.get("deal_breakers_detail") or []
    return bool(detail) and not all(d["met"] for d in detail)


# ── Main test ──────────────────────────────────────────────────────────────────

@pytest.mark.prompt_eval
def test_case_rubric(case: dict):
    """
    Run _evaluate_candidate() against a fixture case and assert all expected verdicts.

    Failures point directly at prompt regressions — fix _SCORE_SYSTEM or
    _build_candidate_summary, re-run to verify.
    """
    from agent.tools.score_candidate_rubric import _evaluate_candidate

    deal_breakers = _parse_dealbreakers(case.get("dealbreakers", ""))
    must_haves: list[str] = case["criteria"]
    candidate_summary: str = case["candidate_summary"]
    expected: dict = case["expected"]

    result = _evaluate_candidate(
        deal_breakers=deal_breakers,
        must_haves=must_haves,
        nice_to_haves=[],
        candidate_summary=candidate_summary,
    )

    hit = _dealbreaker_hit(result)
    verdicts = _verdicts_from_result(result)

    # Print full output for -s debugging
    print(f"\n{'─'*60}")
    print(f"Case: {case['id']}")
    print(f"Dealbreaker hit: {hit}")
    print(f"Scores: skill={result.get('score_skill_match')} exp={result.get('score_experience_depth')} pot={result.get('score_potential')}")
    print(f"Must-haves met: {result.get('must_haves_met')}")
    print(f"Must-haves gap: {result.get('must_haves_gap')}")
    print(f"Recruiter note: {result.get('recruiter_note', '')[:120]}")

    # ── Assert dealbreaker ────────────────────────────────────────────────────
    assert hit == expected["dealbreaker"], (
        f"[{case['id']}] dealbreaker: expected={expected['dealbreaker']}  got={hit}\n"
        f"deal_breakers_detail={result.get('deal_breakers_detail')}"
    )

    # ── Assert per-criterion verdicts ─────────────────────────────────────────
    # Only assert verdicts explicitly listed in expected.verdicts.
    # YES = in must_haves_met, NO = in must_haves_gap.
    if not hit:
        for criterion, expected_verdict in expected.get("verdicts", {}).items():
            actual = verdicts.get(criterion)
            assert actual is not None, (
                f"[{case['id']}] criterion not found in result: '{criterion}'\n"
                f"met={result.get('must_haves_met')}\ngap={result.get('must_haves_gap')}"
            )
            assert actual == expected_verdict, (
                f"[{case['id']}] '{criterion[:60]}'\n"
                f"  expected={expected_verdict!r}  got={actual!r}"
            )


# ── Aggregate accuracy report ──────────────────────────────────────────────────

@pytest.mark.prompt_eval
def test_aggregate_accuracy(capsys):
    """
    Run all cases, count pass/fail, print an accuracy table.
    Does NOT fail the suite on individual mismatches — use this for prompt iteration:

        pytest tests/prompts/test_rubric_eval.py::test_aggregate_accuracy -v -s
    """
    from agent.tools.score_candidate_rubric import _evaluate_candidate

    cases = load_cases()
    rows = []

    for case in cases:
        deal_breakers = _parse_dealbreakers(case.get("dealbreakers", ""))
        result = _evaluate_candidate(
            deal_breakers=deal_breakers,
            must_haves=case["criteria"],
            nice_to_haves=[],
            candidate_summary=case["candidate_summary"],
        )

        hit = _dealbreaker_hit(result)
        expected = case["expected"]
        db_ok = hit == expected["dealbreaker"]
        verdict_hits, verdict_total = 0, 0

        if not hit:
            verdicts = _verdicts_from_result(result)
            for crit, exp_v in expected.get("verdicts", {}).items():
                verdict_total += 1
                if verdicts.get(crit) == exp_v:
                    verdict_hits += 1

        rows.append({
            "id":            case["id"],
            "db_ok":         db_ok,
            "verdict_hits":  verdict_hits,
            "verdict_total": verdict_total,
        })

    total_cases = len(rows)
    db_correct = sum(1 for r in rows if r["db_ok"])
    v_hits = sum(r["verdict_hits"] for r in rows)
    v_total = sum(r["verdict_total"] for r in rows)

    with capsys.disabled():
        print(f"\n{'═'*68}")
        print(f"{'RUBRIC EVAL ACCURACY REPORT':^68}")
        print(f"{'═'*68}")
        print(f"{'Case':<32} {'Dealbreaker':>12} {'Criteria':>12}")
        print(f"{'─'*68}")
        for r in rows:
            db_str = "✓" if r["db_ok"] else "✗"
            crit_str = (
                f"{r['verdict_hits']}/{r['verdict_total']}"
                if r["verdict_total"] else "n/a"
            )
            print(f"{r['id']:<32} {db_str:>12} {crit_str:>12}")
        print(f"{'─'*68}")
        print(f"{'TOTAL':<32} {db_correct}/{total_cases:>10} {v_hits}/{v_total:>9}")
        db_pct = db_correct / total_cases * 100 if total_cases else 0
        v_pct = v_hits / v_total * 100 if v_total else 0
        print(f"{'ACCURACY':<32} {db_pct:>11.0f}% {v_pct:>10.0f}%")
        print(f"{'═'*68}\n")
