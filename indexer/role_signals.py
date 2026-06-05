"""
role_signals.py — infer role-type signals from GitHub profiles and LinkedIn enrichments.

No LLM calls. Pure heuristic based on:
  - GitHub: repo topics, bio keywords, and language mix  (infer_role_signals)
  - LinkedIn: current title and past position titles     (infer_role_signals_from_linkedin)

Signals stored on talent_index.role_signals:
  ml_engineer_signal    — Python + ML libraries / topics / titles
  devops_signal         — Kubernetes, Terraform, Docker repos / topics / titles
  fullstack_signal      — TypeScript/JS frontend + backend language
  backend_signal        — Go, Rust, Java, Python with no frontend pattern
  fde_signal            — SDK/integration repos / customer-facing titles

A profile can have multiple signals. LinkedIn signals are unioned with GitHub signals;
they never replace them (both sources of evidence are valid).
"""

from __future__ import annotations

import re

# ── Keyword sets ──────────────────────────────────────────────────────────────

_ML_TOPICS = {
    "pytorch", "tensorflow", "sklearn", "scikit-learn", "huggingface",
    "machine-learning", "deep-learning", "llm", "transformers", "mlops",
    "computer-vision", "nlp", "reinforcement-learning", "data-science",
    "jupyter", "kaggle", "pandas", "numpy",
}
_ML_BIO_KEYWORDS = re.compile(
    r"\b(machine.?learning|deep.?learning|ai|ml|data.?sci|llm|nlp|neural|"
    r"pytorch|tensorflow|hugging.?face|kaggle)\b",
    re.IGNORECASE,
)

_DEVOPS_TOPICS = {
    "kubernetes", "k8s", "terraform", "docker", "infrastructure-as-code",
    "devops", "helm", "ansible", "ci-cd", "github-actions", "gitlab-ci",
    "monitoring", "prometheus", "grafana", "platform-engineering",
}
_DEVOPS_BIO_KEYWORDS = re.compile(
    r"\b(devops|sre|platform|infra|kubernetes|k8s|terraform|docker|cloud)\b",
    re.IGNORECASE,
)

_FRONTEND_TOPICS = {
    "react", "nextjs", "vue", "svelte", "angular", "typescript",
    "javascript", "frontend", "web-development",
}
_BACKEND_LANGUAGES = {"Python", "Go", "Rust", "Java", "Kotlin", "Ruby", "Scala", "Elixir", "C++"}
_FRONTEND_LANGUAGES = {"TypeScript", "JavaScript"}

_FDE_TOPICS = {
    "sdk", "api-client", "integration", "webhook", "zapier", "demo",
    "customer", "onboarding", "implementation",
}
_FDE_BIO_KEYWORDS = re.compile(
    r"\b(solutions?.?eng|forward.?deploy|implementation|customer.?success|"
    r"technical.?account|sales.?eng|pre.?sales|professional.?services)\b",
    re.IGNORECASE,
)


# ── Main inference function ───────────────────────────────────────────────────

def infer_role_signals(profile: dict) -> list[str]:
    """
    Given a profile dict (output of _parse_profile), return a list of role signal strings.
    Called once at index time. Zero API calls.

    Args:
        profile: dict with keys: profile, languages, pinnedProjects, contributions, github_data

    Returns:
        e.g. ['ml_engineer_signal', 'backend_signal']
    """
    signals: list[str] = []

    p = profile.get("profile", {})
    bio: str = (p.get("bio") or "").lower()
    lang_names: set[str] = {l["name"] for l in (profile.get("languages") or [])}

    # Repo topics from github_data (stored on the raw JSONB but not in parse output)
    # We extract from github_data directly if available
    github_data = profile.get("github_data") or {}
    repos = github_data.get("repositories", {}).get("nodes", []) or []
    all_topics: set[str] = set()
    for repo in repos:
        for t in (repo.get("repositoryTopics", {}).get("nodes") or []):
            topic_name = t.get("topic", {}).get("name", "").lower()
            if topic_name:
                all_topics.add(topic_name)

    # Pinned project names and descriptions for topic hints
    pinned_text = " ".join(
        f"{p.get('name','')} {p.get('description','')}"
        for p in (profile.get("pinnedProjects") or [])
    ).lower()

    combined_text = f"{bio} {pinned_text}"

    # ── ML engineer ──────────────────────────────────────────────────────────
    ml_topic_hit = bool(all_topics & _ML_TOPICS)
    ml_bio_hit = bool(_ML_BIO_KEYWORDS.search(combined_text))
    ml_lang_hit = "Python" in lang_names
    if (ml_topic_hit or ml_bio_hit) and ml_lang_hit:
        signals.append("ml_engineer_signal")

    # ── DevOps / Platform ────────────────────────────────────────────────────
    devops_topic_hit = bool(all_topics & _DEVOPS_TOPICS)
    devops_bio_hit = bool(_DEVOPS_BIO_KEYWORDS.search(combined_text))
    if devops_topic_hit or devops_bio_hit:
        signals.append("devops_signal")

    # ── Full-Stack ───────────────────────────────────────────────────────────
    has_frontend_lang = bool(lang_names & _FRONTEND_LANGUAGES)
    has_backend_lang = bool(lang_names & _BACKEND_LANGUAGES)
    has_frontend_topic = bool(all_topics & _FRONTEND_TOPICS)
    if has_frontend_lang and (has_backend_lang or has_frontend_topic):
        signals.append("fullstack_signal")

    # ── Backend ──────────────────────────────────────────────────────────────
    # Backend: strong backend language, no significant frontend signals
    is_backend_dominant = bool(lang_names & _BACKEND_LANGUAGES) and not has_frontend_topic
    if is_backend_dominant and "ml_engineer_signal" not in signals and "devops_signal" not in signals:
        signals.append("backend_signal")

    # ── FDE / Solutions Engineer ─────────────────────────────────────────────
    fde_topic_hit = bool(all_topics & _FDE_TOPICS)
    fde_bio_hit = bool(_FDE_BIO_KEYWORDS.search(combined_text))
    if fde_topic_hit or fde_bio_hit:
        signals.append("fde_signal")

    return signals


def compute_activity_score(profile: dict) -> int:
    """
    Pre-computed activity score (0–100). Used for ordering in search results.

    Formula:
      up to 50 pts: commit volume (commits / 10, capped at 50)
      up to 30 pts: OSS contribution count (oss_count * 5, capped at 30)
      up to 10 pts: recency bonus (<90d=10, <180d=5, else=0)
      up to 10 pts: max starred repo (stars / 10, capped at 10)
    """
    contrib = profile.get("contributions", {})
    commits = contrib.get("commits", 0)
    oss_count = contrib.get("openSourceRepoCount", 0)
    total_contributions = profile.get("activityHeatmap", {}).get("totalContributions", 0)

    commit_pts = min(commits // 10, 50)
    oss_pts = min(oss_count * 5, 30)

    # Recency: use total_contributions as a proxy (high = recently active)
    recency_pts = 10 if total_contributions > 200 else 5 if total_contributions > 50 else 0

    star_pts = min(_max_repo_stars(profile) // 10, 10)

    return commit_pts + oss_pts + recency_pts + star_pts


def _max_repo_stars(profile: dict) -> int:
    """Max stars on any owned repo."""
    github_data = profile.get("github_data") or {}
    repos = github_data.get("repositories", {}).get("nodes", []) or []
    if not repos:
        # Fall back to pinnedProjects
        return max(
            (p.get("stargazerCount", 0) for p in (profile.get("pinnedProjects") or [])),
            default=0,
        )
    return max((r.get("stargazerCount", 0) for r in repos), default=0)


# ── LinkedIn role signal inference ────────────────────────────────────────────

_LI_ML_TITLES = re.compile(
    r"\b(machine.?learning|ml.?engineer|ai.?engineer|data.?scientist|"
    r"research.?engineer|nlp.?engineer|llm|deep.?learning|computer.?vision|"
    r"mlops|ml.?platform|applied.?scientist|quantitative)\b",
    re.IGNORECASE,
)
_LI_DEVOPS_TITLES = re.compile(
    r"\b(devops|site.?reliability|sre|platform.?engineer|infrastructure|"
    r"cloud.?engineer|devsecops|kubernetes|k8s|terraform|systems.?engineer)\b",
    re.IGNORECASE,
)
_LI_FULLSTACK_TITLES = re.compile(
    r"\b(full.?stack|fullstack|frontend.?&.?backend|web.?developer)\b",
    re.IGNORECASE,
)
_LI_BACKEND_TITLES = re.compile(
    r"\b(backend|back.?end|server.?side|software.?engineer|software.?developer|"
    r"api.?engineer|distributed.?systems|golang|rust.?engineer)\b",
    re.IGNORECASE,
)
_LI_FDE_TITLES = re.compile(
    r"\b(solutions?.?engineer|forward.?deploy|field.?engineer|"
    r"implementation.?engineer|technical.?account|sales.?engineer|"
    r"pre.?sales|professional.?services|customer.?engineer)\b",
    re.IGNORECASE,
)


def infer_role_signals_from_linkedin(enrichment: object) -> list[str]:
    """
    Infer role signals from a parsed LinkedInEnrichment object.
    Receives the enrichment model (output of parse_harvestapi_response), not the raw dict.
    Returns a list of signal strings — same vocabulary as infer_role_signals().
    Returns [] if enrichment is None or has no usable title data.

    Replace semantics: when this function returns a non-empty list, the caller should
    REPLACE talent_index.role_signals with this result (LinkedIn job titles are more
    reliable than GitHub repo topic heuristics). When this returns [], leave existing
    signals unchanged.

    Example:
        li_signals = infer_role_signals_from_linkedin(parsed_enrichment)
        if li_signals:
            talent_index.role_signals = li_signals  # replace
        # else: keep existing GitHub-inferred signals unchanged
    """
    if enrichment is None:
        return []

    # Collect all title strings: current + all past positions
    titles: list[str] = []
    current_title = getattr(enrichment, "current_title", None)
    if current_title:
        titles.append(current_title)

    positions = getattr(enrichment, "positions", None) or []
    for pos in positions:
        title = getattr(pos, "title", None) or (pos.get("title") if isinstance(pos, dict) else None)
        if title:
            titles.append(title)

    if not titles:
        return []

    combined = " | ".join(titles)
    signals: list[str] = []

    if _LI_ML_TITLES.search(combined):
        signals.append("ml_engineer_signal")
    if _LI_DEVOPS_TITLES.search(combined):
        signals.append("devops_signal")
    if _LI_FULLSTACK_TITLES.search(combined):
        signals.append("fullstack_signal")
    if _LI_BACKEND_TITLES.search(combined):
        signals.append("backend_signal")
    if _LI_FDE_TITLES.search(combined):
        signals.append("fde_signal")

    return signals
