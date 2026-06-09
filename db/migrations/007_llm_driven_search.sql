-- Migration 007: parameterize country filter, restore p_min_language_overlap
-- DEPLOYMENT ORDER: apply this migration via Supabase Dashboard > SQL Editor
-- BEFORE deploying the updated Python code.
--
-- Verify after applying:
--   SELECT search_talent_complete_fn(p_languages:=ARRAY['Python'], p_min_language_overlap:=1, p_limit:=5);
--
-- Changes from migration 006:
--   1. Removes hardcoded  ti.country_code IN ('IT', 'CH')
--      → replaced by  (p_country IS NULL OR ti.country_code = p_country)
--      → NULL p_country = global search (no geographic restriction)
--   2. Restores p_min_language_overlap (accidentally dropped in migration 006)

CREATE OR REPLACE FUNCTION search_talent_complete_fn(
  p_languages             TEXT[]  DEFAULT '{}',
  p_role_signal           TEXT    DEFAULT NULL,
  p_country               TEXT    DEFAULT NULL,   -- NULL = no country restriction (global)
  p_city                  TEXT    DEFAULT NULL,
  p_limit                 INT     DEFAULT 2000,
  p_min_language_overlap  INT     DEFAULT 1        -- restored from migration 005
)
RETURNS TABLE (
  github_username     TEXT,
  github_data         JSONB,
  talent_score        JSONB,
  languages           TEXT[],
  skills              TEXT[],
  location_raw        TEXT,
  country_code        TEXT,
  city                TEXT,
  own_repo_max_stars  INT,
  followers           INT,
  activity_score      INT,
  role_signals        TEXT[],
  signals             TEXT[],
  email               TEXT,
  linkedin_url        TEXT,
  source              TEXT,
  indexed_at          TIMESTAMPTZ,
  enrichment_data     JSONB,
  mobility_score      INT,
  data_completeness   FLOAT,
  fetched_at          TIMESTAMPTZ
) AS $$
  SELECT
    ti.github_username, ti.github_data, ti.talent_score, ti.languages, ti.skills,
    ti.location_raw, ti.country_code, ti.city, ti.own_repo_max_stars, ti.followers,
    ti.activity_score, ti.role_signals, ti.signals, ti.email, ti.linkedin_url,
    ti.source, ti.indexed_at,
    le.enrichment_data, le.mobility_score, le.data_completeness, le.fetched_at
  FROM talent_index ti
  INNER JOIN linkedin_enrichments le
    ON le.github_username = ti.github_username
    AND le.expires_at > NOW()
    AND le.enrichment_data IS NOT NULL
  WHERE
    ti.expires_at > NOW()
    -- Country filter: NULL = global (no restriction), otherwise exact match
    AND (p_country IS NULL OR ti.country_code = p_country)
    AND (
      array_length(p_languages, 1) IS NULL
      OR p_languages = '{}'
      OR (
        ti.languages && p_languages
        AND (
          p_min_language_overlap <= 1
          OR (
            SELECT count(1) FROM unnest(ti.languages) l WHERE l = ANY(p_languages)
          ) >= p_min_language_overlap
        )
      )
    )
    AND (p_role_signal IS NULL OR p_role_signal = ANY(ti.role_signals))
  ORDER BY
    CASE
      WHEN p_city IS NOT NULL AND lower(ti.city) = lower(p_city) THEN 0
      WHEN ti.city IN ('Milan', 'Milano', 'Zurich', 'Zuerich') THEN 1
      ELSE 2
    END,
    ti.activity_score DESC,
    ti.own_repo_max_stars DESC
  LIMIT p_limit;
$$ LANGUAGE sql STABLE;

-- Rollback: run migration 006 to restore previous behaviour
-- (country hardcoded to IT/CH, p_min_language_overlap absent)
