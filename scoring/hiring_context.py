"""
Python port of gitcheck-webapp/src/lib/agent/hiringContext.ts

Reweights talent scores based on company stage.
"""

from __future__ import annotations

from agent.models import TalentScore, TalentScoreBreakdown

# ── Context weight profiles ────────────────────────────────────────────────────

CONTEXT_WEIGHTS = {
    "startup_early": {
        "tech_stack": 0.30,
        "open_source": 0.12,
        "consistency": 0.38,
        "collaboration": 0.12,
        "presentation": 0.08,
        "prestige_ceiling_followers": 400,
        "prestige_ceiling_stars": 150,
    },
    "startup_growth": {
        "tech_stack": 0.25,
        "open_source": 0.20,
        "consistency": 0.25,
        "collaboration": 0.22,
        "presentation": 0.08,
        "prestige_ceiling_followers": 2000,
        "prestige_ceiling_stars": 800,
    },
    "enterprise": {
        "tech_stack": 0.30,
        "open_source": 0.25,
        "consistency": 0.20,
        "collaboration": 0.15,
        "presentation": 0.10,
        "prestige_ceiling_followers": None,
        "prestige_ceiling_stars": None,
    },
}


def apply_hiring_context(
    talent_score: TalentScore,
    context: str,
    target_location: str | None = None,
    candidate_location: str | None = None,
    candidate_followers: int = 0,
    candidate_stars: int = 0,
) -> TalentScore:
    """
    Apply hiring-stage-aware reweighting to a TalentScore.

    context: "startup_early" | "startup_growth" | "enterprise"
    """
    weights = CONTEXT_WEIGHTS.get(context, CONTEXT_WEIGHTS["enterprise"])
    b = talent_score.breakdown

    context_score = (
        b.tech_stack.score * weights["tech_stack"] +
        b.open_source.score * weights["open_source"] +
        b.consistency.score * weights["consistency"] +
        b.collaboration.score * weights["collaboration"] +
        b.presentation.score * weights["presentation"]
    )

    # Prestige penalty for over-qualified candidates
    prestige_penalty = 0.0
    ceil_followers = weights.get("prestige_ceiling_followers")
    ceil_stars = weights.get("prestige_ceiling_stars")

    if ceil_followers and candidate_followers > ceil_followers:
        prestige_penalty += 5.0
    if ceil_stars and candidate_stars > ceil_stars:
        prestige_penalty += 5.0

    context_score = max(0.0, context_score - prestige_penalty)

    # Location fit adjustment (±15 pts)
    location_fit = _score_location(target_location, candidate_location)
    if location_fit is not None:
        location_adjustment = (location_fit - 50) / 50 * 15  # -15 to +15
        context_score = max(0.0, min(100.0, context_score + location_adjustment))

    return talent_score.model_copy(update={
        "context_score": round(context_score, 1),
        "location_fit": location_fit,
        "prestige_penalty": prestige_penalty,
        "hiring_context": context,
    })


# City → ISO country code (lowercase) for cities commonly used as bare target strings.
# Expands same-country matching when the target has no ", Country" suffix.
_CITY_COUNTRY: dict[str, str] = {
    "milan": "it", "milano": "it",
    "rome": "it", "roma": "it",
    "turin": "it", "torino": "it",
    "florence": "it", "firenze": "it",
    "bologna": "it", "naples": "it", "napoli": "it",
    "genoa": "it", "palermo": "it", "bari": "it",
    "zurich": "ch", "zürich": "ch", "zuerich": "ch",
    "geneva": "ch", "genève": "ch", "geneve": "ch",
    "basel": "ch", "bern": "ch", "lausanne": "ch", "lugano": "ch",
    "paris": "fr", "lyon": "fr", "marseille": "fr",
    "berlin": "de", "munich": "de", "münchen": "de", "hamburg": "de",
    "madrid": "es", "barcelona": "es",
    "amsterdam": "nl", "rotterdam": "nl",
    "london": "gb", "manchester": "gb", "edinburgh": "gb",
    "new york": "us", "san francisco": "us", "los angeles": "us",
    "chicago": "us", "seattle": "us", "austin": "us", "boston": "us",
    # India
    "mumbai": "in", "bangalore": "in", "bengaluru": "in", "delhi": "in",
    "new delhi": "in", "hyderabad": "in", "pune": "in", "chennai": "in",
    "gurgaon": "in", "gurugram": "in", "noida": "in", "kolkata": "in",
    # China
    "beijing": "cn", "shanghai": "cn", "shenzhen": "cn", "guangzhou": "cn",
    # Others
    "toronto": "ca", "vancouver": "ca", "montreal": "ca",
    "sydney": "au", "melbourne": "au",
    "tokyo": "jp", "osaka": "jp",
    "seoul": "kr", "busan": "kr",
    "singapore": "sg",
    "dubai": "ae", "abu dhabi": "ae",
    "moscow": "ru", "saint petersburg": "ru",
    "warsaw": "pl", "krakow": "pl",
    "istanbul": "tr", "ankara": "tr",
    "tel aviv": "il",
    "sao paulo": "br", "rio de janeiro": "br",
    "buenos aires": "ar",
    "bogota": "co",
}

# Country name/suffix → ISO code
_COUNTRY_ALIAS: dict[str, str] = {
    "italy": "it", "italia": "it",
    "switzerland": "ch", "schweiz": "ch", "svizzera": "ch",
    "france": "fr", "germany": "de", "deutschland": "de",
    "spain": "es", "españa": "es",
    "netherlands": "nl", "holland": "nl",
    "uk": "gb", "united kingdom": "gb", "england": "gb",
    "united states": "us", "usa": "us",
    "india": "in",
    "china": "cn",
    "japan": "jp",
    "brazil": "br", "brasil": "br",
    "canada": "ca",
    "australia": "au",
    "russia": "ru", "россия": "ru",
    "ukraine": "ua",
    "poland": "pl", "polska": "pl",
    "portugal": "pt",
    "romania": "ro",
    "hungary": "hu",
    "czech republic": "cz", "czechia": "cz",
    "austria": "at", "österreich": "at",
    "sweden": "se", "sverige": "se",
    "norway": "no", "norge": "no",
    "denmark": "dk", "danmark": "dk",
    "finland": "fi",
    "belgium": "be", "belgique": "be",
    "turkey": "tr", "türkiye": "tr",
    "israel": "il",
    "singapore": "sg",
    "indonesia": "id",
    "pakistan": "pk",
    "bangladesh": "bd",
    "nigeria": "ng",
    "egypt": "eg",
    "south africa": "za",
    "mexico": "mx", "méxico": "mx",
    "argentina": "ar",
    "colombia": "co",
    "chile": "cl",
    "south korea": "kr", "korea": "kr",
    "taiwan": "tw",
    "vietnam": "vn", "viet nam": "vn",
    "thailand": "th",
    "malaysia": "my",
    "philippines": "ph",
    "new zealand": "nz",
    "ireland": "ie",
    "greece": "gr",
    "serbia": "rs",
    "croatia": "hr",
    "slovakia": "sk",
    "bulgaria": "bg",
    "latvia": "lv",
    "lithuania": "lt",
    "estonia": "ee",
}


def _to_country_code(s: str) -> str | None:
    """Return ISO code for a location token (city name or country name), or None."""
    s = s.lower().strip()
    return _CITY_COUNTRY.get(s) or _COUNTRY_ALIAS.get(s)


def _score_location(target: str | None, candidate: str | None) -> float | None:
    if not target:
        return None
    if not candidate:
        return 40.0   # location unknown

    t = target.lower().strip()
    c = candidate.lower().strip()

    if t == c:
        return 100.0
    if t in c or c in t:
        return 90.0

    # Same country heuristic — try last-comma token, then city lookup.
    t_tokens = [tok.strip() for tok in t.split(",")]
    c_tokens = [tok.strip() for tok in c.split(",")]

    t_country = t_tokens[-1] if len(t_tokens) > 1 else None
    c_country = c_tokens[-1] if len(c_tokens) > 1 else None

    # Compare extracted country tokens
    if t_country and c_country and t_country == c_country:
        return 70.0

    # Fall back to city→ISO lookup for bare city targets (e.g. "Milan" vs "Rome, Italy")
    t_iso = _to_country_code(t_tokens[0]) or (t_country and _to_country_code(t_country))
    c_iso = _to_country_code(c_tokens[0]) or (c_country and _to_country_code(c_country))
    if t_iso and c_iso:
        if t_iso == c_iso:
            return 70.0  # same country, different city
        return 10.0      # confirmed different country

    # Can't determine — return None so hard-gates don't fire on ambiguous data
    return None
