"""
Pydantic models for the AI Talent Scout.

These mirror the TypeScript types in gitcheck-webapp/src/types/
exactly — the parity test suite enforces that scores match within ±2 points.
"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


# ── GitHub / Talent scoring ──────────────────────────────────────────────────

class TechStackScore(BaseModel):
    score: float
    top_languages: list[str]


class OpenSourceScore(BaseModel):
    score: float
    repo_count: int
    commit_count: int


class ConsistencyScore(BaseModel):
    score: float
    active_days: int
    streak: int


class CollaborationScore(BaseModel):
    score: float
    prs: int
    reviews: int


class PresentationScore(BaseModel):
    score: float


class TalentScoreBreakdown(BaseModel):
    tech_stack: TechStackScore
    open_source: OpenSourceScore
    consistency: ConsistencyScore
    collaboration: CollaborationScore
    presentation: PresentationScore


class TalentScore(BaseModel):
    overall: float                          # 0–100
    grade: str                             # S / A+ / A / A- / B+ / B / B- / C+ / C
    breakdown: TalentScoreBreakdown
    context_score: Optional[float] = None  # set when hiring_context provided
    location_fit: Optional[float] = None   # 0–100
    prestige_penalty: Optional[float] = None
    hiring_context: Optional[str] = None


# ── Talent Brief (job-posting-aware search context) ──────────────────────────

class TalentBrief(BaseModel):
    """
    Structured search intent derived from a company_job_postings row.
    Built once by build_talent_brief(); consumed by search_internal_pool(),
    search_github(), and score_candidate_rubric().
    """
    job_posting_id: str
    title: str
    seniority: str                        # junior | mid | senior | lead
    location: str
    remote_eligible: bool
    skills: list[str]
    hiring_rubric: dict                   # raw JSONB — may be {}
    rubric_text: str                      # LLM-flattened SEARCH line for GitHub query
    dealbreaker_text: str                 # comma-separated dealbreakers; "" if none
    role_weights: dict                    # from role_scoring_config; keys = dimension names
    salary_min: Optional[float] = None   # EUR equivalent
    salary_max: Optional[float] = None
    salary_currency: Optional[str] = None
    salary_market: Optional[str] = None  # EU | US | REMOTE
    github_query: str = ""               # pre-translated GitHub search syntax
    language_list: list[str] = Field(default_factory=list)  # primary languages for index search
    role_type: Optional[str] = None     # ml_engineer_signal | devops_signal | fullstack_signal | backend_signal | fde_signal
    index_query: dict = Field(default_factory=dict)  # structured talent_index query params
    sources: list[str] = Field(default_factory=lambda: ["internal_pool", "talent_index"])
    source_reasoning: str = ""
    job_description: str = ""  # raw job posting description, used by scoring LLM


# ── Mobility / "keen to move" scoring ───────────────────────────────────────

class TenureSignal(BaseModel):
    score: float
    months: int


class VelocitySignal(BaseModel):
    score: float
    promotions: int
    years: float


class FrequencySignal(BaseModel):
    score: float
    avg_months: float


class CompanyHealthSignal(BaseModel):
    score: float
    signal: str   # e.g. "layoffs_detected", "headcount_decline", "healthy"


class MobilitySignals(BaseModel):
    tenure: Optional[TenureSignal] = None
    velocity: Optional[VelocitySignal] = None
    frequency: Optional[FrequencySignal] = None
    company_health: Optional[CompanyHealthSignal] = None


class MobilityScore(BaseModel):
    # None means "no data" — explicitly distinct from 0 ("definitely not moving")
    mobility_score: Optional[int] = None   # 0–100 or None if no data
    data_completeness: float = 0.0         # 0.0–1.0 (fraction of signals with data)
    missing_signals: list[str] = Field(default_factory=list)
    signals: MobilitySignals = Field(default_factory=MobilitySignals)


# ── LinkedIn enrichment ──────────────────────────────────────────────────────

class LinkedInPosition(BaseModel):
    title: str
    company: str
    start_date: Optional[str] = None   # ISO date string "YYYY-MM"
    end_date: Optional[str] = None     # None = current role
    is_current: bool = False
    description: Optional[str] = None  # role description / responsibilities (truncated to 300 chars)


class LinkedInEducation(BaseModel):
    school: Optional[str] = None
    degree: Optional[str] = None
    year: Optional[int] = None         # graduation year


class LinkedInEnrichment(BaseModel):
    github_username: str
    linkedin_url: Optional[str] = None
    full_name: Optional[str] = None
    current_title: Optional[str] = None
    current_company: Optional[str] = None
    location: Optional[str] = None
    positions: list[LinkedInPosition] = Field(default_factory=list)
    education: list[LinkedInEducation] = Field(default_factory=list)
    languages_spoken: list[str] = Field(default_factory=list)  # e.g. ["English", "German"]
    open_to_work: bool = False
    fetched_at: Optional[str] = None   # ISO datetime
    source: str = "harvestapi"


# ── Career signals (derived from LinkedIn history) ───────────────────────────

class CareerSignals(BaseModel):
    years_of_experience: Optional[float] = None
    seniority_level: Optional[str] = None      # junior/mid/senior/staff/lead/exec
    career_trajectory: Optional[str] = None    # ascending/lateral/descending/insufficient_data
    has_quantified_outcomes: bool = False


# ── Final candidate result ───────────────────────────────────────────────────

class CandidateResult(BaseModel):
    username: str
    name: Optional[str] = None
    location: Optional[str] = None
    avatar_url: Optional[str] = None
    github_url: str
    linkedin_url: Optional[str] = None

    talent_score: TalentScore
    developer_grade: str
    top_languages: list[str]

    mobility: Optional[MobilityScore] = None         # None if LinkedIn not found
    career_signals: Optional[CareerSignals] = None  # None if LinkedIn not found

    summary: str                   # AI-generated 2-sentence recruiter pitch
    red_flags: list[str] = Field(default_factory=list)
    fit_score: Optional[int] = None   # 0–100 vs job description

    fetched_at: str                # ISO datetime


# ── Search results ───────────────────────────────────────────────────────────

class GitHubSearchResult(BaseModel):
    login: str
    name: Optional[str] = None
    location: Optional[str] = None
    avatar_url: Optional[str] = None
    followers: int = 0
    public_repos: int = 0
    bio: Optional[str] = None
    company: Optional[str] = None
    twitter_username: Optional[str] = None
    blog: Optional[str] = None


class ShortlistResult(BaseModel):
    candidates: list[CandidateResult]
    query: str
    query_translated: str
    total_found: int
    enriched: int
    errors: list[str] = Field(default_factory=list)
