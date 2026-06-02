-- Mirai Talent Scout — database schema
-- Supabase project: odnptiirmmcrxelmdikb (api.mirai-now.io)
-- Run via: Supabase Dashboard > SQL Editor, or supabase db push

-- ── GitHub profile cache (24h TTL) ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS candidates (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  github_username TEXT UNIQUE NOT NULL,
  github_data     JSONB NOT NULL,
  talent_score    JSONB,
  fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at      TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '24 hours'
);

CREATE INDEX IF NOT EXISTS idx_candidates_username ON candidates (github_username);
CREATE INDEX IF NOT EXISTS idx_candidates_expires  ON candidates (expires_at);

ALTER TABLE candidates ENABLE ROW LEVEL SECURITY;

-- Service role has full access; anon has none (server-side only)
CREATE POLICY "service_role_all" ON candidates
  FOR ALL TO service_role USING (true) WITH CHECK (true);


-- ── LinkedIn enrichment cache (30d TTL) ───────────────────────────────────────

CREATE TABLE IF NOT EXISTS linkedin_enrichments (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  github_username    TEXT UNIQUE NOT NULL,
  linkedin_url       TEXT,
  enrichment_data    JSONB,
  mobility_score     INT,           -- 0–100 or NULL (no data)
  data_completeness  FLOAT,         -- 0.0–1.0
  fetched_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at         TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '30 days'
);

CREATE INDEX IF NOT EXISTS idx_linkedin_username ON linkedin_enrichments (github_username);
CREATE INDEX IF NOT EXISTS idx_linkedin_expires  ON linkedin_enrichments (expires_at);

ALTER TABLE linkedin_enrichments ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_all" ON linkedin_enrichments
  FOR ALL TO service_role USING (true) WITH CHECK (true);


-- ── Search session history ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS search_sessions (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  recruiter_id      UUID REFERENCES auth.users(id) ON DELETE SET NULL,
  query             TEXT NOT NULL,
  query_translated  TEXT,
  hiring_context    TEXT,
  result_count      INT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_search_sessions_recruiter ON search_sessions (recruiter_id);
CREATE INDEX IF NOT EXISTS idx_search_sessions_created   ON search_sessions (created_at DESC);

ALTER TABLE search_sessions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "recruiter_own_sessions" ON search_sessions
  FOR ALL TO authenticated USING (recruiter_id = auth.uid()) WITH CHECK (recruiter_id = auth.uid());

CREATE POLICY "service_role_all" ON search_sessions
  FOR ALL TO service_role USING (true) WITH CHECK (true);


-- ── Talent pipeline CRM ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS talent_pipeline (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  recruiter_id  UUID REFERENCES auth.users(id) ON DELETE CASCADE,
  candidate_id  UUID REFERENCES candidates(id) ON DELETE CASCADE,
  stage         TEXT NOT NULL CHECK (stage IN ('shortlisted','contacted','interviewing','hired','rejected')),
  notes         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (recruiter_id, candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_recruiter ON talent_pipeline (recruiter_id);
CREATE INDEX IF NOT EXISTS idx_pipeline_stage     ON talent_pipeline (stage);

ALTER TABLE talent_pipeline ENABLE ROW LEVEL SECURITY;

CREATE POLICY "recruiter_own_pipeline" ON talent_pipeline
  FOR ALL TO authenticated USING (recruiter_id = auth.uid()) WITH CHECK (recruiter_id = auth.uid());

CREATE POLICY "service_role_all" ON talent_pipeline
  FOR ALL TO service_role USING (true) WITH CHECK (true);


-- ── GitHub API rate limit tracking ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS github_api_usage (
  token_id            TEXT PRIMARY KEY,
  requests_this_hour  INT NOT NULL DEFAULT 0,
  reset_at            TIMESTAMPTZ NOT NULL
);

ALTER TABLE github_api_usage ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_all" ON github_api_usage
  FOR ALL TO service_role USING (true) WITH CHECK (true);
