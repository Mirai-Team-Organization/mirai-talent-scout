"""
oss_topics — find top repos by GitHub topic, extract IT/CH contributors.

Runs on Saturdays. Complements the broad user search with higher-signal
candidates: people who actually contribute to relevant open source projects.
"""

from __future__ import annotations

import json
import time
import urllib.request

from indexer.core import TokenPool, fetch_profile, upsert_profile, _deadline, infer_location
from indexer.display import log_accepted, print_section_header
from indexer.role_signals import infer_role_signals
from scoring.talent_scorer import calculate_talent_score

_TOPICS = [
    "pytorch", "fastapi", "react", "nextjs", "typescript",
    "kubernetes", "terraform", "llm", "django", "golang",
    "rust-lang", "swift", "kotlin", "flutter", "postgres",
    # ML/AI signal
    "machine-learning", "deep-learning", "tensorflow", "scikit-learn",
    "huggingface", "transformers", "computer-vision", "nlp", "data-science",
    # AI ecosystem builders
    "langchain", "llamaindex", "litellm", "crewai", "openai-api",
    "claude", "anthropic", "ollama", "RAG", "agents",
]

_MIN_STARS = 200
_MAX_REPOS_PER_TOPIC = 10
_MAX_CONTRIBUTORS_PER_REPO = 30


def _search_repos_by_topic(topic: str, token: str) -> list[dict]:
    """Return top repos for a topic (name, owner login)."""
    import urllib.parse
    q = urllib.parse.quote(f"topic:{topic} stars:>{_MIN_STARS}")
    url = f"https://api.github.com/search/repositories?q={q}&sort=stars&per_page={_MAX_REPOS_PER_TOPIC}"
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
        print(f"[oss_topics] repo search for {topic}: {e}")
        return []


def _get_contributors(owner: str, repo: str, token: str) -> list[str]:
    """Return top contributor logins for a repo."""
    url = f"https://api.github.com/repos/{owner}/{repo}/contributors?per_page={_MAX_CONTRIBUTORS_PER_REPO}"
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


def run(context) -> dict:
    pool = TokenPool.from_env()
    deadline = _deadline(context)
    seen: set[str] = set()
    upserted = 0

    for topic in _TOPICS:
        if time.monotonic() >= deadline:
            break

        print_section_header(f"topic:{topic}")
        token, _ = pool.acquire()
        repos = _search_repos_by_topic(topic, token)
        topic_upserted = 0

        for repo in repos:
            if time.monotonic() >= deadline:
                break

            token, _ = pool.acquire()
            logins = _get_contributors(repo["owner"], repo["name"], token)

            for login in logins:
                if login in seen:
                    continue
                seen.add(login)

                token, _ = pool.acquire()
                profile = fetch_profile(login, token)
                if not profile:
                    continue

                # Only index IT/CH
                country_code, _ = infer_location(profile.get("profile", {}).get("location"))
                if country_code not in ("IT", "CH"):
                    continue

                try:
                    ts = calculate_talent_score(profile)
                    grade, score = ts.grade, ts.overall
                except Exception:
                    continue

                accepted = upsert_profile(
                    profile,
                    source="github_oss",
                    source_details={"topic": topic, "repo": f"{repo['owner']}/{repo['name']}"},
                )
                if accepted:
                    profile["role_signals"] = infer_role_signals(profile)
                    log_accepted(profile, grade, score)
                    topic_upserted += 1
                    upserted += 1

        print(f"\n  → {topic_upserted} accepted from topic:{topic}", flush=True)

    return {"upserted": upserted, "remaining_combos": 0}
