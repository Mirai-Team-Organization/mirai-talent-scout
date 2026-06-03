# Mirai Talent Scout — Architecture Overview

> Last updated: 2026-06-03

---

## What it does

Mirai AI Talent Scout is an AI-powered recruiting orchestrator that finds developers, enriches their profiles with mobility signals, and returns a ranked shortlist. It runs as both an MCP service (for Claude Desktop) and a streaming chat API (for the built-in UI).

---

## Two operating modes

| Mode | Trigger | Flow |
|------|---------|------|
| **A — Job-Posting-Aware** | `job_posting_id` provided | Reads job posting → internal pool → GitHub → rubric scoring → LinkedIn enrichment → ranked shortlist |
| **B — Natural-Language Query** | Free-text query only | GitHub search → LinkedIn enrichment → talent scoring → ranked shortlist |

Mode A is preferred. It uses the hiring rubric as a filter, checks the internal talent pool first (zero API cost), and applies dealbreaker pre-filtering before expensive scoring.

---

## High-level architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        UI  (ui/index.html)                       │
│   Dark-theme chat · context selector · streaming SSE · cards    │
└─────────────────────────┬───────────────────────────────────────┘
                          │  SSE  POST /agent
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│           Streaming Chat Agent  (agent/stream_app.py)            │
│   FastAPI · Lambda Web Adapter · InvokeMode: RESPONSE_STREAM    │
│   Emits: tool_start · text_delta · candidates · done · error    │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│              Strands Agent Orchestrator  (agent/agent.py)        │
│   Model: Bedrock Sonnet 4.5  (eu-west-1 EU inference profile)   │
│   Lightweight tasks: Bedrock Haiku 4.5                          │
└──┬───────────┬───────────┬──────────────┬───────────────────────┘
   │           │           │              │
   ▼           ▼           ▼              ▼
GitHub      Supabase    Apify         AWS Bedrock
GraphQL    (PostgreSQL) (LinkedIn)    (Haiku calls)
```

---

## Tool pipeline

### Mode A (job-posting-aware)

```
build_talent_brief(job_posting_id)
  └─ 2× Haiku: flatten rubric, translate to GitHub query syntax
  └─ returns TalentBrief (rubric_text, dealbreaker_text, salary range, github_query)

search_internal_pool(talent_brief)
  └─ Supabase: user_working_profiles JOIN users
  └─ Zero API calls — always runs first

search_github(talent_brief.github_query, limit=20)
  └─ 1× Haiku: NL → GitHub search syntax (if not pre-translated)
  └─ GitHub REST /search/users + GraphQL per profile
  └─ Returns: languages, contributions (365d), pinned repos, activity heatmap

score_candidate_rubric(profile, talent_brief)  ← all candidates
  └─ 1× Haiku: dealbreaker pre-filter (YES/NO)
  └─ 1× Haiku: rubric match score (0–100)
  └─ Local: salary fit check, location fit (0–100)
  └─ fit_score = rubric_match × 0.60 + salary_adj + location_adj
  └─ Candidates with dealbreaker_hit=True are dropped here

enrich_linkedin(candidates with fit_score ≥ 40)
  └─ Apify: harvestapi/linkedin-profile-scraper ($0.004/profile)
  └─ Cache check first: linkedin_enrichments table (30d TTL)
  └─ Async batch (semaphore=5, 22s timeout, partial results OK)
  └─ Computes: mobility_score (0–100), career_signals

rank_shortlist(candidates)
  └─ Sort by: fit_score → mobility_score → talent_score → grade
```

### Mode B (natural-language query)

```
search_github(query) → enrich_linkedin → score_candidate → rank_shortlist
```

---

## Scoring systems

### Talent score (GitHub-derived, 5 dimensions)

| Dimension | Weight | Signal |
|-----------|--------|--------|
| Tech Stack | 30% | Languages used, code volume |
| Open Source | 25% | Contributions to external repos |
| Consistency | 20% | Active days + longest streak (365d) |
| Collaboration | 15% | PRs opened + PR reviews |
| Presentation | 10% | Bio, README, pinned repos |

Grade map: 90+ → S · 82+ → A+ · 74+ → A · 66+ → A- · 58+ → B+ · 50+ → B · 42+ → B- · 34+ → C+ · <34 → C

Hiring context (`startup_early` / `startup_growth` / `enterprise`) reweights the dimensions and applies a prestige penalty for over-qualified candidates (followers/stars above per-context ceiling).

### Rubric fit score (Mode A, LLM-assisted)

```
fit_score = rubric_match_score × 0.60
          + salary_adj  (−10 to +5)
          + location_adj (−15 to +15)
```

Capped at [0, 100].

### Mobility score (LinkedIn-derived)

"How keen is this person to move right now?"

| Signal | Weight | Logic |
|--------|--------|-------|
| Tenure at current role | 35% | Peak at 12–36 months; fades after 5 years |
| Career velocity | 25% | Few title changes → higher open-to-move |
| Job frequency | 25% | Avg 12–36 months/role = healthy mover |
| Company health | 15% | Placeholder (LayoffsFYI not yet wired) |

**Hard rule:** If LinkedIn `open_to_work = true` → mobility_score floored at 85.

`None` (no LinkedIn data found) is distinct from `0` (structural signals say not moving).

### Career signals (LinkedIn-derived)

- `years_of_experience`: months / 12 from earliest role
- `seniority_level`: inferred from current title (exec → lead → senior → mid → junior)
- `career_trajectory`: ascending / lateral / descending / insufficient_data
- `has_quantified_outcomes`: impact verb + metric near each other in about text

---

## Data models

```
TalentBrief         — job context fed to all Mode A tools
TalentScore         — overall + grade + 5 sub-scores + context adjustments
MobilityScore       — 0–100 + signals + completeness
LinkedInEnrichment  — positions, education, languages, open_to_work
CareerSignals       — YoE, seniority, trajectory, quantified outcomes
CandidateResult     — final output shape (username → fit_score → summary)
ShortlistResult     — list of CandidateResult + metadata
```

---

## Database (Supabase / PostgreSQL)

| Table | Purpose | TTL |
|-------|---------|-----|
| `candidates` | GitHub profile + talent score cache | 24h |
| `linkedin_enrichments` | Apify payload + mobility score | 30d |
| `github_api_usage` | Rate limit tracking per token | rolling |
| `company_job_postings` | Recruiter CRM — job postings | — |
| `role_scoring_config` | Per-role rubric dimension weights | — |
| `user_working_profiles` | Internal talent pool (CV data) | — |
| `users` | Internal team members | — |
| `talent_pipeline` | Recruiter pipeline stages | — |
| `search_sessions` | Audit trail (optional) | — |

Lambda connects via **PgBouncer pooler** (port 6543) to handle concurrent connection limits.

---

## External services

| Service | Purpose | Cost |
|---------|---------|------|
| **GitHub GraphQL** | Developer profiles, contributions | Free (5k req/hr per token) |
| **Apify** — harvestapi/linkedin-profile-scraper | LinkedIn enrichment | **$0.004 / profile** |
| **AWS Bedrock Sonnet 4.5** | Main orchestrator reasoning | ~$0.020 / session |
| **AWS Bedrock Haiku 4.5** | Query translation, rubric scoring, dealbreaker check | ~$0.00006 / call |
| **Supabase** | PostgreSQL + auth + pooler | Usage-based |

---

## Cost breakdown (per search)

Assumptions: 20 candidates from GitHub, 10 enriched (fit_score ≥ 40), 5 from internal pool.

| Step | Calls | Unit cost | Subtotal |
|------|-------|-----------|----------|
| `build_talent_brief` | 2× Haiku | $0.00006 | $0.00012 |
| `search_internal_pool` | 0 | free | $0.00000 |
| `search_github` (query translation) | 1× Haiku | $0.00006 | $0.00006 |
| `score_candidate_rubric` (20 candidates × 2 Haiku) | 40× Haiku | $0.00006 | $0.00240 |
| `enrich_linkedin` (10 candidates, cache miss) | 10× Apify | $0.004 | $0.04000 |
| Bedrock Sonnet (orchestrator session) | 1× | $0.020 | $0.02000 |
| **Total** | | | **~$0.063** |

**If all 10 LinkedIn profiles are cached:** ~$0.023 total.

**Cost discipline rules enforced by agent system prompt:**
1. Internal pool always runs first (free)
2. LinkedIn enrichment only for `fit_score ≥ 40`
3. Dealbreaker pre-filter eliminates candidates before rubric scoring
4. Haiku for lightweight tasks; Sonnet only for orchestration
5. 30-day LinkedIn cache (Apify is the dominant cost driver)

---

## Streaming API

**Endpoint:** `POST /agent`

**Request:**
```json
{
  "message": "Find senior AI engineers in Madrid",
  "history": [],
  "job_posting_id": "abc-123"
}
```

**SSE events emitted:**
```
{"type": "tool_start",  "tool": "search_github",  "text": "Searching GitHub..."}
{"type": "text_delta",  "text": "chunk of model output"}
{"type": "candidates",  "data": [{...CandidateResult...}]}
{"type": "done",        "num_candidates": 8, "num_enriched": 5, "estimated_cost_usd": 0.054}
{"type": "error",       "text": "...", "detail": "..."}
```

---

## MCP tools (for Claude Desktop)

| Tool | Description |
|------|-------------|
| `scout_candidates` | Full pipeline from NL query → ranked shortlist |
| `analyze_candidate` | Single GitHub user → full profile + scores |
| `add_to_pipeline` | Save candidate to recruiter pipeline (shortlisted / contacted / interviewing / hired / rejected) |
| `get_pipeline` | List recruiter's current pipeline, optional stage filter |

**Add to Claude Desktop:**
```json
{
  "mcpServers": {
    "mirai-talent-scout": {
      "type": "url",
      "url": "https://xxxx.execute-api.eu-west-1.amazonaws.com/prod/mcp",
      "headers": { "Authorization": "Bearer YOUR_MCP_AUTH_SECRET" }
    }
  }
}
```

---

## Deployment

- **AWS account:** 335400931736 · **Region:** eu-west-1
- **Runtime:** Python 3.12 · ARM64 (Graviton2)
- **Memory:** 1024 MB · **Timeout:** 900s
- **Provisioned concurrency:** 1 (no cold starts)
- **Secrets:** AWS Secrets Manager (`mirai-talent-scout/{supabase,github,apify,mcp}`)

```bash
make deploy        # SAM deploy
make test          # unit tests only
make test-parity   # Python vs TypeScript score parity (requires gitcheck-webapp:3000)
```

---

## Key design decisions

| Decision | Rationale |
|----------|-----------|
| Internal pool first | CV quality > LinkedIn scrape; zero API cost |
| Dealbreaker pre-filter with Haiku | Cheap binary check eliminates candidates before 2× Haiku rubric scoring |
| `fit_score` = 60% rubric + 40% salary/location | Job match dominates; peripheral factors matter but don't override |
| `mobility_score = None` vs `0` | Explicit: None = no LinkedIn found; 0 = structurally not moving |
| `open_to_work` hard floor at 85 | Self-reported signal overrides structural analysis |
| Career signals not stored to DB | About text for quantified-outcome detection is ephemeral; saves context window |
| Async batch enrichment (semaphore=5, 22s timeout) | Resilience: partial results returned on timeout |
| PgBouncer pooler for Lambda | Prevents connection exhaustion under concurrent invocations |
| Python scores validated against TypeScript ±2 pts | Guards divergence between gitcheck-webapp and this repo |
