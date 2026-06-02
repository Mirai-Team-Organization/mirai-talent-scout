"""
TalentScoutAgent — AWS Strands orchestrator.

Single agent with 5 tools:
  search_github      → find candidates on GitHub
  enrich_linkedin    → add LinkedIn work history + mobility score
  score_candidate    → compute talent grade (S/A+/A/...)
  rank_shortlist     → rank by fit/mobility/grade, return top N

Usage:
    from agent.agent import create_agent
    agent = create_agent()
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


SYSTEM_PROMPT = """You are an AI talent scout for Mirai, a recruiting platform.

Your job is to find software developers who are a strong fit for a role AND likely to be open to new opportunities.

When a recruiter gives you a query:
1. Call search_github() to find matching candidates (default limit 20)
2. Call enrich_linkedin() on all candidates to get work history and mobility signals
3. Call score_candidate() for each candidate to compute their talent grade
4. Call rank_shortlist() to rank by fit + mobility + grade and return the top candidates

Always surface:
- The candidate's talent grade (S, A+, A, etc.)
- Their mobility score and what signals drove it (tenure, career stagnation, company health)
- Their top programming languages
- Any red flags (no open source contributions, inactive account, very short tenure everywhere)

Be factual and specific. Never invent data. If LinkedIn enrichment is missing, say "mobility data unavailable" rather than guessing.

Return results as a structured JSON array of candidates, sorted by rank."""


def create_agent(
    model_id: str | None = None,
    region: str | None = None,
) -> Agent:
    model = BedrockModel(
        model_id=model_id or os.environ.get(
            "BEDROCK_MODEL",
            "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
        ),
        region_name=region or os.environ.get("AWS_REGION", "eu-west-1"),
    )

    return Agent(
        model=model,
        tools=[search_github, enrich_linkedin, score_candidate, rank_shortlist],
        system_prompt=SYSTEM_PROMPT,
    )
