-- Mirai Talent Scout — Migration 002: talent_index
-- Replaces the 24h `candidates` cache with a 30-day persistent talent index.
-- Run via: Supabase Dashboard > SQL Editor

-- ── 1. talent_index ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS talent_index (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  github_username     TEXT UNIQUE NOT NULL,
  github_data         JSONB NOT NULL,
  talent_score        JSONB,

  -- Denormalised for fast filtering (no JSONB extraction at query time)
  languages           TEXT[]  DEFAULT '{}',
  skills              TEXT[]  DEFAULT '{}',
  location_raw        TEXT,
  country_code        TEXT    CHECK (country_code IN ('IT', 'CH')),
  city                TEXT,                         -- 'Milan' | 'Zurich' | ...
  own_repo_max_stars  INT     DEFAULT 0,
  followers           INT     DEFAULT 0,
  activity_score      INT     DEFAULT 0,            -- pre-computed composite

  -- Role signal tags (inferred at index time, no LLM cost)
  role_signals        TEXT[]  DEFAULT '{}',         -- 'ml_engineer_signal' | ...
  signals             TEXT[]  DEFAULT '{}',         -- 'oss_contributor' | 'hackathon_participant' | ...

  -- Contact / outreach fields (populated at index time from GitHub profile)
  email               TEXT,
  linkedin_url        TEXT,

  -- Source tracking
  source              TEXT    NOT NULL DEFAULT 'github_broad',
  source_details      JSONB   DEFAULT '{}',

  -- TTL
  indexed_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at          TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '30 days'
);

CREATE INDEX IF NOT EXISTS idx_ti_country      ON talent_index (country_code);
CREATE INDEX IF NOT EXISTS idx_ti_languages    ON talent_index USING GIN (languages);
CREATE INDEX IF NOT EXISTS idx_ti_skills       ON talent_index USING GIN (skills);
CREATE INDEX IF NOT EXISTS idx_ti_role_signals ON talent_index USING GIN (role_signals);
CREATE INDEX IF NOT EXISTS idx_ti_activity     ON talent_index (activity_score DESC);
CREATE INDEX IF NOT EXISTS idx_ti_stars        ON talent_index (own_repo_max_stars DESC);
CREATE INDEX IF NOT EXISTS idx_ti_expires      ON talent_index (expires_at);
CREATE INDEX IF NOT EXISTS idx_ti_city         ON talent_index (city);
CREATE INDEX IF NOT EXISTS idx_ti_email        ON talent_index (email) WHERE email IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ti_linkedin     ON talent_index (linkedin_url) WHERE linkedin_url IS NOT NULL;

ALTER TABLE talent_index ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_all" ON talent_index
  FOR ALL TO service_role USING (true) WITH CHECK (true);


-- ── 2. indexer_progress — tracks which location×language combos are done ──────
-- Allows multiple Lambda invocations to accumulate without re-fetching work.

CREATE TABLE IF NOT EXISTS indexer_progress (
  location            TEXT NOT NULL,
  language            TEXT NOT NULL,
  pages_fetched       INT  NOT NULL DEFAULT 0,   -- 0–10 (GitHub max 10 pages × 100)
  profiles_upserted   INT  NOT NULL DEFAULT 0,
  completed           BOOL NOT NULL DEFAULT FALSE,
  started_at          TIMESTAMPTZ,
  completed_at        TIMESTAMPTZ,
  PRIMARY KEY (location, language)
);

ALTER TABLE indexer_progress ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_all" ON indexer_progress
  FOR ALL TO service_role USING (true) WITH CHECK (true);


-- ── 3. Migrate existing candidates rows into talent_index ─────────────────────

-- Migrate existing rows. talent_score is re-computed at next index run; NULL is fine for now.
INSERT INTO talent_index (github_username, github_data, source, indexed_at, expires_at)
SELECT
  github_username,
  github_data,
  'github_broad',
  fetched_at,
  expires_at
FROM candidates
ON CONFLICT (github_username) DO NOTHING;


-- ── 4. Update talent_pipeline FK to talent_index ──────────────────────────────
-- Add new column, back-fill from candidates join, then drop old FK.

ALTER TABLE talent_pipeline ADD COLUMN IF NOT EXISTS talent_index_id UUID REFERENCES talent_index(id) ON DELETE CASCADE;

UPDATE talent_pipeline tp
SET talent_index_id = ti.id
FROM candidates c
JOIN talent_index ti ON ti.github_username = c.github_username
WHERE tp.candidate_id = c.id;

-- Once back-filled and verified, run these in a second pass:
-- ALTER TABLE talent_pipeline DROP COLUMN candidate_id;
-- DROP TABLE candidates;


-- ── 5. RPC function for search_talent_index tool ──────────────────────────────

CREATE OR REPLACE FUNCTION search_talent_index_fn(
  p_languages   TEXT[]  DEFAULT '{}',
  p_role_signal TEXT    DEFAULT NULL,
  p_country     TEXT    DEFAULT NULL,
  p_limit       INT     DEFAULT 200
)
RETURNS SETOF talent_index AS $$
  SELECT * FROM talent_index
  WHERE
    -- Geography: default to all IT/CH; narrow to one country if specified
    (p_country IS NULL OR country_code = p_country)
    AND country_code IN ('IT', 'CH')
    AND (array_length(p_languages, 1) = 0 OR p_languages IS NULL OR languages && p_languages)
    AND (p_role_signal IS NULL OR p_role_signal = ANY(role_signals))
    AND expires_at > NOW()
  ORDER BY
    -- Milan and Zurich float to top
    CASE WHEN city IN ('Milan', 'Milano', 'Zurich', 'Zuerich') THEN 0 ELSE 1 END,
    activity_score DESC,
    own_repo_max_stars DESC
  LIMIT p_limit;
$$ LANGUAGE sql STABLE;
