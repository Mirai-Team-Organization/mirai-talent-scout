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


# ── Combined rubric → brief translation (single Haiku call) ──────────────────

_BRIEF_SYSTEM = """You are a recruiting assistant. Given a job posting, output a single JSON object with exactly these keys:

  rubric_text: string       — one sentence describing the ideal candidate (skills, domain, experience level)
  dealbreaker_text: string  — comma-separated dealbreakers, or "" if none
  languages: array          — programming languages REQUIRED for this role (not nice-to-haves)
                              Valid values: Python, TypeScript, JavaScript, Go, Rust, Java, Kotlin, Swift, Ruby, C++, Scala, PHP
  role_signals: array       — role type tags. Read the full job description to determine the actual role type.
                              For generic titles like "Software Engineer", "Engineer", or "Developer", return up to 2 signals
                              if the JD content reveals a specific type (e.g. ["backend_signal", "fullstack_signal"]).
                              For clearly specialised titles, return exactly 1 signal.
                              Valid values ONLY: ml_engineer_signal, devops_signal, fullstack_signal, backend_signal, fde_signal
                              If the JD describes backend/API/server-side work, always include backend_signal.
  signals: array            — achievement signals. Leave empty unless the role explicitly requires OSS work.
                              Valid values ONLY: oss_contributor, starred_project_author
  internal_role_slugs: array — 2-4 kebab-case jobRole values matching the internal talent pool.
                              The pool stores values like "ai-engineer", "software-engineer",
                              "data-scientist", "backend-engineer", "frontend-engineer",
                              "fullstack-engineer", "ml-engineer", "devops-engineer", "python-developer".
                              Generate slug variants that cover the role intent broadly.
                              Empty array if the role is unclear.
                              Examples for "Senior ML Engineer": ["ml-engineer", "ai-engineer", "software-engineer"]
                              Examples for "React Frontend Developer": ["frontend-engineer", "fullstack-engineer"]
  country_code: string|null — ISO 3166-1 alpha-2 country code derived from the job location.
                              Null if not determinable (remote-only, multi-country, unspecified).
                              Examples: "IT" for Milan/Rome/Italy, "CH" for Zurich/Geneva/Switzerland,
                              "DE" for Berlin/Munich/Germany, null for "Remote" or "Worldwide".

Note: the developer index is pre-filtered to GitHub grade C+ or above. Do NOT filter on activity or seniority
— those are determined from LinkedIn enrichment. Your job is only to identify the technical type.

Return ONLY the JSON object. No markdown fences, no explanation."""

_VALID_ROLE_SIGNALS = {"ml_engineer_signal", "devops_signal", "fullstack_signal", "backend_signal", "fde_signal"}
_VALID_SIGNALS = {"oss_contributor", "starred_project_author"}


def _build_brief_from_rubric(
    rubric: dict,
    title: str,
    skills: list[str],
    location: str,
    seniority: str,
    job_description: str = "",
) -> dict:
    """
    Single Haiku call that returns rubric_text, dealbreaker_text, and index_query fields.
    Falls back to keyword heuristics on any error.
    """
    skills_str = ", ".join(skills[:8])
    must_haves = "; ".join((rubric.get("mustHaves") or [])[:6])
    deal_breakers = ", ".join((rubric.get("dealBreakers") or [])[:5])
    role_mission = rubric.get("roleMission") or ""

    # Include a JD excerpt so Haiku can infer role type from content, not just title.
    jd_excerpt = job_description[:1200].strip() if job_description else ""

    user_msg = (
        f"Title: {title}\n"
        f"Seniority: {seniority}\n"
        f"Location: {location}\n"
        f"Skills: {skills_str}\n"
        f"Must-haves: {must_haves}\n"
        f"Dealbreakers: {deal_breakers}\n"
        f"Role mission: {role_mission}"
    )
    if jd_excerpt:
        user_msg += f"\n\nJob description:\n{jd_excerpt}"

    try:
        raw = _haiku(_BRIEF_SYSTEM, user_msg, max_tokens=500)
        cleaned = re.sub(r"```[a-z]*\n?", "", raw).strip()
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            raise ValueError("not a dict")

        rubric_text = str(data.get("rubric_text") or f"{seniority} {title} with {skills_str}").strip()
        dealbreaker_text = str(data.get("dealbreaker_text") or "").strip()
        languages = [str(l) for l in (data.get("languages") or [])]
        role_signals = [s for s in (data.get("role_signals") or []) if s in _VALID_ROLE_SIGNALS]
        signals = [s for s in (data.get("signals") or []) if s in _VALID_SIGNALS]

        # New fields: internal pool search slugs + country code
        raw_slugs = data.get("internal_role_slugs")
        internal_role_slugs = (
            [str(s) for s in raw_slugs if s and isinstance(s, str)]
            if isinstance(raw_slugs, list) else []
        )

        raw_country = data.get("country_code")
        country_code = (
            str(raw_country).strip().upper()
            if raw_country and isinstance(raw_country, str) and len(str(raw_country).strip()) == 2
            else None
        )

        index_query: dict = {
            **({"languages":    languages}    if languages    else {}),
            **({"role_signals": role_signals} if role_signals else {}),
            **({"signals":      signals}      if signals      else {}),
            **({"country":      country_code} if country_code else {}),
        }

        return {
            "rubric_text":          rubric_text,
            "dealbreaker_text":     dealbreaker_text,
            "internal_role_slugs":  internal_role_slugs,
            "index_query":          index_query,
        }

    except Exception as e:
        print(f"[build_talent_brief] rubric translation failed: {e} — using fallback")
        skills_str_short = ", ".join(skills[:5]) if skills else title
        return {
            "rubric_text":          f"{seniority} {title} with {skills_str_short}",
            "dealbreaker_text":     "",
            "internal_role_slugs":  [],
            "index_query":          {},
        }


# ── Role type inference ───────────────────────────────────────────────────────

_ROLE_TYPE_MAP: list[tuple[list[str], str]] = [
    (["machine learning", "ml ", "data science", "ai engineer", "mlops", "llm"], "ml_engineer_signal"),
    (["devops", "platform", "sre ", "infrastructure", "cloud engineer", "devsecops"], "devops_signal"),
    (["fullstack", "full-stack", "full stack", "frontend", "react", "vue", "angular", "next.js"], "fullstack_signal"),
    (["backend", "back-end", "back end", "api engineer", "golang", "rust engineer", "java engineer"], "backend_signal"),
    (["solutions engineer", "forward deploy", "fde", "implementation engineer", "sales engineer"], "fde_signal"),
]

_ROLE_LANGUAGE_MAP: dict[str, list[str]] = {
    "ml_engineer_signal":  ["Python"],
    "devops_signal":       ["Python", "Go"],
    "fullstack_signal":    ["TypeScript", "JavaScript"],
    "backend_signal":      ["Go", "Rust", "Java", "Python", "Kotlin"],
    "fde_signal":          ["Python", "TypeScript", "JavaScript"],
}


def _infer_role_type(title: str, skills: list[str]) -> tuple[str | None, list[str]]:
    """
    Infer role_type signal name and primary language list from job title + skills.
    Returns (role_type, language_list).
    """
    text = f"{title} {' '.join(skills)}".lower()
    for keywords, signal in _ROLE_TYPE_MAP:
        if any(kw in text for kw in keywords):
            return signal, _ROLE_LANGUAGE_MAP.get(signal, [])

    # Fallback: derive languages from skills list directly
    _KNOWN_LANGUAGES = {
        "python", "typescript", "javascript", "go", "golang", "rust",
        "java", "kotlin", "swift", "c++", "scala",
    }
    lang_list = [s for s in skills if s.lower() in _KNOWN_LANGUAGES]
    return None, lang_list[:3]


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
    job_description: str = (posting.get("description") or "")[:3000]

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

    # ── 3. Single Haiku call: rubric_text + dealbreakers + index_query ───────────
    brief_data = _build_brief_from_rubric(hiring_rubric, title, skills, location, seniority, job_description)
    rubric_text: str = brief_data["rubric_text"]
    dealbreaker_text: str = brief_data["dealbreaker_text"]
    index_query: dict = brief_data["index_query"]
    internal_role_slugs: list[str] = brief_data.get("internal_role_slugs") or []

    # ── 4. Normalise salary ───────────────────────────────────────────────────
    salary_min, salary_max, salary_currency = parse_salary(salary_range_str)
    salary_market = derive_market(location) if not remote_eligible else "REMOTE"

    # ── 5. Derive role_type + language_list (index_query authoritative, keyword fallback) ──
    role_signals_from_query = index_query.get("role_signals") or []
    role_type = role_signals_from_query[0] if role_signals_from_query else None
    language_list = index_query.get("languages") or []

    if not role_type or not language_list:
        role_type_fallback, language_list_fallback = _infer_role_type(title, skills)
        role_type = role_type or role_type_fallback
        language_list = language_list or language_list_fallback

    # ── 7. Build source reasoning ─────────────────────────────────────────────
    top_skills = ", ".join(skills[:3]) if skills else title
    source_reasoning = (
        f"Checking Mirai's internal talent pool first (zero API cost), "
        f"then querying the pre-built talent index for {seniority} {title} candidates "
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
        language_list=language_list,
        role_type=role_type,
        index_query=index_query,
        internal_role_slugs=internal_role_slugs,
        sources=["internal_pool", "talent_index"],
        source_reasoning=source_reasoning,
        job_description=job_description,
    )

    return brief.model_dump()
