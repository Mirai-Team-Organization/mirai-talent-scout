"""
score_candidate_rubric — job-posting-aware scoring pipeline.

Aligned with the ai-proxy /candidate/evaluate scoring model.

Given a candidate profile and a TalentBrief, computes:
  1. score_skill_match       — semantic skill overlap (0-100)
  2. score_experience_depth  — relevance and depth of work history (0-100)
  3. score_potential         — growth trajectory + OSS signals (0-100)
  4. overall_match_pct       — skill 37.5% + exp 37.5% + potential 25%
  5. deal_breakers_detail    — per-item boolean evaluation
  6. must_haves_met/gap      — lists of met and missing must-haves
  7. recruiter_note          — 2-3 sentence narrative for the hiring manager
  8. flag                    — misaligned | high_potential | strong_fit
  9. salary_fit              — MATCH | ABOVE_RANGE | BELOW_RANGE | UNKNOWN
 10. location_fit            — 0-100

fit_score is kept as an alias for overall_match_pct for backward compatibility.
"""

from __future__ import annotations

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
import botocore.config
from strands import tool

# Thread-local progress callback — set by _ToolWithSSE in stream_app before invoking the tool.
# Signature: fn(done: int, total: int) -> None
_progress: threading.local = threading.local()

from scoring.hiring_context import _score_location
from scoring.salary_benchmarks import benchmark_range

# ── Bedrock client ─────────────────────────────────────────────────────────────

_bedrock = None
_bedrock_lock = threading.Lock()


def _get_bedrock():
    global _bedrock
    if _bedrock is None:
        with _bedrock_lock:
            if _bedrock is None:
                _bedrock = boto3.client(
                    "bedrock-runtime",
                    region_name=os.environ.get("AWS_REGION", "eu-west-1"),
                    config=botocore.config.Config(
                        read_timeout=15,
                        connect_timeout=5,
                    ),
                )
    return _bedrock


def _haiku(system: str, user: str, max_tokens: int = 200) -> str:
    model_id = os.environ.get(
        "BEDROCK_HAIKU_MODEL", "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
    )
    resp = _get_bedrock().converse(
        modelId=model_id,
        system=[{"text": system}],
        messages=[{"role": "user", "content": [{"text": user}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
    )
    return resp["output"]["message"]["content"][0]["text"].strip()


# ── Dealbreaker pre-filter ─────────────────────────────────────────────────────

# ── Combined dealbreaker + criteria scoring (single Haiku call) ───────────────

_SCORE_SYSTEM = """You are a senior hiring expert evaluating a candidate for a specific job opening.
Your output feeds directly into a recruiter dashboard.
Be objective, evidence-based, and calibrated. Do not invent information not present in the inputs.
The candidate summary includes GitHub activity and a Career history section scraped from LinkedIn.

Score on 3 dimensions and evaluate the hiring rubric items.

1. score_skill_match (0-100): How well the candidate's skills match the job's required skills.
   Calibration:
   - Semantic equivalence counts: Firebase ≈ Supabase (BaaS), Django ≈ FastAPI (Python web frameworks).
   - Partial overlaps score proportionally. A candidate with TypeScript frontend experience but no Node.js
     backend is NOT the same as having no TypeScript at all — score the overlap, not the gap.
   - Adjacent technologies in the same paradigm (e.g. Java/Spring Boot → Node.js) represent a ramp,
     not an absence. Weight the ramp size against the candidate's learning signals (OSS, side projects).
   note_skill_match: One sentence max 20 words — cite the key skill evidence.

2. score_experience_depth (0-100): Depth, seniority, and direct relevance of their work history.
   note_experience_depth: One sentence max 20 words — cite the most relevant past role.

3. score_potential (0-100): Growth trajectory, learning velocity, and future capability signals.
   ALWAYS use LinkedIn career signals as the primary source when present: promotions per year,
   scope expansion (IC→manager, engineer→tech lead), title progression, decreasing tenure with
   increasing seniority. When GitHub data is also available, use OSS contributions, side projects,
   and new technology adoption as additive signals. When only GitHub data is present (no LinkedIn
   career history), use OSS contributions and project diversity as the primary proxy.
   note_potential: One sentence max 20 words — cite the key signal (LinkedIn trajectory or GitHub OSS).

4. deal_breakers_detail: For EACH listed deal-breaker, evaluate whether the candidate satisfies it.
   A deal-breaker is a NEGATIVE statement (e.g. "No Node.js experience") — it is MET if the candidate
   does NOT exhibit that disqualifier. If deal_breakers is empty, return an empty array.
   IMPORTANT: Only mark met=false if there is CLEAR EVIDENCE the candidate fails this requirement.
   Absence of evidence is NOT evidence of failure — if you cannot tell from the profile, mark met=true.
   Reserve met=false for unambiguous disqualifiers (e.g. candidate explicitly works in a different domain,
   or their entire career history shows no overlap with the requirement).
   VERSION EQUIVALENCE: For version-specific technology dealbreakers, treat same-major-framework
   releases as semantically equivalent. Examples:
   - "No Next.js App Router v15+ experience" → met=true if candidate has ANY Next.js App Router
     experience (v13+), since App Router was introduced in v13.4 and the architecture is stable
     across v13/v14/v15. Lack of an explicit "v15" mention is NOT a disqualifier.
   - "No React 18+ experience" → met=true if candidate has React experience (v16/v17/v18 share
     the same core paradigm). Apply this logic to all framework version dealbreakers.
   LANGUAGE FLUENCY: "Not fluent in Italian/French/Spanish/etc." → if candidate is based in that
   country (Italy, France, Spain, etc.) or has worked there, mark met=true unless there is
   explicit evidence they do NOT speak the language.

5. must_haves_met: List (by exact text) each must-have the candidate clearly satisfies.
   must_haves_gap: List (by exact text) each must-have the candidate is missing or only partially meets.

6. nice_to_haves_met: List which nice-to-haves the candidate satisfies (exact text).

7. recruiter_note: 2-3 sentences for the hiring manager. Cover the profile strength, the most relevant
   past role, and the most important gap. Distinguish hard absences (never touched the domain) from
   rampable gaps (adjacent experience + learning signals present).

Reply ONLY with valid JSON, no markdown fences:
{
  "score_skill_match": <0-100>,
  "note_skill_match": "...",
  "score_experience_depth": <0-100>,
  "note_experience_depth": "...",
  "score_potential": <0-100>,
  "note_potential": "...",
  "deal_breakers_detail": [{"item": "<exact text>", "met": true|false}],
  "must_haves_met": ["<exact text>"],
  "must_haves_gap": ["<exact text>"],
  "nice_to_haves_met": ["<exact text>"],
  "recruiter_note": "..."
}"""


def _evaluate_candidate(
    deal_breakers: list[str],
    must_haves: list[str],
    nice_to_haves: list[str],
    candidate_summary: str,
    role_context: str = "",
) -> dict:
    """
    Single Haiku call: scores 3 dimensions, evaluates rubric items, generates recruiter_note.
    Returns parsed assessment dict. Fails open on errors (all scores 0, empty lists).
    """
    _EMPTY = {
        "score_skill_match": 0, "note_skill_match": "",
        "score_experience_depth": 0, "note_experience_depth": "",
        "score_potential": 0, "note_potential": "",
        "deal_breakers_detail": [],
        "must_haves_met": [], "must_haves_gap": list(must_haves),
        "nice_to_haves_met": [],
        "recruiter_note": "",
    }
    try:
        db_str = "\n".join(f"  - {d}" for d in deal_breakers) or "  None specified"
        mh_str = "\n".join(f"  - {m}" for m in must_haves) or "  None specified"
        nh_str = "\n".join(f"  - {n}" for n in nice_to_haves) or "  None"

        user_msg = (
            f"{role_context}\n\n" if role_context else ""
        ) + (
            f"Deal-breakers:\n{db_str}\n\n"
            f"Must-haves:\n{mh_str}\n\n"
            f"Nice-to-haves:\n{nh_str}\n\n"
            f"Candidate:\n{candidate_summary}"
        )

        raw = _haiku(_SCORE_SYSTEM, user_msg, max_tokens=1800)
        cleaned = re.sub(r"```[a-z]*\n?", "", raw).strip()

        # Attempt JSON repair for truncated responses
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            repaired = cleaned.rstrip(",").rstrip()
            opens = repaired.count("{") - repaired.count("}")
            arr_opens = repaired.count("[") - repaired.count("]")
            repaired += "]" * max(arr_opens, 0) + "}" * max(opens, 0)
            data = json.loads(repaired)

        if not isinstance(data, dict):
            raise ValueError("not a dict")

        def _clamp(v, lo=0, hi=100):
            try:
                return max(lo, min(hi, int(round(float(v)))))
            except (TypeError, ValueError):
                return 0

        db_detail = [
            {"item": str(d.get("item", ""))[:200], "met": bool(d.get("met", False))}
            for d in (data.get("deal_breakers_detail") or [])
            if isinstance(d, dict)
        ]

        return {
            "score_skill_match":      _clamp(data.get("score_skill_match", 0)),
            "note_skill_match":       str(data.get("note_skill_match", ""))[:200],
            "score_experience_depth": _clamp(data.get("score_experience_depth", 0)),
            "note_experience_depth":  str(data.get("note_experience_depth", ""))[:200],
            "score_potential":        _clamp(data.get("score_potential", 0)),
            "note_potential":         str(data.get("note_potential", ""))[:200],
            "deal_breakers_detail":   db_detail,
            "must_haves_met":         [str(m)[:200] for m in (data.get("must_haves_met") or []) if m],
            "must_haves_gap":         [str(m)[:200] for m in (data.get("must_haves_gap") or []) if m],
            "nice_to_haves_met":      [str(m)[:200] for m in (data.get("nice_to_haves_met") or []) if m],
            "recruiter_note":         str(data.get("recruiter_note", ""))[:600],
        }

    except Exception as e:
        print(f"[score_candidate_rubric] Evaluation failed: {e}")
        return _EMPTY


# ── Candidate summary builder ─────────────────────────────────────────────────

def _build_candidate_summary(profile: dict, job_description: str = "") -> str:
    """
    Build a compact text summary of the candidate for Haiku prompts.
    Works for both GitHub profiles and internal Mirai profiles.
    """
    p = profile.get("profile", {})
    parts = []

    name = p.get("name") or p.get("login", "Candidate")
    parts.append(name)

    if p.get("bio"):
        parts.append(p["bio"][:200])

    # Internal profile fields
    job_role = profile.get("job_role")
    if job_role:
        parts.append(f"Role: {job_role}")

    seniority = profile.get("seniority")
    if seniority:
        parts.append(f"Seniority: {seniority}")

    # Skills
    all_skills = profile.get("all_skills") or []
    if all_skills:
        parts.append(f"Skills: {', '.join(all_skills[:10])}")
    else:
        langs = [l["name"] for l in (profile.get("languages") or [])]
        if langs:
            parts.append(f"Languages: {', '.join(langs[:5])}")

    # Work experience — most recent 3
    experiences = profile.get("experiences") or []
    if experiences:
        recent = experiences[:3]
        exp_text = "; ".join(
            f"{e.get('title', '')} at {e.get('company', '')} ({e.get('startDate', '')}–{e.get('endDate', 'present')})"
            for e in recent
            if e.get("title") or e.get("company")
        )
        if exp_text:
            parts.append(f"Experience: {exp_text}")

    # GitHub signals for external candidates
    contrib = profile.get("contributions", {})
    commits = contrib.get("commits", 0)
    if commits > 0:
        oss = contrib.get("openSourceRepoCount", 0)
        parts.append(f"GitHub: {commits} commits, {oss} OSS repos")

    # LinkedIn enrichment signals (available post-enrich_linkedin)
    li = profile.get("linkedin") or {}
    cs = profile.get("career_signals") or {}

    li_title   = li.get("current_title")
    li_company = li.get("current_company")
    if li_title or li_company:
        parts.append(f"LinkedIn: {' @ '.join(filter(None, [li_title, li_company]))}")

    # career_summary is pre-built at index time (with descriptions) — use it directly.
    # Falls back to looping positions if not present (e.g. internal candidates).
    career_summary = li.get("career_summary") or ""
    if career_summary:
        parts.append("Career history:\n" + career_summary)
    else:
        li_positions = li.get("positions") or []
        if li_positions:
            pos_parts = []
            for pos in li_positions[:5]:
                if not (pos.get("title") or pos.get("company")):
                    continue
                header = f"{pos.get('title', '')} at {pos.get('company', '')} ({pos.get('start_date', '')}–{pos.get('end_date', 'present')})"
                desc = pos.get("description", "")
                pos_parts.append(f"{header}: {desc}" if desc else header)
            if pos_parts:
                parts.append("Career history:\n" + "\n".join(f"- {p}" for p in pos_parts))

    li_seniority = cs.get("seniority_level")
    if li_seniority:
        parts.append(f"Seniority (LinkedIn): {li_seniority}")

    trajectory = cs.get("career_trajectory")
    if trajectory and trajectory != "insufficient_data":
        parts.append(f"Career trajectory: {trajectory}")

    if li.get("open_to_work"):
        parts.append("Open to work: yes")

    li_languages = li.get("languages_spoken") or []
    if li_languages:
        parts.append(f"Languages spoken: {', '.join(li_languages[:4])}")

    location = p.get("location") or li.get("location")
    if location:
        parts.append(f"Location: {location}")

    # Data freshness — helps Haiku calibrate confidence on stale profiles
    fetched_at = li.get("fetched_at")
    if fetched_at:
        parts.append(f"LinkedIn data fetched: {str(fetched_at)[:10]}")

    if job_description:
        parts.append(f"Job description context:\n{job_description[:1500]}")

    return ". ".join(parts)


# ── Salary fit ────────────────────────────────────────────────────────────────

def _check_salary_fit(
    candidate_salary_expectation: float | None,
    brief_min: float | None,
    brief_max: float | None,
    market: str,
    seniority: str,
) -> tuple[str, float]:
    """
    Returns (verdict, adjustment):
      MATCH        →  +5
      ABOVE_RANGE  → −10  (candidate costs more than the role pays)
      BELOW_RANGE  →   0  (candidate is cheaper — usually fine)
      UNKNOWN      →   0  (no salary data on either side)

    Uses benchmark_range() if the brief has no salary data.
    """
    # Resolve range to compare against
    min_eur = brief_min
    max_eur = brief_max
    if min_eur is None and max_eur is None:
        rng = benchmark_range(market, seniority)
        if rng:
            min_eur, max_eur = rng

    if candidate_salary_expectation is None or (min_eur is None and max_eur is None):
        return "UNKNOWN", 0.0

    if max_eur is not None and candidate_salary_expectation > max_eur:
        return "ABOVE_RANGE", -10.0
    if min_eur is not None and candidate_salary_expectation < min_eur:
        return "BELOW_RANGE", 0.0
    return "MATCH", 5.0


# ── Main tool ─────────────────────────────────────────────────────────────────

def _score_single(
    profile: dict,
    talent_brief: dict,
    candidate_salary_expectation: float | None = None,
) -> dict:
    """Score one candidate profile against a TalentBrief. Returns enriched profile dict."""
    dealbreaker_text: str = talent_brief.get("dealbreaker_text", "")
    rubric_text: str = talent_brief.get("rubric_text", "")
    target_location: str = talent_brief.get("location", "")
    seniority: str = talent_brief.get("seniority", "mid")
    market: str = talent_brief.get("salary_market") or "EU"
    brief_min: float | None = talent_brief.get("salary_min")
    brief_max: float | None = talent_brief.get("salary_max")
    job_description: str = talent_brief.get("job_description", "")

    # ── Salary fit (for context + display) ────────────────────────────────────
    salary_fit, _ = _check_salary_fit(
        candidate_salary_expectation, brief_min, brief_max, market, seniority
    )

    # ── Location fit (for context + hard-gate in rank_shortlist) ─────────────
    li_data = profile.get("linkedin") or {}
    candidate_location = (
        profile.get("profile", {}).get("location")
        or li_data.get("location")
    )
    location_fit = _score_location(target_location or None, candidate_location)
    if location_fit is None and talent_brief.get("remote_eligible"):
        location_fit = 70.0

    # ── Build role context for LLM ────────────────────────────────────────────
    salary_desc = ""
    if brief_min or brief_max:
        parts = []
        if brief_min:
            parts.append(f"€{int(brief_min):,}")
        if brief_max:
            parts.append(f"€{int(brief_max):,}")
        salary_desc = f"Salary range: {' – '.join(parts)}"
        if salary_fit == "ABOVE_RANGE":
            salary_desc += f" (candidate expects above this range)"
        elif salary_fit == "BELOW_RANGE":
            salary_desc += f" (candidate is below this range — usually acceptable)"

    remote_str = ", remote eligible" if talent_brief.get("remote_eligible") else ", in-person/hybrid"
    location_str = f"Role location: {target_location or 'unspecified'}{remote_str}"
    if candidate_location:
        location_str += f" | Candidate location: {candidate_location}"

    role_context_parts = [
        f"Role: {talent_brief.get('title', '')} ({seniority})",
        location_str,
    ]
    if salary_desc:
        role_context_parts.append(salary_desc)
    role_context = "\n".join(role_context_parts)

    candidate_summary = _build_candidate_summary(profile, job_description=job_description)

    # ── Single Haiku call: 3-dimension scoring + rubric evaluation ────────────
    hiring_rubric: dict = talent_brief.get("hiring_rubric") or {}
    must_haves: list[str] = hiring_rubric.get("mustHaves") or []
    nice_to_haves: list[str] = hiring_rubric.get("niceToHaves") or []
    deal_breakers: list[str] = (
        [s.strip() for s in dealbreaker_text.split(",")
         if s.strip() and len(s.strip().split()) >= 3]  # skip fragments from sentence commas
        if dealbreaker_text else []
    )
    # Fall back to rubric_text if no structured must_haves
    if not must_haves and rubric_text.strip():
        must_haves = [s.strip() for s in re.split(r"[;,]", rubric_text) if s.strip()][:6]

    assessment = _evaluate_candidate(
        deal_breakers, must_haves, nice_to_haves, candidate_summary, role_context
    )

    # ── Derive dealbreaker_hit from per-item detail ───────────────────────────
    db_detail = assessment["deal_breakers_detail"]
    dealbreaker_hit = bool(db_detail) and not all(d["met"] for d in db_detail)

    if dealbreaker_hit:
        return {
            **profile,
            "fit_score":              0,
            "overall_match_pct":      0,
            "score_skill_match":      0,
            "score_experience_depth": 0,
            "score_potential":        0,
            "deal_breakers_detail":   db_detail,
            "must_haves_met":         [],
            "must_haves_gap":         must_haves,
            "nice_to_haves_met":      [],
            "recruiter_note":         assessment["recruiter_note"],
            "salary_fit":             salary_fit,
            "location_fit":           location_fit,
            "dealbreaker_hit":        True,
            "flag":                   "misaligned",
        }

    # ── overall_match_pct — aligned with ai-proxy (no interview dimension) ───
    # skill 37.5% + experience 37.5% + potential 25%
    s_skill = assessment["score_skill_match"]
    s_exp   = assessment["score_experience_depth"]
    s_pot   = assessment["score_potential"]
    overall = round(s_skill * 0.375 + s_exp * 0.375 + s_pot * 0.25)

    # ── Flag — mirrors ai-proxy logic (no hiring_mode context here) ──────────
    if overall < 25:
        flag = "misaligned"
    elif overall >= 75:
        flag = "strong_fit"
    else:
        flag = "high_potential"

    scored = {
        **profile,
        # Primary score (aliased for backward compat)
        "overall_match_pct":      overall,
        "fit_score":              overall,
        # Sub-scores
        "score_skill_match":      s_skill,
        "note_skill_match":       assessment["note_skill_match"],
        "score_experience_depth": s_exp,
        "note_experience_depth":  assessment["note_experience_depth"],
        "score_potential":        s_pot,
        "note_potential":         assessment["note_potential"],
        # Rubric evaluation
        "deal_breakers_detail":   db_detail,
        "must_haves_met":         assessment["must_haves_met"],
        "must_haves_gap":         assessment["must_haves_gap"],
        "nice_to_haves_met":      assessment["nice_to_haves_met"],
        "recruiter_note":         assessment["recruiter_note"],
        # Compatibility fields
        "salary_fit":             salary_fit,
        "location_fit":           location_fit,
        "dealbreaker_hit":        False,
        "flag":                   flag,
    }

    # Drop heavy blobs not needed post-scoring
    scored.pop("pinnedProjects", None)
    scored.pop("activityHeatmap", None)
    if scored.get("linkedin"):
        scored["linkedin"].pop("career_summary", None)

    return scored


@tool
def score_candidate_rubric(
    candidates: list[dict],
    talent_brief: dict,
) -> list[dict]:
    """
    Score ALL candidates against a TalentBrief in one call. Pass the complete list.

    Accepts the combined list of internal candidates (from search_internal_pool) and
    enriched index candidates (from enrich_linkedin). Scores every candidate with one
    Haiku call each: dealbreaker pre-filter + per-criterion rubric scoring.

    Args:
        candidates: All candidate profile dicts to score — internal + index combined.
                    Pass the full dicts exactly as returned by search_internal_pool()
                    and enrich_linkedin(). Do NOT pass individual profiles one at a time.
        talent_brief: TalentBrief dict from build_talent_brief()

    Returns:
        List of profile dicts, each enriched with:
          overall_match_pct       0–100  (skill 37.5% + exp 37.5% + potential 25%)
          fit_score               alias for overall_match_pct
          flag                    misaligned | high_potential | strong_fit
          score_skill_match       0–100
          score_experience_depth  0–100
          score_potential         0–100
          note_skill_match / note_experience_depth / note_potential  one-sentence notes
          deal_breakers_detail    list of {item, met}
          must_haves_met          list of met must-haves
          must_haves_gap          list of missing must-haves
          nice_to_haves_met       list
          recruiter_note          2-3 sentence narrative
          salary_fit              MATCH | ABOVE_RANGE | BELOW_RANGE | UNKNOWN
          location_fit            0–100 | None
          dealbreaker_hit         bool
    """
    total = len(candidates)
    _cb = getattr(_progress, "fn", None)
    done_count = 0
    done_lock = threading.Lock()

    def _score_with_index(idx_profile):
        idx, profile = idx_profile
        try:
            result = _score_single(profile, talent_brief)
        except Exception as e:
            print(f"[score_candidate_rubric] Failed for {profile.get('profile', {}).get('login', '?')}: {e}")
            result = {**profile,
                      "overall_match_pct": 0, "fit_score": 0,
                      "flag": "misaligned",
                      "score_skill_match": 0, "note_skill_match": "",
                      "score_experience_depth": 0, "note_experience_depth": "",
                      "score_potential": 0, "note_potential": "",
                      "deal_breakers_detail": [], "must_haves_met": [],
                      "must_haves_gap": [], "nice_to_haves_met": [],
                      "recruiter_note": "",
                      "salary_fit": "UNKNOWN", "location_fit": None,
                      "dealbreaker_hit": False}
        return idx, result

    max_workers = min(10, total) if total > 0 else 1
    results_map: dict[int, dict] = {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_score_with_index, (i, p)): i for i, p in enumerate(candidates)}
        for future in as_completed(futures):
            idx, scored = future.result()
            results_map[idx] = scored
            if _cb:
                with done_lock:
                    done_count += 1
                    n = done_count
                try:
                    _cb(n, total)
                except Exception:
                    pass

    return [results_map[i] for i in range(total)]
