"""
"Keen to move" mobility scorer.

Computes a 0–100 likelihood-to-move score from 4 structural signals derived
from LinkedIn work history. Always returns a result — never raises.

Signal weights:
  Tenure         35%
  Career velocity 25%
  Job frequency  25%
  Company health  15%
"""

from __future__ import annotations

from datetime import datetime, date
from typing import Optional

from agent.models import (
    MobilityScore, MobilitySignals,
    TenureSignal, VelocitySignal, FrequencySignal, CompanyHealthSignal,
    LinkedInEnrichment, LinkedInPosition,
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


# ── Main entry point ──────────────────────────────────────────────────────────

def detect_move_signals(
    enrichment: LinkedInEnrichment,
    company_health_override: str | None = None,
) -> MobilityScore:
    """
    Compute mobility score from LinkedIn enrichment data.

    Always returns a MobilityScore — never raises.
    mobility_score=None means no data (distinct from 0 = "definitely not moving").
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
        return MobilityScore(
            mobility_score=None,
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
