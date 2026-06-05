"""
indexer/orangeslice_findurl.py — Find LinkedIn URLs via Orangeslice for profiles missing them.

Calls person.linkedin.findUrl(name, title, location) for the top N profiles
by activity_score that have no linkedin_url. Costs 2 credits per URL found.

With 2,000 free credits = up to 1,000 lookups.

Usage:
    python -m indexer.orangeslice_findurl --dry-run   # show scope, no API calls
    python -m indexer.orangeslice_findurl             # process top 1000
    python -m indexer.orangeslice_findurl --limit 200 --country IT
    python -m indexer.orangeslice_findurl --country CH

Environment:
    ORANGESLICE_API_KEY=osk_...   (falls back to ~/.config/orangeslice/config.json)
    SUPABASE_URL=...
    SUPABASE_SERVICE_KEY=...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from db.client import get_supabase

_DIM    = "\033[2m"
_GREEN  = "\033[32m"
_YELLOW = "\033[33m"
_RED    = "\033[31m"
_CYAN   = "\033[36m"
_RESET  = "\033[0m"
_BOLD   = "\033[1m"

ORANGESLICE_BASE = "https://enrichly-production.up.railway.app"
CONCURRENCY      = 5
BATCH_DELAY      = 1.0   # seconds between batches
POLL_INTERVAL    = 1.5   # seconds between polls
MAX_POLL_ATTEMPTS = 30

_ROLE_TITLE_MAP = {
    "fullstack_signal":    "Full Stack Developer",
    "backend_signal":      "Software Engineer",
    "ml_engineer_signal":  "Machine Learning Engineer",
    "devops_signal":       "DevOps Engineer",
    "fde_signal":          "Frontend Developer",
}
_DEFAULT_TITLE = "Software Developer"

_stop_requested = False


def _install_signal_handler() -> None:
    def _handler(sig, frame):
        global _stop_requested
        print(f"\n{_YELLOW}[orangeslice] Ctrl-C — stopping after current batch.{_RESET}")
        _stop_requested = True
    signal.signal(signal.SIGINT, _handler)


def _load_api_key() -> str:
    key = os.environ.get("ORANGESLICE_API_KEY", "")
    if key:
        return key
    cfg = Path.home() / ".config" / "orangeslice" / "config.json"
    if cfg.exists():
        data = json.loads(cfg.read_text())
        return data.get("apiKey", "")
    return ""


def _role_to_title(role_signals: list[str] | None) -> str:
    if not role_signals:
        return _DEFAULT_TITLE
    for sig in role_signals:
        if sig in _ROLE_TITLE_MAP:
            return _ROLE_TITLE_MAP[sig]
    return _DEFAULT_TITLE


# ── Load targets ──────────────────────────────────────────────────────────────

def load_targets(country: str | None, limit: int) -> list[dict]:
    """Top profiles by activity_score that have no linkedin_url."""
    sb = get_supabase()
    q = (
        sb.table("talent_index")
        .select("github_username, email, city, country_code, role_signals, github_data")
        .is_("linkedin_url", "null")
        .gt("expires_at", datetime.now(timezone.utc).isoformat())
        .order("activity_score", desc=True)
        .limit(limit)
    )
    if country:
        q = q.eq("country_code", country)
    return q.execute().data or []


# ── Orangeslice API ───────────────────────────────────────────────────────────

async def _call_findurl(
    name: str,
    title: str,
    location: str,
    api_key: str,
    client: httpx.AsyncClient,
) -> str | None:
    """Call findUrl and poll until result. Returns LinkedIn URL or None."""
    try:
        resp = await client.post(
            f"{ORANGESLICE_BASE}/execute/linkedin-find-profile-url",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"name": name, "title": title, "location": location},
            timeout=20.0,
        )
        if resp.status_code not in (200, 202):
            return None

        data = resp.json()

        # Synchronous result
        if not data.get("pending"):
            return data if isinstance(data, str) else None

        # Async — poll
        request_id = data.get("requestId")
        if not request_id:
            return None

        for _ in range(MAX_POLL_ATTEMPTS):
            await asyncio.sleep(POLL_INTERVAL)
            poll = await client.get(
                f"{ORANGESLICE_BASE}/function/result/{request_id}",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15.0,
            )
            result = poll.text.strip()
            if result.startswith("http"):
                return result
            try:
                rj = poll.json()
                if not rj.get("pending"):
                    return rj if isinstance(rj, str) else None
            except Exception:
                pass

    except Exception as e:
        pass

    return None


def _write_linkedin_url(github_username: str, linkedin_url: str) -> None:
    sb = get_supabase()
    sb.table("talent_index") \
        .update({"linkedin_url": linkedin_url}) \
        .eq("github_username", github_username) \
        .execute()


# ── Batch runner ──────────────────────────────────────────────────────────────

PROGRESS_INTERVAL = 25   # print summary every N profiles


async def run(targets: list[dict], api_key: str, dry_run: bool) -> dict:
    stats = {"found": 0, "not_found": 0, "skipped": 0, "total": len(targets)}
    semaphore = asyncio.Semaphore(CONCURRENCY)
    _run_start = time.monotonic()

    def _print_progress(completed: int) -> None:
        elapsed  = time.monotonic() - _run_start
        rate     = completed / elapsed if elapsed > 0 else 0
        remaining = stats["total"] - completed
        eta_s    = remaining / rate if rate > 0 else 0
        eta_str  = f"{eta_s/60:.0f}m" if eta_s >= 60 else f"{eta_s:.0f}s"
        hit_rate = stats["found"] / completed * 100 if completed > 0 else 0
        credits  = stats["found"] * 2
        pct      = completed / stats["total"] * 100
        print(
            f"\n  {_CYAN}── progress {completed}/{stats['total']} ({pct:.0f}%) "
            f"│ ✓ {stats['found']} found ({hit_rate:.0f}%) "
            f"│ {credits} credits used "
            f"│ ETA {eta_str} ──{_RESET}\n",
            flush=True,
        )

    async def _process(row: dict, idx: int) -> None:
        login    = row["github_username"]
        name     = (row.get("github_data") or {}).get("name") or login
        title    = _role_to_title(row.get("role_signals"))
        city     = row.get("city") or ""
        country  = row.get("country_code") or ""
        location = ", ".join(filter(None, [city, country]))
        ts       = datetime.now(timezone.utc).strftime("%H:%M:%S")

        if not name or name == login:
            # No real name — skip, would produce bad results
            print(f"  {_DIM}[{ts}]{_RESET} {_DIM}—{_RESET} ({idx}/{stats['total']}) {login} {_DIM}no name, skipped{_RESET}")
            stats["skipped"] += 1
            stats["not_found"] += 1
            return

        async with semaphore:
            async with httpx.AsyncClient(timeout=30.0) as client:
                url = await _call_findurl(name, title, location, api_key, client)

        if url and "linkedin.com/in/" in url:
            stats["found"] += 1
            print(
                f"  {_DIM}[{ts}]{_RESET} {_GREEN}✓{_RESET} "
                f"({idx}/{stats['total']}) {_BOLD}{login}{_RESET} "
                f"{_DIM}({name} · {title} · {location}){_RESET}  {url}"
            )
            if not dry_run:
                _write_linkedin_url(login, url)
        else:
            stats["not_found"] += 1
            print(
                f"  {_DIM}[{ts}]{_RESET} {_DIM}—{_RESET} "
                f"({idx}/{stats['total']}) {login} "
                f"{_DIM}({name} · {location}){_RESET}"
            )

    for batch_start in range(0, len(targets), CONCURRENCY):
        if _stop_requested:
            break
        batch = targets[batch_start : batch_start + CONCURRENCY]
        tasks = [_process(row, batch_start + i + 1) for i, row in enumerate(batch)]
        await asyncio.gather(*tasks)

        completed = batch_start + len(batch)
        if completed % PROGRESS_INTERVAL == 0:
            _print_progress(completed)

        if batch_start + CONCURRENCY < len(targets) and not _stop_requested:
            await asyncio.sleep(BATCH_DELAY)

    return stats


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Find LinkedIn URLs via Orangeslice")
    parser.add_argument("--dry-run",  action="store_true")
    parser.add_argument("--limit",    type=int, default=1000)
    parser.add_argument("--country",  choices=["IT", "CH"])
    args = parser.parse_args()

    _install_signal_handler()

    api_key = _load_api_key()
    if not api_key:
        print(f"{_RED}Error: ORANGESLICE_API_KEY not set and ~/.config/orangeslice/config.json not found.{_RESET}")
        raise SystemExit(1)

    print(f"\n{_BOLD}{'═' * 70}{_RESET}")
    print(f"  {_BOLD}Mirai — Orangeslice LinkedIn URL Discovery{_RESET}")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'═' * 70}\n")

    print("[1/2] Loading top profiles without LinkedIn URL…")
    targets = load_targets(country=args.country, limit=args.limit)

    if not targets:
        print(f"{_GREEN}✓ No profiles to process.{_RESET}\n")
        return

    by_country: dict[str, int] = {}
    for t in targets:
        c = t.get("country_code") or "??"
        by_country[c] = by_country.get(c, 0) + 1
    breakdown = "  ".join(f"{c}: {n}" for c, n in sorted(by_country.items()))

    est_credits = len(targets) * 2
    print(f"  {_BOLD}{len(targets)}{_RESET} profiles  ({breakdown})")
    print(f"  Estimated credits: {_BOLD}{est_credits}{_RESET} (2 per lookup, charged only if URL found)")
    if args.dry_run:
        print(f"  {_YELLOW}[dry-run] No API calls will be made.{_RESET}\n")
        for t in targets[:10]:
            name  = (t.get("github_data") or {}).get("name") or "—"
            title = _role_to_title(t.get("role_signals"))
            loc   = ", ".join(filter(None, [t.get("city"), t.get("country_code")]))
            print(f"    {t['github_username']:28s}  {name:25s}  {title:30s}  {loc}")
        if len(targets) > 10:
            print(f"    … and {len(targets) - 10} more")
        return

    print(f"\n[2/2] Discovering LinkedIn URLs…\n")
    start = time.monotonic()
    stats = asyncio.run(run(targets, api_key, args.dry_run))
    elapsed = time.monotonic() - start

    hit_rate = stats["found"] / stats["total"] * 100 if stats["total"] else 0
    credits_used = stats["found"] * 2

    print(f"\n{'═' * 70}")
    print(f"  Done in {elapsed:.0f}s")
    print(f"  {_GREEN}Found: {stats['found']}  ({hit_rate:.0f}% hit rate){_RESET}")
    print(f"  {_DIM}Not found: {stats['not_found']}{_RESET}")
    print(f"  Credits used: ~{credits_used} (2 × {stats['found']} found)")
    print(f"\n  Next: python -m indexer.enrich_batch  (enrich the new LinkedIn URLs)")
    print(f"{'═' * 70}\n")


if __name__ == "__main__":
    main()
