"""
hackathon_refresh — find IT/CH hackathon participants + clean expired profiles.

Two strategies:
  1. topic:hackathon repos — project submissions tagged by participants themselves
  2. Known hackathon org repos — ETHGlobal, MLH, Junction, etc.

Runs on Sundays.
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone

from db.client import get_supabase
from indexer.core import TokenPool, fetch_profile, upsert_profile, _deadline, infer_location
from indexer.display import log_accepted, print_section_header
from indexer.role_signals import infer_role_signals
from scoring.talent_scorer import calculate_talent_score

# Orgs that host hackathon project repos (participants fork/submit here)
_HACKATHON_ORGS = [
    "ethglobal",       # ETHGlobal — web3 hackathons
    "buildspace",      # Buildspace
    "MLH",             # Major League Hacking
    "hackclub",        # Hack Club
    "lablab-ai",       # Lablab.ai — AI hackathons
    "junction-hack",   # Junction — top EU hackathon (Helsinki)
    "devfolio",        # Devfolio — India + EU hackathons
]

_HACKATHON_TOPICS = [
    "hackathon",
    "ethglobal",
    "mlh",
    "junction",
    "lablab",
]

_MIN_STARS = 0        # hackathon repos often have 0 stars — don't filter by stars
_MAX_REPOS_PER_SOURCE = 100
_MAX_CONTRIBUTORS_PER_REPO = 20


def _search_repos_by_topic(topic: str, token: str) -> list[dict]:
    """Return recent repos tagged with a hackathon topic."""
    q = urllib.parse.quote(f"topic:{topic} pushed:>2024-01-01")
    url = (
        f"https://api.github.com/search/repositories"
        f"?q={q}&sort=updated&order=desc&per_page={_MAX_REPOS_PER_SOURCE}"
    )
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "mirai-talent-indexer/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
            return [{"owner": r["owner"]["login"], "name": r["name"]} for r in data.get("items", [])]
    except Exception as e:
        print(f"[hackathon_refresh] topic search {topic}: {e}")
        return []


def _search_org_repos(org: str, token: str) -> list[dict]:
    """Return recent repos from a hackathon org."""
    url = (
        f"https://api.github.com/orgs/{org}/repos"
        f"?sort=updated&per_page={_MAX_REPOS_PER_SOURCE}"
    )
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "mirai-talent-indexer/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
            return [{"owner": r["owner"]["login"], "name": r["name"]} for r in data]
    except Exception as e:
        print(f"[hackathon_refresh] org repos {org}: {e}")
        return []


def _get_contributors(owner: str, repo: str, token: str) -> list[str]:
    """Return top contributor logins for a repo."""
    url = (
        f"https://api.github.com/repos/{owner}/{repo}/contributors"
        f"?per_page={_MAX_CONTRIBUTORS_PER_REPO}"
    )
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "mirai-talent-indexer/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
            return [u["login"] for u in data if u.get("type") == "User"]
    except Exception:
        return []


def _process_repos(
    label: str,
    repos: list[dict],
    pool: TokenPool,
    seen: set[str],
    deadline: float,
) -> int:
    """Fetch contributors from repos, filter IT/CH, upsert. Returns count accepted."""
    print_section_header(label)
    accepted = 0

    for repo in repos:
        if time.monotonic() >= deadline:
            break

        token, _ = pool.acquire()
        logins = _get_contributors(repo["owner"], repo["name"], token)

        for login in logins:
            if login in seen or time.monotonic() >= deadline:
                continue
            seen.add(login)

            token, _ = pool.acquire()
            profile = fetch_profile(login, token)
            if not profile:
                continue

            country_code, _ = infer_location(profile.get("profile", {}).get("location"))
            if country_code not in ("IT", "CH"):
                continue

            try:
                ts = calculate_talent_score(profile)
                grade, score = ts.grade, ts.overall
            except Exception:
                continue

            ok = upsert_profile(
                profile,
                source="github_hackathon",
                source_details={"source": label, "repo": f"{repo['owner']}/{repo['name']}"},
            )
            if ok:
                profile["role_signals"] = infer_role_signals(profile)
                log_accepted(profile, grade, score)
                # Tag with hackathon_participant signal
                sb = get_supabase()
                existing = (
                    sb.table("talent_index")
                    .select("signals")
                    .eq("github_username", login)
                    .maybe_single()
                    .execute()
                )
                if existing and existing.data:
                    sigs: list[str] = existing.data.get("signals") or []
                    if "hackathon_participant" not in sigs:
                        sigs.append("hackathon_participant")
                        sb.table("talent_index").update({"signals": sigs}).eq("github_username", login).execute()
                accepted += 1

    print(f"\n  → {accepted} accepted from {label}", flush=True)
    return accepted


def run(context) -> dict:
    pool = TokenPool.from_env()
    deadline = _deadline(context)
    seen: set[str] = set()
    upserted = 0

    # Strategy 1: topic-tagged hackathon repos
    for topic in _HACKATHON_TOPICS:
        if time.monotonic() >= deadline:
            break
        token, _ = pool.acquire()
        repos = _search_repos_by_topic(topic, token)
        upserted += _process_repos(f"topic:{topic}", repos, pool, seen, deadline)

    # Strategy 2: known hackathon org repos
    for org in _HACKATHON_ORGS:
        if time.monotonic() >= deadline:
            break
        token, _ = pool.acquire()
        repos = _search_org_repos(org, token)
        upserted += _process_repos(f"org:{org}", repos, pool, seen, deadline)

    # Clean up expired profiles
    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    deleted = sb.table("talent_index").delete().lt("expires_at", now).execute()
    deleted_count = len(deleted.data or [])
    print(f"\n[hackathon_refresh] Cleaned {deleted_count} expired profiles.")

    return {"upserted": upserted, "expired_deleted": deleted_count, "remaining_combos": 0}
