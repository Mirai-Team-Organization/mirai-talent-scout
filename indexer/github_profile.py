"""
indexer/github_profile.py — Extract contact signals from GitHub profile API.

For profiles with no email and no linkedin_url, calls GET /users/{username}
and extracts:
  - email       (if the user made it public on GitHub)
  - blog        (if it's a linkedin.com/in/ URL, written to linkedin_url;
                  otherwise stored for manual inspection)
  - twitter_username (logged for reference)

This is entirely free (GitHub REST API) and fills the gap that commit-email
extraction can't: developers who set their public profile but never expose
an email in commits.

Usage:
    python -m indexer.github_profile --dry-run   # show scope, no writes
    python -m indexer.github_profile             # process all "nothing" profiles
    python -m indexer.github_profile --country IT
    python -m indexer.github_profile --limit 200

Environment:
    GITHUB_TOKENS=ghp_...,ghp_...   (required — round-robin for rate limits)
    SUPABASE_URL=...                  (required)
    SUPABASE_SERVICE_KEY=...          (required)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import signal
import time
from datetime import datetime, timezone

import httpx

from db.client import get_supabase

_DIM    = "\033[2m"
_GREEN  = "\033[32m"
_YELLOW = "\033[33m"
_RED    = "\033[31m"
_CYAN   = "\033[36m"
_RESET  = "\033[0m"
_BOLD   = "\033[1m"

_LINKEDIN_RE = re.compile(r'linkedin\.com/in/([\w\-%]+)', re.IGNORECASE)
_NOREPLY_RE  = re.compile(r'@users\.noreply\.github\.com', re.IGNORECASE)

CONCURRENCY = 10   # GitHub allows high concurrency with tokens
BATCH_DELAY = 0.2  # seconds between batches

_stop_requested = False


def _install_signal_handler() -> None:
    def _handler(sig, frame):
        global _stop_requested
        print(f"\n{_YELLOW}[github_profile] Ctrl-C — stopping after current batch.{_RESET}")
        _stop_requested = True
    signal.signal(signal.SIGINT, _handler)


# ── Load targets ──────────────────────────────────────────────────────────────

def load_targets(country: str | None, limit: int | None, offset: int = 0) -> list[dict]:
    """Profiles with no email AND no linkedin_url."""
    sb = get_supabase()

    q = (
        sb.table("talent_index")
        .select("github_username, country_code, city")
        .is_("linkedin_url", "null")
        .is_("email", "null")
        .gt("expires_at", datetime.now(timezone.utc).isoformat())
        .order("activity_score", desc=True)
    )

    if country:
        q = q.eq("country_code", country)
    if limit:
        q = q.limit(limit)
    if offset:
        q = q.range(offset, offset + (limit or 1000) - 1)

    return q.execute().data or []


# ── GitHub Users API ──────────────────────────────────────────────────────────

async def _fetch_github_profile(
    login: str,
    token: str,
    client: httpx.AsyncClient,
) -> dict | None:
    try:
        resp = await client.get(
            f"https://api.github.com/users/{login}",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
            },
            timeout=12.0,
        )
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception:
        return None


def _extract_linkedin_from_blog(blog: str) -> str | None:
    """Return normalised linkedin.com/in/handle URL if blog points to LinkedIn."""
    if not blog:
        return None
    m = _LINKEDIN_RE.search(blog)
    if not m:
        return None
    handle = m.group(1).rstrip("/")
    return f"https://www.linkedin.com/in/{handle}"


def _is_real_email(email: str) -> bool:
    return bool(
        email
        and "@" in email
        and not _NOREPLY_RE.search(email)
        and not email.endswith(".local")
    )


# ── DB writes ─────────────────────────────────────────────────────────────────

def _write_findings(github_username: str, linkedin_url: str | None, email: str | None) -> None:
    sb = get_supabase()
    updates: dict = {}
    if linkedin_url:
        updates["linkedin_url"] = linkedin_url
    if email:
        updates["email"] = email
    if updates:
        sb.table("talent_index").update(updates).eq("github_username", github_username).execute()


# ── Batch runner ──────────────────────────────────────────────────────────────

async def run(targets: list[dict], tokens: list[str], dry_run: bool) -> dict:
    stats = {"linkedin": 0, "email": 0, "nothing": 0, "total": len(targets)}
    semaphore = asyncio.Semaphore(CONCURRENCY)
    token_idx = 0

    async def _process(row: dict, idx: int) -> None:
        nonlocal token_idx
        login    = row["github_username"]
        location = " ".join(filter(None, [row.get("city"), row.get("country_code")]))
        ts       = datetime.now(timezone.utc).strftime("%H:%M:%S")

        async with semaphore:
            token = tokens[token_idx % len(tokens)]
            token_idx += 1

            async with httpx.AsyncClient(timeout=15.0) as client:
                profile = await _fetch_github_profile(login, token, client)

            if not profile:
                print(f"  {_DIM}[{ts}]{_RESET} {_DIM}—{_RESET} ({idx}/{stats['total']}) {login} {_DIM}API error{_RESET}")
                stats["nothing"] += 1
                return

            blog     = profile.get("blog") or ""
            bio      = profile.get("bio") or ""
            email    = profile.get("email") or ""
            twitter  = profile.get("twitter_username") or ""

            # Check blog first (explicit link), then bio (free-text mention)
            linkedin_url = _extract_linkedin_from_blog(blog) or _extract_linkedin_from_blog(bio)
            real_email   = email if _is_real_email(email) else None

            if linkedin_url:
                stats["linkedin"] += 1
                print(
                    f"  {_DIM}[{ts}]{_RESET} {_GREEN}✓ linkedin{_RESET} "
                    f"({idx}/{stats['total']}) {_BOLD}{login}{_RESET} "
                    f"{_DIM}({location}){_RESET}  {linkedin_url}"
                )
                if not dry_run:
                    _write_findings(login, linkedin_url, real_email)
            elif real_email:
                stats["email"] += 1
                print(
                    f"  {_DIM}[{ts}]{_RESET} {_CYAN}~ email{_RESET} "
                    f"({idx}/{stats['total']}) {_BOLD}{login}{_RESET} "
                    f"{_DIM}({location}){_RESET}  {real_email}"
                )
                if not dry_run:
                    _write_findings(login, None, real_email)
            else:
                stats["nothing"] += 1
                extras = []
                if blog:
                    extras.append(f"blog={blog[:50]}")
                if twitter:
                    extras.append(f"twitter=@{twitter}")
                extra_str = f"  {_DIM}[{', '.join(extras)}]{_RESET}" if extras else ""
                print(
                    f"  {_DIM}[{ts}]{_RESET} {_DIM}—{_RESET} "
                    f"({idx}/{stats['total']}) {login} {_DIM}({location}){_RESET}{extra_str}"
                )

    tasks = [_process(row, i + 1) for i, row in enumerate(targets)]

    for batch_start in range(0, len(tasks), CONCURRENCY):
        if _stop_requested:
            break
        batch = tasks[batch_start : batch_start + CONCURRENCY]
        await asyncio.gather(*batch)
        if batch_start + CONCURRENCY < len(tasks) and not _stop_requested:
            await asyncio.sleep(BATCH_DELAY)

    return stats


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Extract contact signals from GitHub profile API")
    parser.add_argument("--dry-run",  action="store_true")
    parser.add_argument("--limit",    type=int, default=None)
    parser.add_argument("--offset",   type=int, default=0)
    parser.add_argument("--country",  choices=["IT", "CH"])
    args = parser.parse_args()

    _install_signal_handler()

    raw_tokens = os.environ.get("GITHUB_TOKENS", "")
    tokens = [t.strip() for t in raw_tokens.split(",") if t.strip()]
    if not tokens:
        print(f"{_RED}Error: GITHUB_TOKENS not set.{_RESET}")
        raise SystemExit(1)

    print(f"\n{_BOLD}{'═' * 70}{_RESET}")
    print(f"  {_BOLD}Mirai — GitHub Profile Signal Extraction{_RESET}")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'═' * 70}\n")

    print("[1/2] Loading profiles with no email and no LinkedIn URL…")
    targets = load_targets(country=args.country, limit=args.limit, offset=args.offset)

    if not targets:
        print(f"{_GREEN}✓ No profiles to process.{_RESET}\n")
        return

    by_country: dict[str, int] = {}
    for t in targets:
        c = t.get("country_code") or "??"
        by_country[c] = by_country.get(c, 0) + 1
    breakdown = "  ".join(f"{c}: {n}" for c, n in sorted(by_country.items()))

    print(f"  {_BOLD}{len(targets)}{_RESET} profiles  ({breakdown})")
    print(f"  Tokens: {len(tokens)}  |  Concurrency: {CONCURRENCY}")
    if args.dry_run:
        print(f"  {_YELLOW}[dry-run] No writes to DB.{_RESET}")
        return

    print(f"\n[2/2] Fetching GitHub profiles…\n")
    start = time.monotonic()
    stats = asyncio.run(run(targets, tokens, args.dry_run))
    elapsed = time.monotonic() - start

    print(f"\n{'═' * 70}")
    print(f"  Done in {elapsed:.0f}s")
    print(f"  {_GREEN}LinkedIn found: {stats['linkedin']}{_RESET}")
    print(f"  {_CYAN}Email found:    {stats['email']}{_RESET}")
    print(f"  {_DIM}Nothing:        {stats['nothing']}{_RESET}")
    print(f"{'═' * 70}\n")

    if stats["linkedin"] or stats["email"]:
        print(f"  Next: python -m indexer.proxycurl_linkedin  (for new emails)")
        print(f"        python -m indexer.enrich_batch        (for new LinkedIn URLs)\n")


if __name__ == "__main__":
    main()
