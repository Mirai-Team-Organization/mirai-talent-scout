"""
Rank and shortlist candidates — LLM-driven comparative ranking.

Flow:
  1. Hard-filter (dealbreakers, location_fit floor, fit_score floor)
  2. Sort by LLM fit_score (from score_candidate_rubric)
  3. Pass top 15 to Sonnet for comparative ranking
  4. Return top `limit` in Sonnet's order, falling back to fit_score sort on failure
"""

from __future__ import annotations

import json
import os
import re
import threading

import boto3
import botocore.config
from strands import tool

from scoring.talent_scorer import GRADE_ORDER

# ── Bedrock Sonnet client ─────────────────────────────────────────────────────

_bedrock = None
_bedrock_lock = threading.Lock()


def _get_bedrock():
    global _bedrock
    if _bedrock is None:
        with _bedrock_lock:
            if _bedrock is None:
                _bedrock = boto3.client(
                    "bedrock-runtime",
                    region_name=os.environ.get("AWS_REGION", "eu-west-1"),
                    config=botocore.config.Config(read_timeout=30, connect_timeout=5),
                )
    return _bedrock


def _sonnet(system: str, user: str, max_tokens: int = 1500) -> str:
    model_id = os.environ.get(
        "BEDROCK_MODEL", "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
    )
    resp = _get_bedrock().converse(
        modelId=model_id,
        system=[{"text": system}],
        messages=[{"role": "user", "content": [{"text": user}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
    )
    return resp["output"]["message"]["content"][0]["text"].strip()


# ── Comparative ranking prompt ────────────────────────────────────────────────

_RANK_SYSTEM = """You are a senior technical recruiter making the final call on a candidate shortlist.

You have received a set of candidates pre-scored by an AI recruiter. Your job is to \
comparatively rank them for this specific role — considering trade-offs between candidates, \
not just each one in isolation. Think about: who has the best combination of skills and \
experience for THIS role? Who is over- or under-qualified? Who has the best career \
trajectory signal? Does location actually matter here?

Output a single JSON object:
{
  "ranking": [
    {"id": "<github_username>", "rank": 1, "reason": "one sentence why above rank 2"},
    {"id": "<github_username>", "rank": 2, "reason": "one sentence why below rank 1"},
    ...
  ]
}

Include ALL candidates in the ranking. Return ONLY the JSON object. No markdown fences."""


def _compact_candidate(c: dict) -> str:
    """Build a one-line candidate summary for the Sonnet comparative ranking prompt."""
    p = c.get("profile") or {}
    li = c.get("linkedin") or {}
    cs = c.get("career_signals") or {}

    login = p.get("login") or c.get("github_username") or "?"
    name = p.get("name") or li.get("full_name") or login
    title = li.get("current_title") or c.get("job_role") or "?"
    company = li.get("current_company") or ""
    at_company = f" @ {company}" if company else ""
    location = p.get("location") or li.get("location") or "location unknown"
    overall = c.get("overall_match_pct") or c.get("fit_score") or 0
    s_skill = c.get("score_skill_match", 0)
    s_exp   = c.get("score_experience_depth", 0)
    s_pot   = c.get("score_potential", 0)
    flag    = c.get("flag", "")
    note    = c.get("recruiter_note") or ""
    trajectory = cs.get("career_trajectory") or ""
    trajectory_str = f", {trajectory}" if trajectory and trajectory != "insufficient_data" else ""

    line = (
        f"[{login}] {name} | {title}{at_company} | {location}{trajectory_str} | "
        f"overall: {overall} (skill:{s_skill} exp:{s_exp} pot:{s_pot}) [{flag}]"
    )
    if note:
        line += f"\n  → {note}"
    return line


def _sonnet_rerank(candidates: list[dict], talent_brief: dict) -> list[dict] | None:
    """
    Ask Sonnet to comparatively rank candidates for the role.
    Returns candidates in ranked order, or None on failure (caller falls back to fit_score sort).
    """
    if len(candidates) <= 1:
        return candidates

    title = talent_brief.get("title", "Software Engineer")
    seniority = talent_brief.get("seniority", "mid")
    location = talent_brief.get("location", "")
    remote_str = " (remote eligible)" if talent_brief.get("remote_eligible") else ""
    rubric_text = talent_brief.get("rubric_text", "")
    dealbreaker_text = talent_brief.get("dealbreaker_text", "")

    role_block = f"Role: {title} ({seniority}), {location}{remote_str}"
    if rubric_text:
        role_block += f"\nIdeal candidate: {rubric_text}"
    if dealbreaker_text:
        role_block += f"\nDealbreakers: {dealbreaker_text}"

    candidate_block = "\n\n".join(_compact_candidate(c) for c in candidates)

    user_msg = f"{role_block}\n\nCandidates to rank:\n\n{candidate_block}"

    try:
        raw = _sonnet(_RANK_SYSTEM, user_msg, max_tokens=1500)
        cleaned = re.sub(r"```[a-z]*\n?", "", raw).strip()
        data = json.loads(cleaned)
        ranking = data.get("ranking") or []
        if not ranking:
            return None

        # Build id → candidate map (by github_username / profile.login)
        id_map: dict[str, dict] = {}
        for c in candidates:
            p = c.get("profile") or {}
            login = p.get("login") or c.get("github_username")
            if login:
                id_map[login] = c

        # Sort by Sonnet's rank, attach reason
        ranked_entries = sorted(ranking, key=lambda r: r.get("rank", 999))
        result = []
        seen = set()
        for entry in ranked_entries:
            cid = entry.get("id", "")
            if cid in id_map and cid not in seen:
                candidate = dict(id_map[cid])
                reason = entry.get("reason", "")
                if reason:
                    candidate["rank_reason"] = reason
                result.append(candidate)
                seen.add(cid)

        # Append any candidates Sonnet missed (shouldn't happen, but be safe)
        for c in candidates:
            p = c.get("profile") or {}
            login = p.get("login") or c.get("github_username")
            if login and login not in seen:
                result.append(c)

        return result if result else None

    except Exception as e:
        print(f"[rank_shortlist] Sonnet rerank failed: {e} — falling back to fit_score sort")
        return None


@tool
def rank_shortlist(
    candidates: list[dict],
    limit: int = 10,
    talent_brief: dict | None = None,
) -> list[dict]:
    """
    Rank candidates using Sonnet comparative reasoning, return the top shortlist.

    Filters out dealbreaker hits, wrong-region candidates, and very low scorers.
    Then uses Sonnet to comparatively rank the top candidates for the specific role,
    considering trade-offs between them — not just individual scores.

    Args:
        candidates: Scored candidate dicts from score_candidate_rubric()
        limit: Max candidates to return (default 10)
        talent_brief: TalentBrief dict from build_talent_brief() — required for
                      comparative ranking. If omitted, falls back to fit_score sort.

    Returns:
        Ranked list of up to `limit` candidate dicts.
    """
    # Hard filter: drop confirmed dealbreakers and very low scorers.
    n_dealbreaker = sum(1 for c in candidates if c.get("dealbreaker_hit"))
    n_misaligned  = sum(1 for c in candidates if not c.get("dealbreaker_hit") and c.get("flag") == "misaligned")
    n_low_score   = sum(1 for c in candidates if not c.get("dealbreaker_hit") and c.get("flag") != "misaligned" and (c.get("overall_match_pct") or c.get("fit_score") or 0) < 25)
    print(f"[rank_shortlist] {len(candidates)} in | dealbreaker_hit={n_dealbreaker} misaligned={n_misaligned} low_score={n_low_score}")
    eligible = [
        c for c in candidates
        if not c.get("dealbreaker_hit")
        and c.get("flag") != "misaligned"
        and (c.get("overall_match_pct") or c.get("fit_score") or 0) >= 25
    ]

    # Sort by overall_match_pct, then location (city > country priority), then sub-scores
    def _fit_sort_key(c: dict):
        overall  = c.get("overall_match_pct") or c.get("fit_score") or 0
        loc_fit  = c.get("location_fit") or 0   # 100=city, 90=nearby, 70=same country, 10=abroad
        s_skill  = c.get("score_skill_match") or 0
        s_exp    = c.get("score_experience_depth") or 0
        mob      = (c.get("mobility") or {}).get("mobility_score") or 0
        talent   = (c.get("talent_score") or {}).get("overall") or 0
        grade    = GRADE_ORDER.get((c.get("talent_score") or {}).get("grade", "C"), 0)
        return (overall, loc_fit, s_skill, s_exp, mob, talent, grade)

    eligible.sort(key=_fit_sort_key, reverse=True)

    # Sonnet comparatively ranks the top 15; the rest follow in fit_score order
    top = eligible[:15]
    rest = eligible[15:]

    if talent_brief and len(top) > 1:
        reranked = _sonnet_rerank(top, talent_brief)
        if reranked:
            top = reranked

    return [_slim(c) for c in top + rest]


def _slim(c: dict) -> dict:
    """
    Strip heavy fields that were only needed during scoring.
    Keeps everything the UI needs; removes blobs that bloat the agent context.
    Retains up to 3 LinkedIn positions so the UI can show career history.
    """
    li = c.get("linkedin") or {}
    positions = (li.get("positions") or [])[:3]
    slim_li = {k: v for k, v in li.items()
               if k not in ("education", "fetched_at", "source", "github_username")}
    slim_li["positions"] = positions

    p = c.get("profile") or {}
    slim_p = {k: v for k, v in p.items()
              if k not in ("createdAt", "email", "twitterUsername", "websiteUrl")}

    return {
        "source":                 c.get("source"),
        "profile":                slim_p,
        "languages":              (c.get("languages") or [])[:5],
        "all_skills":             (c.get("all_skills") or [])[:8],
        "job_role":               c.get("job_role"),
        "seniority":              c.get("seniority"),
        "talent_score":           c.get("talent_score"),
        # Primary score
        "overall_match_pct":      c.get("overall_match_pct") or c.get("fit_score") or 0,
        "fit_score":              c.get("overall_match_pct") or c.get("fit_score") or 0,
        "flag":                   c.get("flag", ""),
        # Sub-scores
        "score_skill_match":      c.get("score_skill_match", 0),
        "note_skill_match":       c.get("note_skill_match", ""),
        "score_experience_depth": c.get("score_experience_depth", 0),
        "note_experience_depth":  c.get("note_experience_depth", ""),
        "score_potential":        c.get("score_potential", 0),
        "note_potential":         c.get("note_potential", ""),
        # Rubric evaluation
        "deal_breakers_detail":   c.get("deal_breakers_detail") or [],
        "must_haves_met":         c.get("must_haves_met") or [],
        "must_haves_gap":         c.get("must_haves_gap") or [],
        "nice_to_haves_met":      c.get("nice_to_haves_met") or [],
        "recruiter_note":         c.get("recruiter_note") or "",
        "rank_reason":            c.get("rank_reason") or "",
        # Gates
        "salary_fit":             c.get("salary_fit"),
        "location_fit":           c.get("location_fit"),
        "dealbreaker_hit":        c.get("dealbreaker_hit"),
        "linkedin":               slim_li or None,
        "career_signals":         c.get("career_signals"),
        "mobility":               {
            "mobility_score":    (c.get("mobility") or {}).get("mobility_score"),
            "data_completeness": (c.get("mobility") or {}).get("data_completeness"),
        } if c.get("mobility") else None,
    }
