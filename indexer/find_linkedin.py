"""
indexer/find_linkedin.py — Multi-signal LinkedIn URL discovery for talent_index profiles.

Finds LinkedIn URLs for profiles that don't have one using two techniques
applied in order (stops as soon as a URL is confirmed):

  1. Commit-email trick
     Fetches the author email from recent git commits via GitHub REST API.
     Even bare profiles expose their real email this way.
     The email is then used for a targeted Google X-ray search.

  2. Google X-ray search (3 fallback queries, tried in order)
     a. site:linkedin.com/in/ "email"            ← most precise
     b. site:linkedin.com/in/ "github_username"  ← catches people who reuse their handle
     c. site:linkedin.com/in/ "Full Name" "City" ← location + name

Google search:
  - If GOOGLE_CSE_KEY + GOOGLE_CSE_ID are set: uses Google Custom Search JSON API
    (free tier: 100 searches/day).
  - Otherwise: falls back to direct HTTPS request with a browser User-Agent.
    Works for light usage; add a delay between searches to avoid 429s.

Usage:
    python -m indexer.find_linkedin                 # process all profiles without linkedin_url
    python -m indexer.find_linkedin --dry-run        # show scope, no updates
    python -m indexer.find_linkedin --limit 50       # process at most 50 profiles
    python -m indexer.find_linkedin --country IT     # Italy only
    python -m indexer.find_linkedin --no-google      # commit-email only (no Google)

Environment:
    GITHUB_TOKENS=ghp_...,ghp_...   (required — used to fetch commit emails)
    SUPABASE_URL=...                  (required)
    SUPABASE_SERVICE_KEY=...          (required)
    GOOGLE_CSE_KEY=...               (optional — for reliable Google search)
    GOOGLE_CSE_ID=...                (optional — Custom Search Engine ID)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from db.client import get_supabase

# ── ANSI colour helpers ───────────────────────────────────────────────────────
_DIM    = "\033[2m"
_GREEN  = "\033[32m"
_YELLOW = "\033[33m"
_RED    = "\033[31m"
_CYAN   = "\033[36m"
_RESET  = "\033[0m"
_BOLD   = "\033[1m"

_LINKEDIN_RE = re.compile(r'linkedin\.com/in/([\w\-%]+)', re.IGNORECASE)
_NOREPLY_RE  = re.compile(r'@users\.noreply\.github\.com', re.IGNORECASE)

_stop_requested = False


def _install_signal_handler() -> None:
    def _handler(sig, frame):
        global _stop_requested
        print(f"\n{_YELLOW}[find_linkedin] Ctrl-C — finishing current batch then stopping.{_RESET}")
        _stop_requested = True
    signal.signal(signal.SIGINT, _handler)


# ── Load targets from Supabase ────────────────────────────────────────────────

def load_targets(country: str | None, limit: int | None) -> list[dict]:
    """Return talent_index rows that have no linkedin_url."""
    sb = get_supabase()

    q = (
        sb.table("talent_index")
        .select("github_username, email, location_raw, city, country_code, github_data")
        .is_("linkedin_url", "null")
        .gt("expires_at", datetime.now(timezone.utc).isoformat())
        .order("activity_score", desc=True)
    )

    if country:
        q = q.eq("country_code", country)
    if limit:
        q = q.limit(limit)

    result = q.execute()
    return result.data or []


# ── GitHub: commit email extraction ──────────────────────────────────────────

async def _fetch_commit_email(
    login: str,
    repo_full_name: str,
    token: str,
    client: httpx.AsyncClient,
) -> str | None:
    """
    Fetch recent commits from a repo and return the author's real email if found.
    Filters out GitHub no-reply addresses and obviously fake ones.
    """
    try:
        resp = await client.get(
            f"https://api.github.com/repos/{repo_full_name}/commits",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
            },
            params={"author": login, "per_page": 5},
            timeout=12.0,
        )
        if resp.status_code != 200:
            return None

        for commit in resp.json():
            email = commit.get("commit", {}).get("author", {}).get("email", "")
            if (
                email
                and "@" in email
                and not _NOREPLY_RE.search(email)
                and not email.endswith(".local")
            ):
                return email.lower().strip()

    except Exception:
        pass

    return None


async def get_commit_email(
    login: str,
    github_data: dict,
    token: str,
    client: httpx.AsyncClient,
) -> str | None:
    """
    Try up to 3 of the user's own non-fork repos to find a real commit email.
    Prefers repos from contributionsCollection (recent activity), then pinnedItems.
    """
    candidate_repos: list[str] = []

    # 1. Repos with recent commits (from contributionsCollection)
    cc = github_data.get("contributionsCollection") or {}
    for item in cc.get("commitContributionsByRepository") or []:
        repo = item.get("repository") or {}
        owner_login = (repo.get("owner") or {}).get("login", "")
        if owner_login.lower() == login.lower() and not repo.get("isFork"):
            name = repo.get("nameWithOwner")
            if name and name not in candidate_repos:
                candidate_repos.append(name)

    # 2. Pinned repos (may be external but often their own flagship projects)
    for node in (github_data.get("pinnedItems") or {}).get("nodes") or []:
        name = node.get("nameWithOwner")
        owner_login = (node.get("owner") or {}).get("login", "")
        if name and owner_login.lower() == login.lower() and name not in candidate_repos:
            candidate_repos.append(name)

    for repo in candidate_repos[:4]:
        email = await _fetch_commit_email(login, repo, token, client)
        if email:
            return email

    return None


# ── Google X-ray search ───────────────────────────────────────────────────────

_CSE_KEY = os.environ.get("GOOGLE_CSE_KEY", "")
_CSE_ID  = os.environ.get("GOOGLE_CSE_ID", "")

_GOOGLE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


async def _google_cse(query: str, client: httpx.AsyncClient) -> str | None:
    """Query Google Custom Search API — reliable, 100 free searches/day."""
    try:
        resp = await client.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"key": _CSE_KEY, "cx": _CSE_ID, "q": query, "num": 5},
            timeout=15.0,
        )
        if resp.status_code != 200:
            return None

        for item in resp.json().get("items") or []:
            url = item.get("link") or item.get("formattedUrl") or ""
            m = _LINKEDIN_RE.search(url)
            if m:
                return f"https://www.linkedin.com/in/{m.group(1)}"

    except Exception:
        pass

    return None


async def _bing_search(query: str, client: httpx.AsyncClient) -> str | None:
    """
    Bing web search — much more lenient than Google for automated queries.
    Parses LinkedIn URLs from the HTML response body.
    """
    try:
        resp = await client.get(
            "https://www.bing.com/search",
            params={"q": query, "count": 5},
            headers={
                **_GOOGLE_HEADERS,
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            },
            timeout=15.0,
            follow_redirects=True,
        )
        if resp.status_code == 429:
            print(f"  {_YELLOW}[bing] Rate-limited — increase GOOGLE_DELAY{_RESET}")
            return None

        for m in _LINKEDIN_RE.finditer(resp.text):
            handle = m.group(1)
            if handle.startswith("search") or handle.startswith("pub") or len(handle) < 3:
                continue
            return f"https://www.linkedin.com/in/{handle}"

    except Exception:
        pass

    return None


async def _google_direct(query: str, client: httpx.AsyncClient) -> str | None:
    """
    Direct Google HTTPS request — fallback, rate-limits quickly.
    """
    try:
        resp = await client.get(
            "https://www.google.com/search",
            params={"q": query, "num": 5, "hl": "en"},
            headers=_GOOGLE_HEADERS,
            timeout=15.0,
            follow_redirects=True,
        )
        if resp.status_code == 429:
            return None

        for m in _LINKEDIN_RE.finditer(resp.text):
            handle = m.group(1)
            if handle.startswith("search") or handle.startswith("pub"):
                continue
            return f"https://www.linkedin.com/in/{handle}"

    except Exception:
        pass

    return None


async def google_xray(query: str, client: httpx.AsyncClient) -> str | None:
    """Run an X-ray search; uses CSE if configured, else Bing, else Google direct."""
    full_query = f'site:linkedin.com/in/ {query}'
    if _CSE_KEY and _CSE_ID:
        return await _google_cse(full_query, client)
    # Bing is the reliable free fallback
    result = await _bing_search(full_query, client)
    if result:
        return result
    return await _google_direct(full_query, client)


# ── Verify a LinkedIn URL is a real profile (not a search page) ───────────────

_VALID_HANDLE = re.compile(r'^[\w\-%]{3,}$')

def _looks_like_profile_url(url: str) -> bool:
    m = _LINKEDIN_RE.search(url)
    if not m:
        return False
    handle = m.group(1)
    return bool(_VALID_HANDLE.match(handle))


# ── Per-profile LinkedIn discovery ───────────────────────────────────────────

GOOGLE_DELAY = float(os.environ.get("GOOGLE_DELAY", "2.5"))  # seconds between Google calls


async def find_linkedin_for_profile(
    row: dict,
    token: str,
    gh_client: httpx.AsyncClient,
    google_client: httpx.AsyncClient,
    use_google: bool,
) -> tuple[str | None, str | None, str]:
    """
    Try to find a LinkedIn URL for a talent_index profile.

    Returns (linkedin_url, commit_email, method_used).
    linkedin_url is None if not found.
    """
    login       = row["github_username"]
    github_data = row.get("github_data") or {}
    name        = github_data.get("name") or ""
    company     = (github_data.get("company") or "").strip().lstrip("@")
    city        = row.get("city") or ""
    known_email = row.get("email") or ""

    # ── Step 1: extract commit email ─────────────────────────────────────────
    commit_email: str | None = known_email or None
    if not commit_email:
        commit_email = await get_commit_email(login, github_data, token, gh_client)

    if not use_google:
        return None, commit_email, "email_only"

    # ── Step 2a: Google X-ray by email (most precise) ─────────────────────
    if commit_email:
        await asyncio.sleep(GOOGLE_DELAY)
        url = await google_xray(f'"{commit_email}"', google_client)
        if url and _looks_like_profile_url(url):
            return url, commit_email, f"email→google ({commit_email})"

    # ── Step 2b: Google X-ray by GitHub username ──────────────────────────
    await asyncio.sleep(GOOGLE_DELAY)
    url = await google_xray(f'"{login}"', google_client)
    if url and _looks_like_profile_url(url):
        return url, commit_email, "username→google"

    # ── Step 2c: Google X-ray by name + company (if we have both) ───────
    if name and company:
        await asyncio.sleep(GOOGLE_DELAY)
        url = await google_xray(f'"{name}" "{company}"', google_client)
        if url and _looks_like_profile_url(url):
            return url, commit_email, f"name+company→google"

    # ── Step 2d: Google X-ray by name + city (if we have both) ──────────
    if name and city:
        await asyncio.sleep(GOOGLE_DELAY)
        url = await google_xray(f'"{name}" "{city}"', google_client)
        if url and _looks_like_profile_url(url):
            return url, commit_email, f"name+city→google"

    return None, commit_email, "not_found"


# ── Persist results to Supabase ───────────────────────────────────────────────

def _update_profile(github_username: str, linkedin_url: str | None, email: str | None) -> None:
    sb = get_supabase()
    updates: dict = {}
    if linkedin_url:
        updates["linkedin_url"] = linkedin_url
    if email:
        updates["email"] = email
    if updates:
        sb.table("talent_index").update(updates).eq("github_username", github_username).execute()


# ── Main batch loop ───────────────────────────────────────────────────────────

async def run(targets: list[dict], use_google: bool, dry_run: bool, token: str) -> dict:
    stats = {"linkedin_found": 0, "email_found": 0, "not_found": 0, "total": len(targets)}

    # Two separate clients: GitHub (with auth) and Google (without auth header)
    async with (
        httpx.AsyncClient(timeout=15.0) as gh_client,
        httpx.AsyncClient(timeout=15.0, headers=_GOOGLE_HEADERS) as google_client,
    ):
        for i, row in enumerate(targets, 1):
            if _stop_requested:
                break

            login = row["github_username"]
            location = " ".join(filter(None, [row.get("city"), row.get("country_code")]))
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")

            print(
                f"  {_DIM}[{ts}]{_RESET} ({i}/{len(targets)}) "
                f"{_BOLD}{login}{_RESET} {_DIM}({location}){_RESET}",
                end=" ",
                flush=True,
            )

            linkedin_url, commit_email, method = await find_linkedin_for_profile(
                row, token, gh_client, google_client, use_google
            )

            if linkedin_url:
                print(f"{_GREEN}✓ {linkedin_url}{_RESET}  {_DIM}[{method}]{_RESET}")
                stats["linkedin_found"] += 1
                if commit_email:
                    stats["email_found"] += 1
                if not dry_run:
                    _update_profile(login, linkedin_url, commit_email)
            elif commit_email:
                print(f"{_CYAN}~ email only: {commit_email}{_RESET}  {_DIM}[{method}]{_RESET}")
                stats["email_found"] += 1
                if not dry_run:
                    _update_profile(login, None, commit_email)
            else:
                print(f"{_DIM}— not found{_RESET}")
                stats["not_found"] += 1

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Find LinkedIn URLs for talent_index profiles")
    parser.add_argument("--dry-run",   action="store_true", help="Show findings without updating DB")
    parser.add_argument("--limit",     type=int, default=None, help="Max profiles to process")
    parser.add_argument("--country",   choices=["IT", "CH"],   help="Restrict to one country")
    parser.add_argument("--no-google", action="store_true",    help="Skip Google search (emails only)")
    args = parser.parse_args()

    _install_signal_handler()

    # Pick one GitHub token for REST calls
    raw_tokens = os.environ.get("GITHUB_TOKENS", "")
    tokens = [t.strip() for t in raw_tokens.split(",") if t.strip()]
    if not tokens:
        print(f"{_RED}Error: GITHUB_TOKENS not set.{_RESET}")
        raise SystemExit(1)
    token = tokens[0]

    print(f"\n{_BOLD}{'═' * 70}{_RESET}")
    print(f"  {_BOLD}Mirai — LinkedIn URL Discovery{_RESET}")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'═' * 70}\n")

    use_google = not args.no_google
    if use_google:
        if _CSE_KEY and _CSE_ID:
            print(f"  Google: {_GREEN}Custom Search API (reliable){_RESET}")
        else:
            print(f"  Google: {_YELLOW}direct HTTP (set GOOGLE_CSE_KEY+GOOGLE_CSE_ID for reliability){_RESET}")
    else:
        print(f"  Google: {_DIM}disabled (--no-google){_RESET}")

    print(f"\n[1/2] Loading profiles without linkedin_url…", flush=True)
    targets = load_targets(country=args.country, limit=args.limit)

    if not targets:
        print(f"{_GREEN}✓ All indexed profiles already have a LinkedIn URL.{_RESET}\n")
        return

    by_country: dict[str, int] = {}
    for t in targets:
        c = t.get("country_code") or "??"
        by_country[c] = by_country.get(c, 0) + 1
    breakdown = "  ".join(f"{c}: {n}" for c, n in sorted(by_country.items()))

    print(f"  {_BOLD}{len(targets)}{_RESET} profiles need LinkedIn discovery  ({breakdown})")
    if args.dry_run:
        print(f"  {_YELLOW}[dry-run] DB will NOT be updated.{_RESET}")

    # Searches per profile: up to 3 Google calls × GOOGLE_DELAY seconds each
    # + 1 GitHub commit lookup (fast)
    max_time_min = len(targets) * (3 * GOOGLE_DELAY + 2) / 60
    print(f"  Estimated time: ~{max_time_min:.0f} min at {GOOGLE_DELAY}s/search\n")

    print(f"[2/2] Discovering LinkedIn URLs…\n")
    start = time.monotonic()

    stats = asyncio.run(run(targets, use_google, args.dry_run, token))

    elapsed = time.monotonic() - start
    print(f"\n{'═' * 70}")
    print(f"  Completed in {elapsed:.0f}s")
    print(f"  {_GREEN}LinkedIn found: {stats['linkedin_found']}{_RESET}")
    print(f"  {_CYAN}Commit email found (no LinkedIn): {stats['email_found'] - stats['linkedin_found']}{_RESET}")
    print(f"  {_DIM}Not found: {stats['not_found']}{_RESET}")
    if args.dry_run:
        print(f"  {_YELLOW}[dry-run] No DB changes made.{_RESET}")
    print(f"{'═' * 70}\n")


if __name__ == "__main__":
    main()
