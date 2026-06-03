"""
build_talent_brief — reads a company_job_postings row and produces a TalentBrief.

Steps:
  1. Fetch job posting from Supabase
  2. Match role_scoring_config by title (ILIKE)
  3. Flatten hiring_rubric via Haiku → rubric_text + dealbreaker_text
  4. Translate rubric_text → GitHub search query via Haiku
  5. Normalise salary_range
  6. Return TalentBrief as dict
"""

from __future__ import annotations

import json
import os
import re

import boto3
from strands import tool

from agent.models import TalentBrief
from db.client import get_supabase
from scoring.salary_parser import parse_salary
from scoring.salary_benchmarks import derive_market

# ── Generic fallback role weights ─────────────────────────────────────────────
_GENERIC_WEIGHTS = {
    "technical": 0.30,
    "open_source": 0.25,
    "consistency": 0.20,
    "collaboration": 0.15,
    "presentation": 0.10,
}

# ── Bedrock client (lazy) ─────────────────────────────────────────────────────
_bedrock = None


def _get_bedrock():
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client(
            "bedrock-runtime",
            region_name=os.environ.get("AWS_REGION", "eu-west-1"),
        )
    return _bedrock


def _haiku(system: str, user: str, max_tokens: int = 300) -> str:
    """Call Haiku via Bedrock converse API. Returns the text response."""
    model_id = os.environ.get(
        "BEDROCK_HAIKU_MODEL", "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
    )
    resp = _get_bedrock().converse(
        modelId=model_id,
        system=[{"text": system}],
        messages=[{"role": "user", "content": [{"text": user}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
    )
    return resp["output"]["message"]["content"][0]["text"].strip()


# ── Rubric flattening ─────────────────────────────────────────────────────────

_RUBRIC_SYSTEM = """You are a recruiting assistant. Given a job posting rubric, output exactly two lines:
SEARCH: <1-sentence description of the ideal candidate for a GitHub user search. Focus on technical skills, years of experience, domain. Be specific and concise.>
DEALBREAKERS: <comma-separated list of dealbreakers from the rubric, or "none" if empty>

Output ONLY those two lines. No preamble, no explanation."""


def _flatten_rubric(rubric: dict, title: str, skills: list[str]) -> tuple[str, str]:
    """
    Returns (rubric_text, dealbreaker_text).
    Falls back to a simple skills-based description if rubric is empty.
    """
    if not rubric or not any(rubric.get(k) for k in ("mustHaves", "dealBreakers", "roleMission")):
        skills_str = ", ".join(skills[:5]) if skills else title
        return f"{title} engineer with strong {skills_str} skills", ""

    must_haves = "; ".join(rubric.get("mustHaves", [])[:5])
    deal_breakers = rubric.get("dealBreakers", [])
    role_mission = rubric.get("roleMission", "")

    user_msg = f"""mustHaves: {must_haves}
dealBreakers: {"; ".join(deal_breakers)}
roleMission: {role_mission}"""

    try:
        raw = _haiku(_RUBRIC_SYSTEM, user_msg, max_tokens=200)
    except Exception as e:
        print(f"[build_talent_brief] Haiku rubric flatten failed: {e}")
        skills_str = ", ".join(skills[:5]) if skills else title
        return f"{title} with {skills_str}", ""

    search_text = ""
    dealbreaker_text = ""
    for line in raw.splitlines():
        if line.upper().startswith("SEARCH:"):
            search_text = line[7:].strip()
        elif line.upper().startswith("DEALBREAKERS:"):
            raw_db = line[13:].strip()
            dealbreaker_text = "" if raw_db.lower() == "none" else raw_db

    if not search_text:
        skills_str = ", ".join(skills[:5]) if skills else title
        search_text = f"{title} with {skills_str}"

    return search_text, dealbreaker_text


# ── GitHub query translation ──────────────────────────────────────────────────

_QUERY_SYSTEM = """You are a GitHub user search query builder. Convert a candidate description into GitHub search syntax.

Valid qualifiers only:
- location:City  (city or country)
- language:Name  (Python, JavaScript, TypeScript, Go, Rust, Java, Ruby, C++, etc.)
- followers:>N   (10=junior, 50=mid, 100=senior, 500=influential)
- repos:>N       (5=active contributor)

Rules:
- Extract only what maps to a GitHub qualifier.
- Return ONLY the GitHub search string, nothing else.

Examples:
"Senior Python AI engineer in Milan"         → location:Milan language:Python followers:>100
"Mid-level TypeScript React engineer remote" → language:TypeScript followers:>50
"Junior data scientist"                      → language:Python followers:>10"""

_GITHUB_QUALIFIER_PREFIXES = (
    "language:", "location:", "followers:", "repos:", "in:", "type:", "is:",
)


def _translate_to_github_query(rubric_text: str, location: str, seniority: str) -> str:
    """Translate rubric_text + location + seniority into GitHub search syntax."""
    nl = f"{seniority} {rubric_text} in {location}" if location else f"{seniority} {rubric_text}"
    try:
        raw = _haiku(_QUERY_SYSTEM, nl, max_tokens=100)
    except Exception as e:
        print(f"[build_talent_brief] GitHub query translation failed: {e}")
        return rubric_text

    # Validate: strip unrecognised qualifiers
    tokens = raw.strip().split()
    valid = [
        t for t in tokens
        if ":" not in t
        or any(t.lower().startswith(p) for p in _GITHUB_QUALIFIER_PREFIXES)
    ]
    return " ".join(valid) or rubric_text


# ── Seniority normalisation ───────────────────────────────────────────────────

def _normalise_seniority(raw: str | None) -> str:
    """Map free-text seniority to junior|mid|senior|lead."""
    if not raw:
        return "mid"
    r = raw.lower().strip()
    if r in ("junior", "entry", "graduate", "intern"):
        return "junior"
    if r in ("senior", "staff", "principal", "expert"):
        return "senior"
    if r in ("lead", "manager", "director", "head", "vp"):
        return "lead"
    return "mid"


# ── Main tool ─────────────────────────────────────────────────────────────────

@tool
def build_talent_brief(job_posting_id: str) -> dict:
    """
    Build a TalentBrief from a company job posting.

    Reads hiring_rubric, skills, salary_range, location, seniority, and
    role_scoring_config weights. Translates the rubric to a GitHub search query.

    Args:
        job_posting_id: UUID of the company_job_postings row

    Returns:
        TalentBrief dict consumed by search_internal_pool(), search_github(),
        and score_candidate_rubric().
    """
    sb = get_supabase()

    # ── 1. Fetch job posting ──────────────────────────────────────────────────
    result = (
        sb.table("company_job_postings")
        .select("id,title,location,work_model,seniority,salary_range,skills,hiring_rubric,description")
        .eq("id", job_posting_id)
        .maybe_single()
        .execute()
    )
    if not result or not result.data:
        raise ValueError(f"Job posting {job_posting_id} not found")

    posting = result.data
    title: str = posting.get("title", "Software Engineer")
    location: str = posting.get("location", "")
    work_model: str = posting.get("work_model", "")
    seniority_raw: str = posting.get("seniority", "mid")
    salary_range_str: str | None = posting.get("salary_range")
    skills: list[str] = posting.get("skills") or []
    hiring_rubric: dict = posting.get("hiring_rubric") or {}

    seniority = _normalise_seniority(seniority_raw)
    remote_eligible = (
        "remote" in (work_model or "").lower()
        or "remote" in (location or "").lower()
    )

    # ── 2. Role scoring config ────────────────────────────────────────────────
    cfg_result = (
        sb.table("role_scoring_config")
        .select("*")
        .ilike("role_name", f"%{title.split()[0]}%")  # fuzzy: "AI Engineer" matches "AI"
        .eq("is_active", True)
        .maybe_single()
        .execute()
    )
    if cfg_result and cfg_result.data:
        cfg = cfg_result.data
        total = sum([
            cfg.get("technical_weight", 0),
            cfg.get("analytical_weight", 0),
            cfg.get("communication_weight", 0),
            cfg.get("ownership_weight", 0),
            cfg.get("judgment_weight", 0),
            cfg.get("collaboration_weight", 0),
        ]) or 1
        role_weights = {
            "technical":     cfg["technical_weight"] / total,
            "analytical":    cfg["analytical_weight"] / total,
            "communication": cfg["communication_weight"] / total,
            "ownership":     cfg["ownership_weight"] / total,
            "judgment":      cfg["judgment_weight"] / total,
            "collaboration": cfg["collaboration_weight"] / total,
        }
    else:
        role_weights = _GENERIC_WEIGHTS.copy()

    # ── 3. Flatten rubric via Haiku ───────────────────────────────────────────
    rubric_text, dealbreaker_text = _flatten_rubric(hiring_rubric, title, skills)

    # ── 4. Translate to GitHub search syntax ──────────────────────────────────
    github_query = _translate_to_github_query(rubric_text, location, seniority)

    # ── 5. Normalise salary ───────────────────────────────────────────────────
    salary_min, salary_max, salary_currency = parse_salary(salary_range_str)
    salary_market = derive_market(location) if not remote_eligible else "REMOTE"

    # ── 6. Build source reasoning ─────────────────────────────────────────────
    top_skills = ", ".join(skills[:3]) if skills else title
    source_reasoning = (
        f"Checking Mirai's internal talent pool first (zero API cost), "
        f"then searching GitHub for {seniority} {title} candidates "
        f"with focus on {top_skills}."
    )

    brief = TalentBrief(
        job_posting_id=job_posting_id,
        title=title,
        seniority=seniority,
        location=location,
        remote_eligible=remote_eligible,
        skills=skills,
        hiring_rubric=hiring_rubric,
        rubric_text=rubric_text,
        dealbreaker_text=dealbreaker_text,
        role_weights=role_weights,
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=salary_currency,
        salary_market=salary_market,
        github_query=github_query,
        sources=["internal_pool", "github_broad"],
        source_reasoning=source_reasoning,
    )

    return brief.model_dump()
