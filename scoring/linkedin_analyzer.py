"""
LinkedIn analyzer — response parsing, mobility scoring, and career signal derivation.

Public entry points:
  parse_harvestapi_response()  — normalise raw Apify harvestapi payload → (enrichment, about_text)
  detect_move_signals()        — 0-100 "keen to move" score from work history
  compute_career_signals()     — structured career metadata from enrichment + about text

Signal weights for mobility:
  Tenure         35%
  Career velocity 25%
  Job frequency  25%
  Company health  15%
"""

from __future__ import annotations

import re
from datetime import datetime, date
from typing import Optional

from agent.models import (
    MobilityScore, MobilitySignals,
    TenureSignal, VelocitySignal, FrequencySignal, CompanyHealthSignal,
    LinkedInEnrichment, LinkedInEducation, LinkedInPosition,
    CareerSignals,
)


def _parse_date(date_str: str | None) -> date | None:
    if not date_str:
        return None
    for fmt in ("%Y-%m", "%Y-%m-%d", "%Y"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None


def _months_between(start: date, end: date) -> int:
    return max(0, (end.year - start.year) * 12 + (end.month - start.month))


# ── Harvestapi response parser ────────────────────────────────────────────────

_MONTH_MAP = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def _fmt_date(d: dict) -> Optional[str]:
    if not d or not d.get("year"):
        return None
    m = d.get("month", "")
    m_str = _MONTH_MAP.get(str(m).lower()[:3], str(m).zfill(2) if str(m).isdigit() else "01")
    return f"{d['year']}-{m_str}"


def parse_harvestapi_response(github_username: str, data: dict) -> tuple[LinkedInEnrichment, str]:
    """Normalise a harvestapi linkedin-profile-scraper payload into (enrichment, about_text).

    harvestapi field mapping:
      firstName + lastName  → full_name
      headline              → current_title fallback
      location.linkedinText → location
      experience[]          → positions (endDate.text == "Present" → is_current)
      education[]           → education (school + degree + year only — D3)
      openToWork            → open_to_work
      about / summary       → about_text (returned separately, not stored — D3)

    Returns:
        (enrichment, about_text) — about_text feeds compute_career_signals() only.
    """
    positions = []
    for pos in data.get("experience", []):
        start = pos.get("startDate") or {}
        end = pos.get("endDate") or {}

        start_str = _fmt_date(start)
        is_current = str(end.get("text", "")).strip().lower() == "present"
        end_str = None if is_current else _fmt_date(end)

        positions.append(LinkedInPosition(
            title=pos.get("position", ""),
            company=pos.get("companyName", ""),
            start_date=start_str,
            end_date=end_str,
            is_current=is_current,
        ))

    current = next((p for p in positions if p.is_current), None)

    first = data.get("firstName", "") or ""
    last = data.get("lastName", "") or ""
    full_name = f"{first} {last}".strip() or None

    location_raw = data.get("location") or {}
    if isinstance(location_raw, dict):
        location = location_raw.get("linkedinText") or location_raw.get("text")
    else:
        location = location_raw or None

    # Education — school + degree + year only (D3: compact to save context window)
    education = []
    for edu in data.get("education", []):
        end_date = edu.get("endDate") or {}
        year = end_date.get("year") if isinstance(end_date, dict) else None
        education.append(LinkedInEducation(
            school=edu.get("schoolName") or edu.get("school"),
            degree=edu.get("degreeName") or edu.get("degree"),
            year=int(year) if year else None,
        ))

    # Languages spoken — name only, strip proficiency levels
    languages_spoken: list[str] = []
    for lang in data.get("languages", []):
        if isinstance(lang, dict):
            name = lang.get("name") or lang.get("language") or ""
            if name:
                languages_spoken.append(name)
        elif isinstance(lang, str) and lang:
            languages_spoken.append(lang)

    # about text — extracted for scoring only, NOT stored in the model (D3)
    about_text: str = data.get("about") or data.get("summary") or data.get("description") or ""

    open_to_work: bool = bool(data.get("openToWork") or data.get("open_to_work") or False)

    from datetime import timezone
    enrichment = LinkedInEnrichment(
        github_username=github_username,
        linkedin_url=data.get("linkedinUrl") or data.get("url"),
        full_name=full_name,
        current_title=current.title if current else data.get("headline"),
        current_company=current.company if current else None,
        location=location,
        positions=positions,
        education=education,
        languages_spoken=languages_spoken,
        open_to_work=open_to_work,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )
    return enrichment, about_text


# ── Signal 1: Tenure (35%) ────────────────────────────────────────────────────

def _tenure_signal(positions: list[LinkedInPosition]) -> TenureSignal | None:
    current = next((p for p in positions if p.is_current or not p.end_date), None)
    if not current or not current.start_date:
        return None

    start = _parse_date(current.start_date)
    if not start:
        return None

    months = _months_between(start, date.today())

    if months < 12:
        # Too recent — likely still in honeymoon period
        score = 15.0
    elif months <= 36:
        # Peak mobility window 18–36 months
        score = 25.0 + (months - 12) / 24 * 35  # ramps 25→60
    elif months <= 60:
        # Starting to settle
        score = 60.0 - (months - 36) / 24 * 20  # fades 60→40
    else:
        # Long tenure — could go either way, slight positive signal for "finally ready"
        score = 40.0

    return TenureSignal(score=round(score, 1), months=months)


# ── Signal 2: Career velocity (25%) ──────────────────────────────────────────

def _velocity_signal(positions: list[LinkedInPosition]) -> VelocitySignal | None:
    if len(positions) < 2:
        return None

    # Count distinct titles (proxy for promotions)
    titles = [p.title.lower() for p in positions if p.title]
    promotions = len(set(titles)) - 1  # unique title changes

    # Years of experience = span from first role to now
    starts = [_parse_date(p.start_date) for p in positions if p.start_date]
    starts = [s for s in starts if s]
    if not starts:
        return None

    years = _months_between(min(starts), date.today()) / 12
    if years < 1:
        return None

    velocity = promotions / years  # title changes per year

    if velocity < 0.2:
        # Stagnant — open to new opportunities signal
        score = 70.0
    elif velocity < 0.5:
        score = 55.0
    else:
        # High velocity — already getting what they want, less likely to move
        score = 35.0

    return VelocitySignal(score=round(score, 1), promotions=promotions, years=round(years, 1))


# ── Signal 3: Job change frequency (25%) ──────────────────────────────────────

def _frequency_signal(positions: list[LinkedInPosition]) -> FrequencySignal | None:
    if len(positions) < 2:
        return None

    durations: list[int] = []
    for p in positions:
        start = _parse_date(p.start_date)
        end = _parse_date(p.end_date) if p.end_date else date.today()
        if start and end:
            durations.append(_months_between(start, end))

    if not durations:
        return None

    avg_months = sum(durations) / len(durations)

    if avg_months < 12:
        # Hopper — unstable signal, slight penalty
        score = 40.0
    elif avg_months <= 36:
        # Healthy mover — most likely to move again
        score = 70.0
    elif avg_months <= 60:
        score = 55.0
    else:
        # Long stints — less frequent mover but still possible
        score = 45.0

    return FrequencySignal(score=round(score, 1), avg_months=round(avg_months, 1))


# ── Signal 4: Company health (15%) ────────────────────────────────────────────
# Placeholder — requires external enrichment (e.g. LayoffsFYI, LinkedIn headcount API)
# Returns None for now; inject enriched data via the `company_health_override` param.

def _company_health_signal(
    current_company: str | None,
    company_health_override: str | None = None,
) -> CompanyHealthSignal | None:
    if not company_health_override:
        return None

    signal_map = {
        "layoffs": ("layoffs_detected", 85.0),
        "headcount_decline": ("headcount_decline", 70.0),
        "funding_freeze": ("funding_freeze", 65.0),
        "healthy": ("healthy", 20.0),
    }

    key = company_health_override.lower()
    for pattern, (signal, score) in signal_map.items():
        if pattern in key:
            return CompanyHealthSignal(score=score, signal=signal)

    return None


# ── Mobility: main entry point ────────────────────────────────────────────────

def detect_move_signals(
    enrichment: LinkedInEnrichment,
    company_health_override: str | None = None,
) -> MobilityScore:
    """
    Compute mobility score from LinkedIn enrichment data.

    Always returns a MobilityScore — never raises.
    mobility_score=None means no data (distinct from 0 = "definitely not moving").

    D5: if enrichment.open_to_work is True, mobility_score is floored at 85.
    """
    positions = enrichment.positions or []

    tenure = _tenure_signal(positions)
    velocity = _velocity_signal(positions)
    frequency = _frequency_signal(positions)
    company = _company_health_signal(enrichment.current_company, company_health_override)

    signals_present = [s for s in [tenure, velocity, frequency, company] if s is not None]
    total_signals = 4
    completeness = len(signals_present) / total_signals

    missing = []
    if tenure is None:
        missing.append("tenure")
    if velocity is None:
        missing.append("velocity")
    if frequency is None:
        missing.append("frequency")
    if company is None:
        missing.append("company_health")

    if not signals_present:
        # openToWork hard floor applies even with no structural data
        raw_score = None
        if enrichment.open_to_work:
            raw_score = 85
        return MobilityScore(
            mobility_score=raw_score,
            data_completeness=0.0,
            missing_signals=missing,
            signals=MobilitySignals(),
        )

    # Weighted average of available signals (normalize weights to sum to 1)
    weights = {"tenure": 0.35, "velocity": 0.25, "frequency": 0.25, "company_health": 0.15}
    available_weight = sum(
        w for k, w in weights.items() if k not in missing
    )

    score = 0.0
    if tenure:
        score += tenure.score * (weights["tenure"] / available_weight)
    if velocity:
        score += velocity.score * (weights["velocity"] / available_weight)
    if frequency:
        score += frequency.score * (weights["frequency"] / available_weight)
    if company:
        score += company.score * (weights["company_health"] / available_weight)

    # D5: openToWork hard floor
    if enrichment.open_to_work:
        score = max(score, 85.0)

    return MobilityScore(
        mobility_score=round(score),
        data_completeness=round(completeness, 2),
        missing_signals=missing,
        signals=MobilitySignals(
            tenure=tenure,
            velocity=velocity,
            frequency=frequency,
            company_health=company,
        ),
    )


# ── Career signals ────────────────────────────────────────────────────────────

# Checked in reverse priority order so higher levels win on ambiguous titles
_SENIORITY_LEVELS = ["junior", "mid", "senior", "staff", "lead", "exec"]

_SENIORITY_KEYWORDS: dict[str, list[str]] = {
    "exec": ["cto", "ceo", "cpo", "coo", "vp ", "vice president", "chief", "founder", "co-founder", "cofounder", "owner", "partner"],
    "lead": ["lead engineer", "principal", "director", "head of", "engineering manager", " em,", "architect"],
    "staff": ["staff engineer", "staff software"],
    "senior": ["senior", "sr.", "sr "],
    "junior": ["junior", "jr.", "jr ", "associate engineer", "entry level", "intern"],
}

# Regex for quantified outcomes: impact verb near a number/metric
_IMPACT_VERB_RE = re.compile(
    r"\b(increas|decreas|reduc|improv|grew|grown|scal|sav|cut|boost|accelerat|"
    r"deliver|launch|ship(?:ped)?|led|built|migrat|optimiz|achiev|generat|rais|hired?)\w*\b",
    re.IGNORECASE,
)

_METRIC_RE = re.compile(
    r"\b\d[\d,.]*\s*[%x×]"           # 40%, 3x, 2×
    r"|\$\s*\d[\d,.]*[kmb]?\b"       # $5M, $200k
    r"|\b\d+[kmb]\b"                 # 10k, 2m, 5b
    r"|\b\d+\s*(?:ms|seconds?|hours?|days?|weeks?|months?)\b"
    r"|\b\d+\s*(?:users?|customers?|engineers?|employees?|services?|repos?|requests?|"
    r"teams?|members?|deployments?|releases?)\b",
    re.IGNORECASE,
)


def _infer_seniority(title: str) -> str:
    t = title.lower()
    for level, keywords in _SENIORITY_KEYWORDS.items():
        if any(k in t for k in keywords):
            return level
    return "mid"


def _has_quantified_outcomes(texts: list[str]) -> bool:
    """Return True if any text contains an impact verb within 120 chars of a metric."""
    combined = " ".join(t for t in texts if t)
    if not _IMPACT_VERB_RE.search(combined):
        return False
    return bool(_METRIC_RE.search(combined))


def compute_career_signals(
    enrichment: LinkedInEnrichment,
    about_text: str | None = None,
) -> CareerSignals:
    """
    Derive structured career signals from LinkedIn enrichment + optional about text.

    about_text is used for has_quantified_outcomes only — it is NOT stored in
    LinkedInEnrichment so it never inflates the context window returned to the agent.

    Always returns a CareerSignals — never raises.
    """
    positions = enrichment.positions or []

    # years_of_experience: span from earliest start date to today
    years_of_experience: Optional[float] = None
    starts = [_parse_date(p.start_date) for p in positions if p.start_date]
    starts = [s for s in starts if s]
    if starts:
        months = _months_between(min(starts), date.today())
        years_of_experience = round(months / 12, 1)

    # seniority_level: inferred from current title
    seniority_level: Optional[str] = None
    if enrichment.current_title:
        seniority_level = _infer_seniority(enrichment.current_title)

    # career_trajectory: compare early career vs recent career seniority.
    # positions are newest-first (LinkedIn ordering).
    # For 6+ positions: average the newest 3 vs oldest 3.
    # For 3–5: compare newest 1 vs oldest 1 (avoids same-slice overlap).
    career_trajectory: Optional[str] = None
    if len(positions) >= 3:
        level_idx = {v: i for i, v in enumerate(_SENIORITY_LEVELS)}

        def avg_level(ps: list[LinkedInPosition]) -> float:
            levels = [level_idx[_infer_seniority(p.title)] for p in ps if p.title]
            return sum(levels) / len(levels) if levels else 0.0

        if len(positions) >= 6:
            recent_avg = avg_level(positions[:3])
            early_avg = avg_level(positions[-3:])
        else:
            recent_avg = avg_level(positions[:1])
            early_avg = avg_level(positions[-1:])

        if recent_avg > early_avg + 0.5:
            career_trajectory = "ascending"
        elif recent_avg < early_avg - 0.5:
            career_trajectory = "descending"
        else:
            career_trajectory = "lateral"
    elif positions:
        career_trajectory = "insufficient_data"

    # has_quantified_outcomes: regex over about text (scoring-only, not stored)
    has_quantified = _has_quantified_outcomes([about_text or ""])

    return CareerSignals(
        years_of_experience=years_of_experience,
        seniority_level=seniority_level,
        career_trajectory=career_trajectory,
        has_quantified_outcomes=has_quantified,
    )
