"""
TalentScoutAgent — AWS Strands orchestrator.

Two search modes:

  Job-posting-aware (preferred):
    build_talent_brief → search_internal_pool → search_github
    → score_candidate_rubric (× N) → rank_shortlist

  Natural-language query (legacy / no job posting):
    search_github → enrich_linkedin → score_candidate → rank_shortlist

Usage:
    from agent.agent import create_agent
    agent = create_agent()

    # Job-posting mode:
    result = agent("Find candidates for job posting abc-123")

    # NL mode:
    result = agent("Find senior React engineers in Zurich open to moving")
"""

from __future__ import annotations

import os

from strands import Agent
from strands.models import BedrockModel

from agent.tools.search_github import search_github
from agent.tools.enrich_linkedin import enrich_linkedin
from agent.tools.score_candidate import score_candidate
from agent.tools.rank_shortlist import rank_shortlist
from agent.tools.build_talent_brief import build_talent_brief
from agent.tools.search_internal_pool import search_internal_pool
from agent.tools.score_candidate_rubric import score_candidate_rubric
from agent.tools.agentic_rerank import agentic_rerank


SYSTEM_PROMPT = """You are Mirai's AI Talent Scout — an autonomous recruiting orchestrator.

You have two operating modes. Choose based on whether a job_posting_id is available.

────────────────────────────────────────────────────────
MODE A: Job-Posting-Aware Search (preferred)
Use when the recruiter provides a job_posting_id or asks to "find candidates for [role]"
and a job posting exists.

Step 1 — build_talent_brief(job_posting_id)
  Reads the hiring rubric, skills, salary range, and location from the job posting.
  Translates the rubric into a GitHub search query via Haiku.
  Returns a TalentBrief you will pass to all subsequent tools.

Step 2 — search_internal_pool(talent_brief, limit=20)
  Checks Mirai's own database first — zero API cost, fastest signal.
  Internal candidates already have full CV data; do NOT call enrich_linkedin() on them.
  If this returns ≥ 5 candidates, include them in the shortlist.

Step 3 — search_github(query=talent_brief.github_query, limit=20)
  Searches GitHub with the pre-translated query from the TalentBrief.
  Do NOT translate the query yourself — it was already translated in Step 1.

Step 4 — score_candidate_rubric(profile, talent_brief) × all candidates
  Scores every candidate (internal + GitHub) against the hiring rubric.
  Candidates with dealbreaker_hit=True are excluded from the shortlist.
  Call this on all candidates in parallel if the framework supports it.

Step 5 — enrich_linkedin(usernames) on top GitHub candidates only
  Only call for GitHub candidates with fit_score ≥ 40 (avoid wasting API calls).
  NEVER call for internal candidates — they already have CV data.

Step 6 — agentic_rerank(candidates, talent_brief, limit=10)
  Sonnet sub-agent that reasons comparatively over the scored pool:
    - Assesses pool quality (flag distribution, must_haves coverage)
    - If fewer than 3 strong_fit candidates: calls expand_pool() to search the
      talent index with relaxed criteria (remote, adjacent skills, broader seniority)
      and scores new candidates via score_candidate_rubric before ranking
    - Produces a final ranked list with per-candidate rerank_reasoning
  Use agentic_rerank() for Mode A. Do NOT use rank_shortlist() here.

────────────────────────────────────────────────────────
MODE B: Natural-Language Query (legacy)
Use when no job_posting_id is available.

Step 1 — search_github(query, limit=20)
Step 2 — enrich_linkedin(usernames) on all candidates
Step 3 — score_candidate(profile, hiring_context) for each candidate
Step 4 — rank_shortlist(candidates)

────────────────────────────────────────────────────────
ALWAYS surface in your final response:
- Source of each candidate: "internal_mirai" or "github"
- Talent grade (S, A+, A, …)
- fit_score (0–100) and what drove it: rubric match, salary fit, location fit
- Mobility score and signals (tenure, career velocity, openToWork)
- Top programming languages or skills
- Any dealbreakers that eliminated candidates
- Red flags (inactive account, very short tenures, no OSS contributions)
- source_reasoning from TalentBrief (explain why you searched where you did)

Be factual and specific. Never invent data. If a field is missing, say so.
Return results as a structured JSON array of candidates, sorted by rank.

COST DISCIPLINE:
- Internal pool first — it's free.
- Only enrich LinkedIn for candidates with fit_score ≥ 40.
- Dealbreaker pre-filter eliminates poor fits before expensive rubric scoring."""


def create_agent(
    model_id: str | None = None,
    region: str | None = None,
    system_prompt: str | None = None,
    tools: list | None = None,
    callback_handler=None,
) -> Agent:
    model = BedrockModel(
        model_id=model_id or os.environ.get(
            "BEDROCK_MODEL",
            "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
        ),
        region_name=region or os.environ.get("AWS_REGION", "eu-west-1"),
    )

    default_tools = [
        # Mode A — job-posting-aware
        build_talent_brief,
        search_internal_pool,
        score_candidate_rubric,
        agentic_rerank,
        # Mode B — NL legacy
        score_candidate,
        rank_shortlist,
        # Shared
        search_github,
        enrich_linkedin,
    ]

    kwargs: dict = dict(
        model=model,
        tools=tools if tools is not None else default_tools,
        system_prompt=system_prompt or SYSTEM_PROMPT,
    )
    if callback_handler is not None:
        kwargs["callback_handler"] = callback_handler

    return Agent(**kwargs)
