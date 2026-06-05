-- Mirai Talent Scout — Migration 003: talent_index city filter
-- Adds p_city parameter to search_talent_index_fn so the RPC prioritises
-- the exact requested city (e.g. Milan) over other cities in the same country.
-- Default p_limit reduced from 200 to 50 — callers now pass the exact limit
-- instead of over-fetching and filtering client-side.

CREATE OR REPLACE FUNCTION search_talent_index_fn(
  p_languages   TEXT[]  DEFAULT '{}',
  p_role_signal TEXT    DEFAULT NULL,
  p_country     TEXT    DEFAULT NULL,
  p_city        TEXT    DEFAULT NULL,
  p_limit       INT     DEFAULT 50
)
RETURNS SETOF talent_index AS $$
  SELECT * FROM talent_index
  WHERE
    -- Hard country filter: restrict to the requested country (IT or CH).
    -- NULL means "any" — used only for cold-start fallback.
    (p_country IS NULL OR country_code = p_country)
    AND country_code IN ('IT', 'CH')
    AND (array_length(p_languages, 1) = 0 OR p_languages IS NULL OR languages && p_languages)
    AND (p_role_signal IS NULL OR p_role_signal = ANY(role_signals))
    AND expires_at > NOW()
  ORDER BY
    -- City priority: requested city first, then other priority cities, then rest
    CASE
      WHEN p_city IS NOT NULL AND LOWER(city) = LOWER(p_city) THEN 0
      WHEN p_city IS NOT NULL AND city IN ('Milan', 'Milano', 'Zurich', 'Zuerich') THEN 1
      WHEN p_city IS NULL     AND city IN ('Milan', 'Milano', 'Zurich', 'Zuerich') THEN 0
      ELSE 2
    END,
    activity_score DESC,
    own_repo_max_stars DESC
  LIMIT p_limit;
$$ LANGUAGE sql STABLE;
