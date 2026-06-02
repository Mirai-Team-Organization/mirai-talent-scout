"""
Rank and shortlist candidates by fit score, mobility, and talent grade.
"""

from __future__ import annotations

import os
import json
import boto3

from strands import tool
from scoring.talent_scorer import GRADE_ORDER


def _fit_score_via_bedrock(candidate: dict, job_description: str) -> int:
    """Call Bedrock Haiku to score candidate fit against a job description."""
    client = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "eu-west-1"))
    model_id = os.environ.get("BEDROCK_HAIKU_MODEL", "eu.anthropic.claude-haiku-4-5-20251001-v1:0")

    profile = candidate.get("profile", {})
    talent = candidate.get("talent_score", {})
    languages = [l["name"] for l in candidate.get("languages", [])[:5]]

    prompt = f"""Rate this developer's fit for the job on a scale of 0-100. Return only JSON: {{"fit_score": <int>}}

Job description:
{job_description[:1000]}

Developer profile:
- Name: {profile.get('name', 'Unknown')}
- Bio: {profile.get('bio', 'None')}
- Location: {profile.get('location', 'Unknown')}
- Languages: {', '.join(languages)}
- Grade: {talent.get('grade', 'Unknown')}
- Open source contributions: {talent.get('breakdown', {}).get('open_source', {}).get('commit_count', 0)} commits"""

    response = client.invoke_model(
        modelId=model_id,
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 50,
            "messages": [{"role": "user", "content": prompt}],
        }),
        contentType="application/json",
        accept="application/json",
    )

    body = json.loads(response["body"].read())
    text = body["content"][0]["text"].strip()

    try:
        data = json.loads(text)
        return int(data.get("fit_score", 50))
    except (json.JSONDecodeError, ValueError):
        return 50


@tool
def rank_shortlist(
    candidates: list[dict],
    job_description: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """
    Rank candidates and return the top shortlist.

    Sort priority:
      1. fit_score (if job_description provided — Bedrock Haiku)
      2. mobility_score (if available)
      3. talent_score.overall
      4. grade (S > A+ > A > ...)

    Args:
        candidates: Enriched candidate dicts from enrich_linkedin()
        job_description: Optional job description for fit scoring
        limit: Number of candidates to return (default 10)

    Returns:
        Ranked list of candidate dicts, each with fit_score if JD provided.
    """
    if job_description:
        for c in candidates:
            try:
                c["fit_score"] = _fit_score_via_bedrock(c, job_description)
            except Exception as e:
                print(f"[rank_shortlist] Fit scoring failed for {c.get('profile', {}).get('login')}: {e}")
                c["fit_score"] = None

    def sort_key(c: dict):
        fit = c.get("fit_score") or 0
        mobility = (c.get("mobility") or {}).get("mobility_score") or 0
        talent = (c.get("talent_score") or {}).get("overall") or 0
        grade = GRADE_ORDER.get((c.get("talent_score") or {}).get("grade", "C"), 0)
        return (fit, mobility, talent, grade)

    ranked = sorted(candidates, key=sort_key, reverse=True)
    return ranked[:limit]
