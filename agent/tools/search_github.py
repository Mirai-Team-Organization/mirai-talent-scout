"""
GitHub search tool — NL query → GitHub user search → full GraphQL profiles.

Rate limiting: token pool (round-robin) + global counter in Supabase.
If sum(requests_this_hour) > 4000: raises RateLimitQueuedError.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

import boto3
from strands import tool

from db.client import get_supabase

# ── Bedrock Haiku query translator ───────────────────────────────────────────

_bedrock_client = None
_query_cache: dict[str, str] = {}
_QUERY_CACHE_MAX = 100

# GitHub search qualifiers we allow through validation
_GITHUB_QUALIFIER_PREFIXES = (
    "language:", "location:", "followers:", "repos:", "in:", "type:", "is:",
)

_TRANSLATE_SYSTEM = """You are a GitHub user search query builder. Convert recruiter queries into GitHub search syntax.

Valid qualifiers only:
- location:City  (city or country)
- language:Name  (Python, JavaScript, TypeScript, Go, Rust, Java, Ruby, C++, etc.)
- followers:>N   (10=junior, 50=mid, 100=senior, 500=influential)
- repos:>N       (5=active contributor)

Rules:
- Extract only what maps to a GitHub qualifier. Ignore "early stage startup", "remote ok", "open to moving".
- Return ONLY the GitHub search string, nothing else. No explanation.

Examples:
"senior React engineer in Zurich"           → location:Zurich language:JavaScript followers:>100
"back-end engineer early stage startup Milan" → location:Milan followers:>10
"Python data scientist Berlin open to moving" → location:Berlin language:Python followers:>20
"junior iOS developer"                       → language:Swift followers:>10"""


def _get_bedrock():
    global _bedrock_client
    if _bedrock_client is None:
        _bedrock_client = boto3.client("bedrock-runtime")
    return _bedrock_client


def _validate_github_query(query: str) -> str:
    """Strip tokens with unrecognized qualifier prefixes (Haiku hallucinations)."""
    tokens = query.strip().split()
    valid = [
        t for t in tokens
        if ":" not in t  # free-text word — keep
        or any(t.lower().startswith(p) for p in _GITHUB_QUALIFIER_PREFIXES)
    ]
    return " ".join(valid)


def _translate_query(nl_query: str) -> str:
    """
    Translate a natural-language recruiter query to GitHub search syntax via Bedrock Haiku.
    Results are cached in-process (module-level dict, max 100 entries).
    Falls back to the raw query string on any error.
    """
    if nl_query in _query_cache:
        return _query_cache[nl_query]

    try:
        model_id = os.environ.get(
            "BEDROCK_HAIKU_MODEL", "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
        )
        resp = _get_bedrock().converse(
            modelId=model_id,
            system=[{"text": _TRANSLATE_SYSTEM}],
            messages=[{"role": "user", "content": [{"text": nl_query}]}],
            inferenceConfig={"maxTokens": 100, "temperature": 0},
        )
        raw = resp["output"]["message"]["content"][0]["text"].strip()
        translated = _validate_github_query(raw) or nl_query

    except Exception as e:
        print(f"[translate_query] Bedrock call failed ({e}), using raw query")
        return nl_query  # don't cache failures — retry on next call

    if len(_query_cache) < _QUERY_CACHE_MAX:
        _query_cache[nl_query] = translated
    return translated

# ── GitHub GraphQL query (mirrors profileFetcher.ts) ─────────────────────────

PROFILE_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    login name bio email location company websiteUrl twitterUsername createdAt
    followers { totalCount }
    repositories(first: 100, isFork: false, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        name stargazerCount
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
        weeks {
          contributionDays { date contributionCount weekday }
        }
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


class RateLimitQueuedError(Exception):
    pass


def _get_tokens() -> list[str]:
    raw = os.environ.get("GITHUB_TOKENS", "")
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if not tokens:
        raise ValueError("GITHUB_TOKENS env var is not set")
    return tokens


def _pick_token(search_id: int) -> str:
    tokens = _get_tokens()
    return tokens[search_id % len(tokens)]


def _check_rate_limit(token_id: str) -> None:
    """Raise RateLimitQueuedError if global usage is near the quota ceiling."""
    sb = get_supabase()
    now = datetime.now(timezone.utc)

    # Reset counters that have expired
    sb.table("github_api_usage") \
      .update({"requests_this_hour": 0, "reset_at": now.isoformat()}) \
      .lt("reset_at", now.isoformat()) \
      .execute()

    result = sb.table("github_api_usage").select("requests_this_hour").execute()
    total = sum(row["requests_this_hour"] for row in (result.data or []))

    if total > 4000:
        raise RateLimitQueuedError(
            f"GitHub API quota near limit ({total}/5000 req/hr). "
            "Search queued — try again in a few minutes."
        )


def _increment_usage(token_id: str, count: int) -> None:
    sb = get_supabase()
    now = datetime.now(timezone.utc)

    # Upsert: create row if first use of this token this hour
    from datetime import timedelta
    reset_at = (now + timedelta(hours=1)).isoformat()

    existing = (
        sb.table("github_api_usage")
        .select("requests_this_hour, reset_at")
        .eq("token_id", token_id)
        .maybe_single()
        .execute()
    )

    if existing and existing.data:
        sb.table("github_api_usage") \
          .update({"requests_this_hour": existing.data["requests_this_hour"] + count}) \
          .eq("token_id", token_id) \
          .execute()
    else:
        sb.table("github_api_usage").insert({
            "token_id": token_id,
            "requests_this_hour": count,
            "reset_at": reset_at,
        }).execute()


def _github_graphql(query: str, variables: dict, token: str) -> dict:
    """Execute a GitHub GraphQL query with retry on 429/403."""
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "mirai-talent-scout/1.0",
        },
    )

    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 403) and attempt < 2:
                time.sleep(2 ** attempt * 5)
                continue
            raise
    raise RuntimeError("GitHub API request failed after retries")


def _github_search_users(query: str, limit: int, token: str) -> list[dict]:
    """Call GitHub REST /search/users."""
    encoded = urllib.parse.quote(query)
    url = f"https://api.github.com/search/users?q={encoded}&per_page={min(limit + 5, 30)}&sort=followers"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "mirai-talent-scout/1.0",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    return data.get("items", [])[:limit]


import urllib.parse
from datetime import timedelta


@tool
def search_github(query: str, limit: int = 20, hiring_context: str | None = None) -> list[dict]:
    """
    Search GitHub for developer profiles matching a natural language query.

    Args:
        query: Natural language description, e.g. "senior React engineer in Zurich"
        limit: Max candidates to return (default 20, max 30)
        hiring_context: "startup_early" | "startup_growth" | "enterprise"

    Returns:
        List of raw GitHub profile dicts ready for scoring.
    """
    import hashlib
    limit = min(limit, 30)
    search_id = int(hashlib.md5(query.encode()).hexdigest()[:8], 16)
    token = _pick_token(search_id)
    token_id = f"tok_{search_id % len(_get_tokens())}"

    _check_rate_limit(token_id)

    # Translate NL query → GitHub search syntax
    translated = _translate_query(query)

    # Search users
    users = _github_search_users(translated, limit, token)
    _increment_usage(token_id, 1)

    # Fetch full profiles via GraphQL
    now = datetime.now(timezone.utc)
    from_date = (now - timedelta(days=365)).isoformat()

    profiles = []
    for user in users:
        try:
            result = _github_graphql(
                PROFILE_QUERY,
                {"login": user["login"], "from": from_date, "to": now.isoformat()},
                token,
            )
            _increment_usage(token_id, 1)
            if result.get("data", {}).get("user"):
                profiles.append(_parse_profile(result["data"]["user"]))
        except Exception as e:
            # Don't abort the batch — log and continue
            print(f"[search_github] Failed to fetch {user['login']}: {e}")

    return profiles




def _parse_profile(user: dict) -> dict:
    """Normalise a GitHub GraphQL user response into the profile shape expected by scoring."""
    cc = user.get("contributionsCollection", {})
    calendar = cc.get("contributionCalendar", {})
    weeks = calendar.get("weeks", [])

    daily_activity = [
        {"date": day["date"], "count": day["contributionCount"], "weekday": day["weekday"]}
        for week in weeks
        for day in week.get("contributionDays", [])
    ]

    # Aggregate languages across repos
    lang_totals: dict[str, int] = {}
    for repo in user.get("repositories", {}).get("nodes", []):
        for edge in repo.get("languages", {}).get("edges", []):
            name = edge["node"]["name"]
            lang_totals[name] = lang_totals.get(name, 0) + edge.get("size", 0)

    # Top 5 languages only — full list can be 20+ entries and bloats context
    languages = [
        {"name": k, "size": v}
        for k, v in sorted(lang_totals.items(), key=lambda x: -x[1])[:5]
    ]

    # Open source contributions — count only; full repo list is ~50 entries per candidate
    login = user.get("login", "")
    oss_count = sum(
        1 for c in cc.get("commitContributionsByRepository", [])
        if c["repository"]["owner"]["login"] != login
    )

    return {
        "profile": {
            "login": login,
            "name": user.get("name"),
            "bio": user.get("bio"),
            "email": user.get("email"),
            "location": user.get("location"),
            "company": user.get("company"),
            "websiteUrl": user.get("websiteUrl"),
            "twitterUsername": user.get("twitterUsername"),
            "createdAt": user.get("createdAt"),
            "followers": user.get("followers", {}).get("totalCount", 0),
        },
        "languages": languages,
        "pinnedProjects": user.get("pinnedItems", {}).get("nodes", []),
        "activityHeatmap": {
            # dailyActivity (365 records/candidate) removed — totalContributions is sufficient for scoring
            "totalContributions": calendar.get("totalContributions", 0),
        },
        "contributions": {
            "commits": cc.get("totalCommitContributions", 0),
            "issues": cc.get("totalIssueContributions", 0),
            "pullRequests": cc.get("totalPullRequestContributions", 0),
            "pullRequestReviews": cc.get("totalPullRequestReviewContributions", 0),
            "openSourceRepoCount": oss_count,
        },
        "profileReadme": {"exists": False},  # not fetched in batch mode
    }
