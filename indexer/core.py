"""
indexer/core.py — shared GitHub fetch + Supabase upsert logic.

Used by both the Lambda handler and the local runner.

Rate limiting:
  - Tokens are rotated round-robin across calls.
  - A shared RateLimiter enforces max 5,000 calls/hr per token.
  - With N tokens the effective throughput is N × 5,000 calls/hr.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from threading import Lock

from db.client import get_supabase
from agent.tools.search_github import _github_graphql
from indexer.role_signals import compute_activity_score, infer_role_signals
from scoring.talent_scorer import GRADE_ORDER, calculate_talent_score

# ── Quality / celebrity filter constants ──────────────────────────────────────
# Minimum grade to accept into the index (C+ = overall >= 34).
# Lower than the previous B- floor (42) to widen the pool — we compensate by
# requiring a LinkedIn URL so profiles are immediately enrichable.
_MIN_GRADE = "C+"
_MIN_GRADE_ORDER = GRADE_ORDER[_MIN_GRADE]

# Reject profiles that are too famous — these people won't join an early-stage startup.
# Karpathy-level: ~90k followers, ~50k stars on single repos.
_MAX_FOLLOWERS = 20_000
_MAX_REPO_STARS = 50_000

# ── LinkedIn URL extraction from bio / websiteUrl ─────────────────────────────
_LINKEDIN_RE = re.compile(r'linkedin\.com/in/([\w\-%]+)', re.IGNORECASE)


def _extract_linkedin_url(website: str, bio: str) -> str | None:
    """Return a normalised linkedin.com/in/handle URL if found in website or bio."""
    for text in (website, bio):
        if not text:
            continue
        m = _LINKEDIN_RE.search(text)
        if m:
            handle = m.group(1).rstrip("/")
            return f"https://www.linkedin.com/in/{handle}"
    return None


# ── Top-university mention check ──────────────────────────────────────────────
# Covers the full set of top European universities used by linkedin_search.mjs,
# plus well-known abbreviations developers write in GitHub bios.
# GitHub has no formal education data — this is a best-effort bio/company signal.
_UNIVERSITY_RE = re.compile(
    r'('
    # ── Italy ────────────────────────────────────────────────────────────────
    r'politecnico\s+di\s+milano|polimi'
    r'|politecnico\s+di\s+torino|polito'
    r'|universit[aà]\s+bocconi|bocconi'
    r'|sapienza\s+university\s+of\s+rome|sapienza'
    r'|university\s+of\s+bologna|unibo'
    r'|university\s+of\s+padua|unipd'
    r'|scuola\s+normale\s+superiore'
    r'|university\s+of\s+trento|unitn'
    r'|university\s+of\s+pisa|unipi'
    # ── Switzerland ──────────────────────────────────────────────────────────
    r'|eth\s+zurich|eth\s+z[üu]rich|ethz'
    r'|epfl'
    r'|university\s+of\s+zurich|uzh'
    r'|university\s+of\s+basel'
    r'|university\s+of\s+bern|unibe'
    r'|university\s+of\s+geneva|unige'
    r'|university\s+of\s+lausanne|unil'
    # ── Germany ──────────────────────────────────────────────────────────────
    r'|technical\s+university\s+of\s+munich|tu\s+munich|tum\b|technische\s+universit[aä]t\s+m[üu]nchen'
    r'|rwth\s+aachen'
    r'|karlsruhe\s+institute\s+of\s+technology|\bkit\b'
    r'|tu\s+berlin|technische\s+universit[aä]t\s+berlin'
    r'|lmu\s+munich|ludwig\s+maximilian\s+university'
    r'|heidelberg\s+university|universit[aä]t\s+heidelberg'
    r'|university\s+of\s+stuttgart|tu\s+stuttgart'
    # ── United Kingdom ────────────────────────────────────────────────────────
    r'|university\s+of\s+oxford|oxford\s+university'
    r'|university\s+of\s+cambridge|cambridge\s+university'
    r'|imperial\s+college\s+london|imperial\s+college'
    r'|university\s+college\s+london|\bucl\b'
    r'|university\s+of\s+edinburgh'
    r'|university\s+of\s+manchester'
    # ── France ───────────────────────────────────────────────────────────────
    r'|[eé]cole\s+polytechnique'
    r'|[eé]cole\s+normale\s+sup[eé]rieure|\bens\s+paris\b|\bens\b'
    r'|centralesup[eé]lec'
    r'|t[eé]l[eé]com\s+paris'
    # ── Netherlands ───────────────────────────────────────────────────────────
    r'|delft\s+university\s+of\s+technology|tu\s+delft'
    r'|university\s+of\s+amsterdam|\buva\b'
    # ── Sweden ───────────────────────────────────────────────────────────────
    r'|kth\s+royal\s+institute|\bkth\b'
    r'|chalmers\s+university'
    # ── Denmark ──────────────────────────────────────────────────────────────
    r'|technical\s+university\s+of\s+denmark|\bdtu\b'
    # ── Austria ───────────────────────────────────────────────────────────────
    r'|tu\s+wien|vienna\s+university\s+of\s+technology|technische\s+universit[aä]t\s+wien'
    # ── Spain ────────────────────────────────────────────────────────────────
    r'|universidad\s+polit[eé]cnica\s+de\s+madrid|\bupm\b'
    r'|universitat\s+polit[eè]cnica\s+de\s+catalunya|\bupc\b'
    # ── Belgium ───────────────────────────────────────────────────────────────
    r'|ku\s+leuven|katholieke\s+universiteit\s+leuven'
    r'|universit[eé]\s+libre\s+de\s+bruxelles|\bulb\b'
    # ── Portugal ─────────────────────────────────────────────────────────────
    r'|instituto\s+superior\s+t[eé]cnico|t[eé]cnico\s+lisboa|\bist\s+lisbon\b'
    # ── Finland ───────────────────────────────────────────────────────────────
    r'|aalto\s+university'
    r'|university\s+of\s+helsinki'
    # ── Czech Republic ────────────────────────────────────────────────────────
    r'|czech\s+technical\s+university|\bctu\s+prague\b'
    r')',
    re.IGNORECASE,
)

# ── GitHub account age — seniority proxy ─────────────────────────────────────
# Accounts created in 2015 or earlier are likely 35+ years old (GitHub launched
# in 2008; early adopters tend to be more senior). Rough heuristic — not applied
# if the field is missing.
_MAX_ACCOUNT_AGE_YEAR = 2015  # inclusive upper bound to reject


def _mentions_top_university(bio: str, company: str) -> bool:
    """Return True if bio or company text references one of the indexed top universities."""
    for text in (bio, company):
        if text and _UNIVERSITY_RE.search(text):
            return True
    return False

# ── Location → country/city mapping ──────────────────────────────────────────

_CITY_MAP: dict[str, tuple[str, str]] = {
    # (country_code, canonical_city)
    "milan":       ("IT", "Milan"),
    "milano":      ("IT", "Milan"),
    "rome":        ("IT", "Rome"),
    "roma":        ("IT", "Rome"),
    "turin":       ("IT", "Turin"),
    "torino":      ("IT", "Turin"),
    "florence":    ("IT", "Florence"),
    "firenze":     ("IT", "Florence"),
    "brescia":     ("IT", "Brescia"),
    "bologna":     ("IT", "Bologna"),
    "naples":      ("IT", "Naples"),
    "napoli":      ("IT", "Naples"),
    "genoa":       ("IT", "Genoa"),
    "palermo":     ("IT", "Palermo"),
    "bari":        ("IT", "Bari"),
    "verona":        ("IT", "Verona"),
    "padova":        ("IT", "Padova"),
    "padua":         ("IT", "Padova"),
    "venice":        ("IT", "Venice"),
    "venezia":       ("IT", "Venice"),
    "trento":        ("IT", "Trento"),
    "bergamo":       ("IT", "Bergamo"),
    "modena":        ("IT", "Modena"),
    "parma":         ("IT", "Parma"),
    "reggio emilia": ("IT", "Reggio Emilia"),
    "catania":       ("IT", "Catania"),
    "trieste":       ("IT", "Trieste"),
    "italy":         ("IT", None),
    "italia":        ("IT", None),
    "zurich":      ("CH", "Zurich"),
    "zuerich":     ("CH", "Zurich"),
    "zürich":      ("CH", "Zurich"),
    "geneva":      ("CH", "Geneva"),
    "geneve":      ("CH", "Geneva"),
    "genève":      ("CH", "Geneva"),
    "basel":       ("CH", "Basel"),
    "bern":        ("CH", "Bern"),
    "lausanne":    ("CH", "Lausanne"),
    "lugano":      ("CH", "Lugano"),
    "switzerland": ("CH", None),
    "schweiz":     ("CH", None),
    "svizzera":    ("CH", None),
}


def infer_location(raw: str | None) -> tuple[str | None, str | None]:
    """
    Return (country_code, city) from a raw GitHub location string.
    Returns (None, None) if location is not IT or CH.
    """
    if not raw:
        return None, None
    lower = raw.lower().strip()
    # Direct match
    for key, (cc, city) in _CITY_MAP.items():
        if key in lower:
            return cc, city
    return None, None


# ── Lambda deadline helper ────────────────────────────────────────────────────
# Defined here (not in handler.py) so shards can import it without a circular dep.

_DEFAULT_TIMEOUT_S = int(os.environ.get("FUNCTION_TIMEOUT", "900"))
_BUFFER_S = 90   # stop fetching 90s before timeout to allow final DB writes


def _deadline(context) -> float:
    """Return the wall-clock time (time.monotonic()) at which we must stop."""
    if context is not None and hasattr(context, "get_remaining_time_in_millis"):
        remaining_ms = context.get_remaining_time_in_millis()
        return time.monotonic() + (remaining_ms / 1000) - _BUFFER_S
    return time.monotonic() + _DEFAULT_TIMEOUT_S - _BUFFER_S


# ── Token pool with per-token rate limiting ───────────────────────────────────

class TokenPool:
    """
    Round-robin token pool with per-token rate limiting.
    Enforces max `rate_per_hour` calls per token per hour.
    Thread-safe (used by the local runner which may be single-threaded, but safe).
    """

    def __init__(self, tokens: list[str], rate_per_hour: int = 5000):
        self._tokens = tokens
        self._rate = rate_per_hour
        self._counts: list[int] = [0] * len(tokens)
        self._window_start = time.monotonic()
        self._idx = 0
        self._lock = Lock()

    def acquire(self) -> tuple[str, int]:
        """
        Return (token, token_index). Sleeps if all tokens are rate-limited.
        """
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._window_start
            if elapsed >= 3600:
                self._counts = [0] * len(self._tokens)
                self._window_start = now
                elapsed = 0

            # Find a token with remaining quota
            for _ in range(len(self._tokens)):
                idx = self._idx % len(self._tokens)
                if self._counts[idx] < self._rate:
                    self._counts[idx] += 1
                    self._idx += 1
                    return self._tokens[idx], idx

                self._idx += 1

            # All tokens exhausted — sleep until window resets
            sleep_secs = 3600 - elapsed + 1
            print(f"[TokenPool] All tokens rate-limited. Sleeping {sleep_secs:.0f}s...")
        time.sleep(sleep_secs)
        return self.acquire()

    @classmethod
    def from_env(cls) -> "TokenPool":
        raw = os.environ.get("GITHUB_TOKENS", "")
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        if not tokens:
            raise ValueError("GITHUB_TOKENS env var not set")
        print(f"[TokenPool] {len(tokens)} token(s) loaded.")
        return cls(tokens)


# ── GitHub REST search (users) ────────────────────────────────────────────────

def github_search_users_page(
    location: str,
    language: str,
    page: int,
    token: str,
) -> list[str]:
    """
    Fetch one page (up to 100) of GitHub user search results.
    Returns list of login strings.
    """
    # followers:<_MAX_FOLLOWERS filters out celebrities at the API level — no wasted GraphQL calls.
    # followers:>5 ensures at least some traction (not an empty/bot account).
    q = urllib.parse.quote(
        f"location:{location} language:{language} "
        f"followers:>5 followers:<{_MAX_FOLLOWERS}"
    )
    url = (
        f"https://api.github.com/search/users"
        f"?q={q}&per_page=100&page={page}&sort=followers&order=desc"
    )
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "mirai-talent-indexer/1.0",
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
                return [u["login"] for u in data.get("items", [])]
        except urllib.error.HTTPError as e:
            if e.code == 422:
                return []  # GitHub rejects page > 10
            if e.code in (429, 403) and attempt < 2:
                wait = 60 * (attempt + 1)  # 60s, then 120s — search API resets per minute
                print(f"[search] 403/429 on page {page}, waiting {wait}s...")
                time.sleep(wait)
                continue
            raise
    return []


# ── GraphQL profile fetcher ───────────────────────────────────────────────────
# Inlined (not derived from search_github.PROFILE_QUERY) to include repositoryTopics
# needed by role_signals inference. Fragile string-replace approach dropped.

_PROFILE_QUERY_WITH_TOPICS = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    login name bio email location company websiteUrl twitterUsername createdAt
    followers { totalCount }
    repositories(first: 100, isFork: false, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        name stargazerCount
        repositoryTopics(first: 10) { nodes { topic { name } } }
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name } }
        }
      }
    }
    pinnedItems(first: 6, types: [REPOSITORY]) {
      nodes {
        ... on Repository { name description stargazerCount url }
      }
    }
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      totalIssueContributions
      totalPullRequestContributions
      totalPullRequestReviewContributions
      totalRepositoriesWithContributedCommits
      contributionCalendar {
        totalContributions
      }
      commitContributionsByRepository(maxRepositories: 100) {
        repository { name owner { login } }
        contributions(first: 1) { totalCount }
      }
      pullRequestContributions(first: 100) {
        nodes { pullRequest { repository { name owner { login } } } }
      }
      pullRequestReviewContributions(first: 100) {
        nodes { pullRequest { repository { name owner { login } } } }
      }
    }
  }
}
"""


def fetch_profile(login: str, token: str) -> dict | None:
    """
    Fetch full GitHub profile via GraphQL. Returns parsed profile dict or None.
    """
    now = datetime.now(timezone.utc)
    from_date = (now - timedelta(days=365)).isoformat()
    try:
        result = _github_graphql(
            _PROFILE_QUERY_WITH_TOPICS,
            {"login": login, "from": from_date, "to": now.isoformat()},
            token,
        )
        user = result.get("data", {}).get("user")
        if not user:
            return None
        return _parse_profile_extended(user)
    except Exception as e:
        print(f"[fetch_profile] {login}: {e}")
        return None


def _parse_profile_extended(user: dict) -> dict:
    """
    Extended _parse_profile that preserves github_data for role signal inference.
    Keeps the same output shape as search_github._parse_profile but adds github_data.
    """
    from agent.tools.search_github import _parse_profile
    profile = _parse_profile(user)
    # Attach raw github_data so role_signals can inspect repo topics
    profile["github_data"] = user
    return profile


# ── Supabase upsert ───────────────────────────────────────────────────────────

def upsert_profile(profile: dict, source: str = "github_broad", source_details: dict | None = None) -> bool:
    """
    Upsert a profile into talent_index if it passes all gates:
      1. GitHub account not too old (proxy for ≤35 years old)
      2. LinkedIn URL present in websiteUrl or bio
      3. Bio or company mentions one of the indexed top European universities
      4. Effective score C+ or above (university affiliation gives +5 bonus
         so strong-background/lower-activity profiles aren't unfairly penalised)
    Returns True if upserted, False if skipped.
    """
    sb = get_supabase()
    p = profile.get("profile", {})
    login = p.get("login", "")
    if not login:
        return False

    # ── Seniority proxy: skip accounts created 2015 or earlier ───────────────
    created_at = p.get("createdAt") or ""
    if created_at:
        try:
            if int(created_at[:4]) <= _MAX_ACCOUNT_AGE_YEAR:
                return False
        except (ValueError, TypeError):
            pass  # unparseable date — allow through

    location_raw = p.get("location")
    country_code, city = infer_location(location_raw)

    role_signals = infer_role_signals(profile)
    activity_score = compute_activity_score(profile)
    max_stars = max(
        (r.get("stargazerCount", 0) for r in (profile.get("github_data", {}).get("repositories", {}).get("nodes") or [])),
        default=0,
    )

    # Celebrity filter: gate on repo star count
    if max_stars > _MAX_REPO_STARS:
        return False

    # Extract contact fields from profile
    email = p.get("email") or None
    website = (p.get("websiteUrl") or "").strip()
    bio = (p.get("bio") or "").strip()
    linkedin_url = _extract_linkedin_url(website, bio)

    # Require a LinkedIn URL — profiles without one can't be enriched easily
    if not linkedin_url:
        return False

    # Top-university mention in bio/company — soft signal, not a hard filter
    company = (p.get("company") or "").strip()
    has_uni = _mentions_top_university(bio, company)

    # ── Quality gate ─────────────────────────────────────────────────────────
    # University-affiliated profiles get +5 to the effective score so that strong
    # academic backgrounds aren't penalised by lower recent-activity numbers.
    # The stored talent_score always reflects the true computed value.
    try:
        from scoring.talent_scorer import score_to_grade
        talent_score = calculate_talent_score(profile)
        effective_score = talent_score.overall + (5.0 if has_uni else 0.0)
        effective_grade = score_to_grade(effective_score)
        if GRADE_ORDER.get(effective_grade, 0) < _MIN_GRADE_ORDER:
            return False
    except Exception as e:
        print(f"[upsert_profile] {login}: quality gate scoring failed ({e}) — skipping")
        return False

    lang_names = [l["name"] for l in (profile.get("languages") or [])]
    contrib = profile.get("contributions", {})
    oss_count = contrib.get("openSourceRepoCount", 0)
    signals: list[str] = []
    if oss_count >= 3:
        signals.append("oss_contributor")
    if max_stars >= 10:
        signals.append("starred_project_author")

    row = {
        "github_username":    login,
        "github_data":        profile.get("github_data") or {},
        "talent_score":       talent_score.model_dump(),
        "languages":          lang_names,
        "location_raw":       location_raw,
        "country_code":       country_code,
        "city":               city,
        "own_repo_max_stars": max_stars,
        "followers":          p.get("followers", 0),
        "activity_score":     activity_score,
        "email":              email,
        "linkedin_url":       linkedin_url,
        "role_signals":       role_signals,
        "signals":            signals,
        "source":             source,
        "source_details":     source_details or {},
        "indexed_at":         datetime.now(timezone.utc).isoformat(),
        "expires_at":         (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
    }

    result = (
        sb.table("talent_index")
        .upsert(row, on_conflict="github_username")
        .execute()
    )
    return bool(result.data)


# ── Progress tracking ─────────────────────────────────────────────────────────

def mark_progress(location: str, language: str, pages_fetched: int, profiles_upserted: int, completed: bool = False) -> None:
    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    row: dict = {
        "location":          location,
        "language":          language,
        "pages_fetched":     pages_fetched,
        "profiles_upserted": profiles_upserted,
        "completed":         completed,
        "started_at":        now,
    }
    if completed:
        row["completed_at"] = now
    sb.table("indexer_progress").upsert(row, on_conflict="location,language").execute()


def get_pending_combos(locations: list[str], languages: list[str]) -> list[tuple[str, str]]:
    """
    Return (location, language) pairs that are not yet completed.
    Ordered so Milan and Zurich come first.
    """
    sb = get_supabase()
    done_result = (
        sb.table("indexer_progress")
        .select("location,language")
        .eq("completed", True)
        .execute()
    )
    done: set[tuple[str, str]] = {
        (r["location"], r["language"]) for r in (done_result.data or [])
    }

    all_combos = [(loc, lang) for loc in locations for lang in languages]

    # Priority: Milan and Zurich first
    priority_locs = {"Milan", "Milano", "Zurich", "Zuerich"}
    priority = [(loc, lang) for loc, lang in all_combos if loc in priority_locs and (loc, lang) not in done]
    rest = [(loc, lang) for loc, lang in all_combos if loc not in priority_locs and (loc, lang) not in done]
    return priority + rest


def index_summary() -> dict:
    """Return current counts from talent_index."""
    sb = get_supabase()
    it = sb.table("talent_index").select("id", count="exact").eq("country_code", "IT").execute()
    ch = sb.table("talent_index").select("id", count="exact").eq("country_code", "CH").execute()
    total = sb.table("talent_index").select("id", count="exact").execute()
    return {
        "total": total.count or 0,
        "italy": it.count or 0,
        "switzerland": ch.count or 0,
    }
