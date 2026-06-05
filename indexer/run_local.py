"""
indexer/run_local.py — run the talent indexer locally (no Lambda needed).

Usage:
    python -m indexer.run_local [--shard SHARD] [--dry-run]

    SHARD options: it_milan | it_north | it_rest | ch_zurich | ch_rest |
                   oss_topics | hackathon_refresh | all (default)

    --dry-run: show pending combos without fetching profiles

Environment:
    GITHUB_TOKENS=ghp_token1,ghp_token2   (required)
    SUPABASE_URL=...                       (required)
    SUPABASE_SERVICE_KEY=...               (required)

Runs until all combos for the selected shard(s) are done or Ctrl-C.
Progress is checkpointed to `indexer_progress` so you can resume any time.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime, timezone

from indexer.core import (
    TokenPool,
    fetch_profile,
    get_pending_combos,
    github_search_users_page,
    index_summary,
    mark_progress,
    upsert_profile,
    _MAX_FOLLOWERS,
    _MAX_REPO_STARS,
    _MIN_GRADE,
)
from scoring.talent_scorer import calculate_talent_score, GRADE_ORDER
from indexer.display import log_accepted, print_section_header, role_label, _GRADE_COLOUR, _RESET, _DIM

# ── Shard definitions (mirrors handler.py) ────────────────────────────────────

_LANGUAGES = [
    "Python", "Jupyter Notebook", "TypeScript", "JavaScript", "Go", "Rust",
    "Java", "Kotlin", "Swift", "C++",
]

_SHARDS: dict[str, list[str]] = {
    "it_milan":  ["Milan", "Milano"],
    "it_north":  ["Rome", "Roma", "Turin", "Torino", "Brescia"],
    "it_rest":   [
        "Florence", "Firenze", "Bologna", "Naples", "Napoli", "Genoa", "Palermo", "Bari",
        "Verona", "Padova", "Padua", "Venice", "Venezia",
        "Trento", "Bergamo", "Modena", "Parma", "Reggio Emilia",
        "Catania", "Trieste",
        "Italy", "Italia",
    ],
    "ch_zurich": ["Zurich", "Zuerich"],
    "ch_rest":   ["Geneva", "Geneve", "Basel", "Bern", "Lausanne", "Lugano", "Switzerland", "Schweiz"],
}

_stop_requested = False


def _install_signal_handler() -> None:
    def _handler(sig, frame):
        global _stop_requested
        print("\n[run_local] Ctrl-C received — finishing current profile then stopping...")
        _stop_requested = True
    signal.signal(signal.SIGINT, _handler)


def _fmt_rate(count: int, elapsed: float) -> str:
    if elapsed < 1:
        return "—"
    rate = count / elapsed * 3600
    return f"{rate:.0f}/hr"


def _print_combo_header(location: str, language: str) -> None:
    print_section_header(f"{location} × {language}")


def _print_progress(shard: str, total: int, start_time: float, last_count: int) -> None:
    elapsed = time.monotonic() - start_time
    rate = _fmt_rate(total, elapsed)
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"\n[{ts}] [{shard}] running total: {total} accepted | {rate}", flush=True)


def run_shard(shard: str, locations: list[str], pool: TokenPool, dry_run: bool) -> int:
    pending = get_pending_combos(locations, _LANGUAGES)

    if not pending:
        print(f"[{shard}] Nothing pending — already complete.")
        return 0

    print(f"[{shard}] {len(pending)} combos pending.")

    if dry_run:
        for loc, lang in pending:
            print(f"  {loc} × {lang}")
        return 0

    total_upserted = 0
    seen: set[str] = set()
    start_time = time.monotonic()
    last_reported = 0

    for location, language in pending:
        if _stop_requested:
            break

        combo_upserted = 0
        pages_fetched = 0
        last_page_count = 0
        _print_combo_header(location, language)

        for page in range(1, 11):
            if _stop_requested:
                break

            token, _ = pool.acquire()
            logins = github_search_users_page(location, language, page, token)

            if not logins:
                break

            pages_fetched += 1
            last_page_count = len(logins)

            for login in logins:
                if _stop_requested:
                    break
                if login in seen:
                    continue
                seen.add(login)

                token, _ = pool.acquire()
                profile = fetch_profile(login, token)
                if not profile:
                    continue

                # Pre-compute score to decide + log before DB write
                try:
                    ts = calculate_talent_score(profile)
                    grade, score = ts.grade, ts.overall
                except Exception:
                    continue  # can't score → skip

                accepted = upsert_profile(profile, source="github_broad")
                if accepted:
                    # Attach role_signals for log display (upsert_profile computes them internally)
                    from indexer.role_signals import infer_role_signals
                    profile["role_signals"] = infer_role_signals(profile)
                    log_accepted(profile, grade, score)
                    combo_upserted += 1
                    total_upserted += 1

                # Progress summary every 50 accepted
                if total_upserted > 0 and total_upserted % 50 == 0 and total_upserted != last_reported:
                    _print_progress(shard, total_upserted, start_time, last_reported)
                    last_reported = total_upserted

        completed = pages_fetched > 0 and last_page_count < 100
        mark_progress(location, language, pages_fetched, combo_upserted, completed=completed)
        print(f"\n  → {combo_upserted} accepted from {location} × {language} ({pages_fetched} pages)", flush=True)

    return total_upserted


def run_oss_topics(pool: TokenPool, dry_run: bool) -> int:
    if dry_run:
        from indexer.shards.oss_topics import _TOPICS
        print(f"[oss_topics] {len(_TOPICS)} topics to scan")
        for t in _TOPICS:
            print(f"  {t}")
        return 0

    # Pass a fake context with no timeout
    result = _run_with_no_deadline("oss_topics")
    return result.get("upserted", 0)


def run_hackathon_refresh(pool: TokenPool, dry_run: bool) -> int:
    if dry_run:
        from indexer.shards.hackathon_refresh import _HACKATHON_ORGS
        print(f"[hackathon_refresh] orgs: {_HACKATHON_ORGS}")
        return 0

    result = _run_with_no_deadline("hackathon_refresh")
    return result.get("upserted", 0)


def _run_with_no_deadline(shard: str) -> dict:
    """Run a special shard with a generous (24h) deadline so it never times out locally."""
    import time as _time

    class _FakeContext:
        def get_remaining_time_in_millis(self):
            return 24 * 3600 * 1000  # 24h

    if shard == "oss_topics":
        from indexer.shards.oss_topics import run
        return run(_FakeContext())
    elif shard == "hackathon_refresh":
        from indexer.shards.hackathon_refresh import run
        return run(_FakeContext())

    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Mirai talent indexer — local runner")
    parser.add_argument(
        "--shard",
        default="all",
        choices=list(_SHARDS.keys()) + ["oss_topics", "hackathon_refresh", "all"],
        help="Which shard to run (default: all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show pending combos without indexing",
    )
    args = parser.parse_args()

    _install_signal_handler()

    pool = TokenPool.from_env()

    print(f"[run_local] Starting. Shard={args.shard}  dry_run={args.dry_run}")
    print(f"[run_local] {datetime.now(timezone.utc).isoformat()}")

    if not args.dry_run:
        summary_before = index_summary()
        print(f"[run_local] Index before: {summary_before}")

    start = time.monotonic()
    grand_total = 0

    shards_to_run = list(_SHARDS.keys()) if args.shard == "all" else [args.shard]

    for shard in shards_to_run:
        if _stop_requested:
            break

        if shard == "oss_topics":
            grand_total += run_oss_topics(pool, args.dry_run)
        elif shard == "hackathon_refresh":
            grand_total += run_hackathon_refresh(pool, args.dry_run)
        else:
            locations = _SHARDS[shard]
            grand_total += run_shard(shard, locations, pool, args.dry_run)

    elapsed = time.monotonic() - start

    if not args.dry_run:
        summary_after = index_summary()
        print(f"\n{'═'*100}")
        print(f"  Run complete in {elapsed/60:.1f} min  |  accepted this run: {grand_total}")
        print(f"  Index totals → Italy: {summary_after['italy']}  Switzerland: {summary_after['switzerland']}  Total: {summary_after['total']}")
        print(f"{'═'*100}\n")
    else:
        print(f"\n[run_local] Dry run complete.")


if __name__ == "__main__":
    main()
