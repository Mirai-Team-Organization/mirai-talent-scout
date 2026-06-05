# Deferred work

Items surfaced during engineering review but not in scope for the initial streaming implementation.

---

## P2 — Rate limiting on `/agent` endpoint

**Risk:** billing exposure. Each request invokes Claude Sonnet + 4 Strands tools; a
runaway client (or missing auth in staging) could rack up significant Bedrock costs.

**What to do:**
- Add per-token (or per-IP fallback) rate limiting in the FastAPI middleware.
- Recommended library: [`slowapi`](https://github.com/laurentS/slowapi) (thin wrapper
  around `limits`; works with FastAPI/Starlette out of the box).
- Suggested limit: 10 req/min per Bearer token, 2 concurrent in-flight per token.
- Store state in Redis (ElastiCache Serverless) or use in-process limits acceptable
  for single-instance provisioned concurrency (ProvisionedConcurrentExecutions: 1).

**Files:** `agent/stream_app.py`, `requirements.txt`, `template.yaml`

---

## P3 — Bump Lambda Web Adapter (LWA) layer version

**Why it matters:** LWA is pinned to `:27` in `template.yaml`. New releases fix bugs,
improve cold-start time, and add ARM64 optimisations.

**What to do:**
- Periodically check: https://github.com/awslabs/aws-lambda-web-adapter/releases
- Update the layer ARN in `template.yaml`:
  ```yaml
  Layers:
    - !Sub arn:aws:lambda:${AWS::Region}:753240598075:layer:LambdaAdapterLayerArm64:<NEW_VERSION>
  ```
- Re-run `make deploy` after bumping.
- Set a calendar reminder quarterly (or subscribe to GitHub release notifications).

**Files:** `template.yaml` line 89

---

## P1 — Recruiter feedback loop (Phase 2 moat signal)

**What:** `match_feedback` Supabase table + `POST /feedback` endpoint in `stream_app.py` + thumbs-up/down UI button per candidate card in `ui/index.html`.

**Why:** Outcome data (hired/rejected/ghosted) is the long-term moat — it closes the loop between search results and scoring quality. Without it, the scout can't learn which signal patterns lead to hires. Identified as Phase 2 priority in the approved design doc.

**What to build:**
- Table: `match_feedback(id, job_posting_id, candidate_github_username, verdict enum('good_fit','not_a_fit','hired'), recruiter_note text, created_at)` — indexed on `(job_posting_id, candidate_github_username)`.
- Endpoint: `POST /feedback` accepting `{job_posting_id, github_username, verdict, note?}`, writes to `match_feedback`.
- UI: per-candidate card: thumbs-up / thumbs-down buttons that call the endpoint.
- Phase 3: feed `match_feedback` outcomes back into `role_scoring_config` weight adjustments (separate feature).

**Depends on:** Phase 1 (current work) shipped and validated with real recruiters.
**Files:** `db/migrations/007_match_feedback.sql` (new), `agent/stream_app.py`, `ui/index.html`

---

## P2 — Fix test_active_developer_scores_high threshold

**What:** `tests/unit/test_score_candidate.py:test_active_developer_scores_high` asserts `result.overall >= 60` but the test profile scores ~50.8 after the weight change that removed `presentation` from the overall score.

**Why:** Failing test creates CI noise and erodes trust in the test suite. The assertion threshold no longer matches the current scoring weights.

**What to do:** Either raise the test profile's `commits`/`active_days` params so it genuinely scores ≥ 60 under current 4-component weights, or lower the assertion threshold to ~50.

**Files:** `tests/unit/test_score_candidate.py` line 60

---

## P3 — Extract role signal string constants to shared module

**What:** Role signal strings (`'ml_engineer_signal'`, `'devops_signal'`, etc.) are string literals duplicated in `indexer/role_signals.py` and `agent/tools/search_talent_index.py:_ROLE_LANGUAGES`. A typo in one won't be caught.

**Why:** DRY violation. Low risk today (5 signals, 2 files) but grows as new signals are added.

**What to do:** Extract to `indexer/role_signal_constants.py` or a module-level `__all__` list; import the constants in both files.

**Files:** `indexer/role_signals.py`, `agent/tools/search_talent_index.py`
