"""
agentic_rerank — Sonnet-powered iterative reranker with pool expansion.

Replaces rank_shortlist for the job-posting-aware pipeline (Mode A).

A Strands sub-agent (Sonnet) that:
  1. Assesses the scored candidate pool (quality, flag distribution, must_haves coverage)
  2. If pool is thin or weak: expands by searching talent_index with relaxed criteria,
     scores new candidates via score_candidate_rubric
  3. Produces a final comparative ranking with per-candidate reasoning

Candidates entering this tool are already fully scored (score_candidate_rubric) and
have LinkedIn enrichment from the talent_index join. No enrichment step.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

import boto3
import botocore.config
from strands import tool as strands_tool
from strands import Agent
from strands.models import BedrockModel

from scoring.talent_scorer import GRADE_ORDER

# ── Compact summary builder ───────────────────────────────────────────────────

def _compact(profile: dict) -> dict:
    """Extract the fields the sub-agent needs for reasoning. ~300 chars per candidate."""
    p = profile.get("profile", {})
    li = profile.get("linkedin") or {}
    cs = profile.get("career_signals") or {}
    mob = profile.get("mobility") or {}

    return {
        "username":           p.get("login", ""),
        "name":               p.get("name") or li.get("full_name"),
        "location":           p.get("location") or li.get("location"),
        "source":             profile.get("source", "talent_index"),
        "fit_score":          profile.get("fit_score") or profile.get("overall_match_pct", 0),
        "flag":               profile.get("flag", "misaligned"),
        "score_skill_match":  profile.get("score_skill_match", 0),
        "note_skill_match":   profile.get("note_skill_match", ""),
        "score_experience_depth": profile.get("score_experience_depth", 0),
        "note_experience_depth":  profile.get("note_experience_depth", ""),
        "score_potential":    profile.get("score_potential", 0),
        "note_potential":     profile.get("note_potential", ""),
        "must_haves_met":     profile.get("must_haves_met") or [],
        "must_haves_gap":     profile.get("must_haves_gap") or [],
        "nice_to_haves_met":  profile.get("nice_to_haves_met") or [],
        "dealbreaker_hit":    profile.get("dealbreaker_hit", False),
        "salary_fit":         profile.get("salary_fit", "UNKNOWN"),
        "location_fit":       profile.get("location_fit"),
        "recruiter_note":     profile.get("recruiter_note", ""),
        "mobility_score":     mob.get("mobility_score"),
        "open_to_work":       li.get("open_to_work", False),
        "career_trajectory":  cs.get("career_trajectory"),
        "seniority_level":    cs.get("seniority_level"),
        "current_title":      li.get("current_title"),
        "current_company":    li.get("current_company"),
        "years_of_experience": cs.get("years_of_experience"),
    }


# ── Pool stats for system prompt context ─────────────────────────────────────

def _pool_stats(summaries: list[dict]) -> dict:
    flags = {"strong_fit": 0, "high_potential": 0, "misaligned": 0}
    for s in summaries:
        f = s.get("flag", "misaligned")
        flags[f] = flags.get(f, 0) + 1
    avg_fit = round(sum(s.get("fit_score", 0) for s in summaries) / len(summaries), 1) if summaries else 0
    return {
        "total": len(summaries),
        "strong_fit": flags["strong_fit"],
        "high_potential": flags["high_potential"],
        "misaligned": flags["misaligned"],
        "avg_fit_score": avg_fit,
    }


# ── System prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are a senior technical recruiter and ranking expert for Mirai.

You receive a pre-scored candidate pool for a specific job opening. Your job is to produce
the best possible ranked shortlist — not just sort by scores, but reason about:
  - Which candidates truly satisfy the must-haves vs. which have gaps
  - Career trajectory: ascending > lateral > descending
  - Mobility signals: tenure length, open_to_work, career velocity
  - Salary fit and location fit as tiebreakers
  - High-potential candidates worth flagging even if fit_score is mid-range

WHEN TO EXPAND THE POOL:
Call expand_pool() if ANY of these are true:
  - Fewer than 3 strong_fit candidates in the pool
  - The top candidates all share the same must_haves_gap (signals a search miss)
  - The pool has fewer than 5 non-dealbreaker candidates total
Do NOT expand if you already have 5+ strong_fit candidates — that is sufficient.
Call expand_pool() at most twice.

WHEN THE POOL IS EMPTY (all dealbreaker hits):
If the initial pool is empty OR all candidates hit dealbreakers, ALWAYS:
1. Call expand_pool("global") to remove location constraints
2. If still empty, call expand_pool("skills") to broaden tech stack
3. After expansion attempts: submit the BEST AVAILABLE candidates regardless of
   dealbreaker status — rank them honestly, noting gaps in the reasoning field.
   A partial match is more useful to the recruiter than zero results.
   NEVER return an empty ranking when candidates exist in the pool.

WHEN TO FINALIZE:
Call submit_ranking() once you are satisfied with the pool. Pass:
  - The top candidates in ranked order (max 10, exclude dealbreaker_hit=True)
  - A 2-3 sentence reasoning per candidate explaining their rank position comparatively
    (e.g. "Ranked #1 because she is the only candidate satisfying all must-haves with an
    ascending trajectory. Her TypeScript + Node.js background directly maps to the role...")

RANKING PRINCIPLES:
  1. Must-haves fully met → strong preference over partial gaps
  2. For ties in fit_score: ascending trajectory > lateral > descending
  3. open_to_work=True is a strong positive signal — boost over equally-scored candidates
  4. salary_fit=ABOVE_RANGE is a soft negative — rank below equivalent fit_score candidates
  5. Exclude dealbreaker_hit=True candidates entirely

Output only via submit_ranking(). Do not output a JSON array in text."""


# ── Main tool ─────────────────────────────────────────────────────────────────

@strands_tool
def agentic_rerank(
    candidates: list[dict],
    talent_brief: dict,
    limit: int = 10,
) -> list[dict]:
    """
    Rank a pre-scored candidate pool using an agentic Sonnet reasoner.

    The sub-agent assesses pool quality, optionally expands it by searching the
    talent index with relaxed criteria, then produces a final comparative ranking
    with per-candidate reasoning.

    All input candidates must already be scored via score_candidate_rubric().
    LinkedIn enrichment is assumed present (from talent_index join).

    Args:
        candidates: Fully scored candidate dicts from score_candidate_rubric()
        talent_brief: TalentBrief dict from build_talent_brief()
        limit: Max candidates to return (default 10)

    Returns:
        Ranked list of candidate dicts (subset of input + any newly found),
        each with an added rerank_reasoning field.
    """
    if not candidates:
        return []

    # ── Build profile registry (username → full dict, mutable) ───────────────
    profile_registry: dict[str, dict] = {}
    for c in candidates:
        username = c.get("profile", {}).get("login", "")
        if username:
            profile_registry[username] = c

    # ── Compact summaries for sub-agent context ───────────────────────────────
    summaries = [_compact(c) for c in candidates if not c.get("dealbreaker_hit")]
    # When all candidates hit dealbreakers, include them anyway so the sub-agent
    # has something to reason about (and can rank with a caveat).
    if not summaries:
        summaries = [_compact(c) for c in candidates]
    stats = _pool_stats(summaries)
    expand_call_count = [0]

    # ── Ranking capture ───────────────────────────────────────────────────────
    captured_ranking: list[dict] = []

    # ── Sub-agent tools (closures over brief + registry) ──────────────────────

    @strands_tool
    def expand_pool(relaxation_type: str) -> str:
        """
        Search for additional candidates with relaxed criteria and score them.

        relaxation_type (pick one):
          "remote"    — include remote-eligible candidates (relaxes strict location)
          "skills"    — relax required language overlap to 1 (accepts adjacent stacks)
          "seniority" — broaden to include one tier above/below required seniority
          "global"    — remove country filter entirely (any location worldwide)

        Returns a JSON summary of newly added candidates, or a message if none found.
        """
        from agent.tools.search_talent_index import search_talent_index
        from agent.tools.score_candidate_rubric import score_candidate_rubric

        if expand_call_count[0] >= 2:
            return "expand_pool limit reached (max 2 calls)."
        expand_call_count[0] += 1

        # Build a relaxed brief copy
        relaxed = dict(talent_brief)
        relaxed_iq = dict(talent_brief.get("index_query") or {})

        rt = (relaxation_type or "").lower().strip()
        if rt == "remote":
            relaxed["remote_eligible"] = True
            relaxed["location"] = ""          # clear location so country filter is removed
            relaxed_iq.pop("country", None)
        elif rt == "skills":
            relaxed_iq["min_language_overlap"] = 1
        elif rt == "seniority":
            tier_map = {"junior": ["junior", "mid"], "mid": ["junior", "mid", "senior"],
                        "senior": ["mid", "senior", "lead"], "lead": ["senior", "lead"]}
            relaxed["seniority"] = tier_map.get(talent_brief.get("seniority", "mid"), ["mid"])
        elif rt == "global":
            relaxed["location"] = ""
            relaxed_iq.pop("country", None)

        relaxed["index_query"] = relaxed_iq

        try:
            new_profiles = search_talent_index(relaxed, limit=50)
        except Exception as e:
            return f"Search failed: {e}"

        # Deduplicate
        fresh = [p for p in new_profiles
                 if p.get("profile", {}).get("login", "") not in profile_registry]

        if not fresh:
            return "No new candidates found — pool already covers this search space."

        # Score new candidates
        try:
            scored_fresh = score_candidate_rubric(fresh, talent_brief)
        except Exception as e:
            return f"Scoring failed for new candidates: {e}"

        # Register and build compact summaries for new non-dealbreaker candidates
        new_summaries = []
        for sc in scored_fresh:
            uname = sc.get("profile", {}).get("login", "")
            if uname:
                profile_registry[uname] = sc
                if not sc.get("dealbreaker_hit"):
                    new_summaries.append(_compact(sc))

        if not new_summaries:
            return "New candidates found but all hit dealbreakers — none added to pool."

        return json.dumps({
            "added": len(new_summaries),
            "candidates": new_summaries,
        }, default=str)

    @strands_tool
    def submit_ranking(ranked_candidates: str) -> str:
        """
        Submit the final ranked list. Call this once when you are done reasoning.

        ranked_candidates: JSON array of objects:
          [{"username": "...", "rank": 1, "reasoning": "2-3 sentence explanation"}, ...]

        Prefer excluding dealbreaker_hit=True candidates, but if no non-dealbreaker
        candidates exist, include the best available candidates with the dealbreaker
        gap explained in the reasoning field. Max 10 entries.
        """
        try:
            parsed = json.loads(ranked_candidates)
            if not isinstance(parsed, list):
                raise ValueError("expected a JSON array")
            captured_ranking.extend(parsed)
        except Exception as e:
            return f"Error parsing ranking: {e} — please retry with valid JSON."
        return f"Ranking captured ({len(captured_ranking)} candidates). Task complete."

    # ── Build initial message ─────────────────────────────────────────────────
    brief_context = (
        f"Role: {talent_brief.get('title', 'Unknown')} ({talent_brief.get('seniority', 'mid')})\n"
        f"Location: {talent_brief.get('location', 'unspecified')} "
        f"({'remote eligible' if talent_brief.get('remote_eligible') else 'on-site/hybrid'})\n"
        f"Skills required: {', '.join((talent_brief.get('skills') or [])[:8])}\n"
        f"Must-haves: {'; '.join((talent_brief.get('hiring_rubric') or {}).get('mustHaves') or [])}\n"
        f"Deal-breakers: {talent_brief.get('dealbreaker_text', 'none')}"
    )

    initial_message = (
        f"Job opening:\n{brief_context}\n\n"
        f"Pool stats: {json.dumps(stats)}\n\n"
        f"Scored candidates (dealbreaker hits excluded):\n"
        f"{json.dumps(summaries, default=str)}\n\n"
        f"Assess this pool and produce the final ranking via submit_ranking(). "
        f"Expand the pool first if quality is insufficient."
    )

    # ── Run sub-agent ─────────────────────────────────────────────────────────
    model_id = os.environ.get("BEDROCK_MODEL", "eu.anthropic.claude-sonnet-4-5-20250929-v1:0")
    region = os.environ.get("AWS_REGION", "eu-west-1")

    # In debug mode (LOG_LEVEL=DEBUG) print reasoning to terminal.
    # In production keep it silent so it never leaks to the UI stream.
    _debug = os.environ.get("LOG_LEVEL", "INFO").upper() == "DEBUG"

    def _silent(**_kwargs: Any) -> None:
        pass

    def _debug_cb(**kwargs: Any) -> None:
        data = kwargs.get("data", "")
        if data:
            print(f"[reranker] {data}", end="", flush=True)

    sub_agent = Agent(
        model=BedrockModel(
            model_id=model_id,
            region_name=region,
        ),
        tools=[expand_pool, submit_ranking],
        system_prompt=_SYSTEM_PROMPT,
        callback_handler=_debug_cb if _debug else _silent,
    )

    try:
        sub_agent(initial_message)
    except Exception as e:
        print(f"[agentic_rerank] Sub-agent error: {e}")

    # ── Reconstruct full profiles from captured ranking ───────────────────────
    if not captured_ranking:
        # Fallback: sort by fit_score + mobility + grade
        print("[agentic_rerank] Sub-agent did not submit a ranking — falling back to sort")
        return _fallback_sort(list(profile_registry.values()), limit)

    result = []
    for entry in captured_ranking[:limit]:
        uname = entry.get("username", "")
        profile = profile_registry.get(uname)
        if not profile:
            continue
        enriched = dict(profile)
        enriched["rank"] = entry.get("rank", len(result) + 1)
        enriched["rerank_reasoning"] = entry.get("reasoning", "")
        result.append(enriched)

    return result


def _fallback_sort(candidates: list[dict], limit: int) -> list[dict]:
    def _key(c):
        fit = c.get("fit_score") or c.get("overall_match_pct") or 0
        mob = (c.get("mobility") or {}).get("mobility_score") or 0
        ts  = (c.get("talent_score") or {}).get("overall") or 0
        grade = GRADE_ORDER.get((c.get("talent_score") or {}).get("grade", "C"), 0)
        return (fit, mob, ts, grade)

    clean = [c for c in candidates if not c.get("dealbreaker_hit")]
    pool = clean if clean else candidates  # fall back to all candidates if none pass dealbreakers
    ranked = sorted(pool, key=_key, reverse=True)
    for i, c in enumerate(ranked[:limit]):
        c["rank"] = i + 1
    return ranked[:limit]
