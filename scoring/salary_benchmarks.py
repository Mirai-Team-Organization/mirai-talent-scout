"""
Salary market benchmarks (EUR equivalent) for junior/mid/senior by region.

Three tiers — add city-level detail only when benchmarks actually diverge.
Rates are conservative midpoints; used for MATCH/ABOVE_RANGE/BELOW_RANGE verdicts.
"""

from __future__ import annotations

# (min_eur, max_eur) per seniority tier
BENCHMARKS: dict[str, dict[str, tuple[int, int]]] = {
    "EU": {
        "junior": (35_000, 55_000),
        "mid":    (55_000, 85_000),
        "senior": (85_000, 130_000),
        "lead":   (100_000, 150_000),
    },
    "US": {
        "junior": (73_600, 101_200),
        "mid":    (101_200, 147_200),
        "senior": (147_200, 202_400),
        "lead":   (160_000, 220_000),
    },
    "REMOTE": {
        "junior": (35_000, 55_000),
        "mid":    (55_000, 85_000),
        "senior": (85_000, 130_000),
        "lead":   (100_000, 150_000),
    },
}

# Fixed FX rates (not live — update quarterly)
FX_TO_EUR: dict[str, float] = {
    "EUR": 1.0,
    "USD": 0.92,
    "GBP": 1.16,
}


def derive_market(location: str | None) -> str:
    """
    Derive market tier from a location string.
    Returns "EU" | "US" | "REMOTE".
    """
    if not location:
        return "EU"
    loc = location.lower()
    if "remote" in loc:
        return "REMOTE"
    if any(kw in loc for kw in ("united states", "us", "san francisco", "new york", "nyc", "seattle", "austin")):
        return "US"
    return "EU"


def benchmark_range(market: str, seniority: str) -> tuple[int, int] | None:
    """
    Return (min_eur, max_eur) for a market + seniority pair.
    Returns None if seniority is unrecognised.
    """
    tier = BENCHMARKS.get(market, BENCHMARKS["EU"])
    return tier.get(seniority)
