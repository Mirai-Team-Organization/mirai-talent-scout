/**
 * indexer/find_linkedin.mjs — LinkedIn URL discovery using orangeslice
 *
 * Uses services.person.linkedin.findUrl to find LinkedIn profiles for
 * talent_index entries that don't have one yet.
 *
 * Signals used (in order of richness):
 *   1. email (reverse-lookup — most precise, 50 credits if found)
 *   2. name + company (from GitHub profile)
 *   3. name + city (fallback)
 *
 * Usage:
 *   node indexer/find_linkedin.mjs
 *   node indexer/find_linkedin.mjs --dry-run
 *   node indexer/find_linkedin.mjs --limit 50
 *   node indexer/find_linkedin.mjs --country IT
 */

import { services } from "orangeslice";
import { createClient } from "@supabase/supabase-js";

// ── CLI args ─────────────────────────────────────────────────────────────────
const args = process.argv.slice(2);
const DRY_RUN = args.includes("--dry-run");
const COUNTRY = (() => { const i = args.indexOf("--country"); return i !== -1 ? args[i + 1] : null; })();
const LIMIT   = (() => { const i = args.indexOf("--limit");   return i !== -1 ? parseInt(args[i + 1], 10) : null; })();

// ── Supabase ──────────────────────────────────────────────────────────────────
const sb = createClient(
  process.env.SUPABASE_URL,
  process.env.SUPABASE_SERVICE_ROLE_KEY,
);

// ── ANSI colours ─────────────────────────────────────────────────────────────
const DIM    = "\x1b[2m";
const GREEN  = "\x1b[32m";
const YELLOW = "\x1b[33m";
const CYAN   = "\x1b[36m";
const RESET  = "\x1b[0m";
const BOLD   = "\x1b[1m";

// ── Load targets from Supabase ────────────────────────────────────────────────
async function loadTargets() {
  let q = sb
    .from("talent_index")
    .select("github_username, email, location_raw, city, country_code, github_data")
    .is("linkedin_url", null)
    .gt("expires_at", new Date().toISOString())
    .order("activity_score", { ascending: false });

  if (COUNTRY) q = q.eq("country_code", COUNTRY);
  if (LIMIT)   q = q.limit(LIMIT);

  const { data, error } = await q;
  if (error) throw new Error(`Supabase load error: ${error.message}`);
  return data ?? [];
}

// ── Persist result ────────────────────────────────────────────────────────────
async function updateProfile(githubUsername, linkedinUrl, email) {
  const updates = {};
  if (linkedinUrl) updates.linkedin_url = linkedinUrl;
  if (email)       updates.email = email;
  if (Object.keys(updates).length === 0) return;

  const { error } = await sb
    .from("talent_index")
    .update(updates)
    .eq("github_username", githubUsername);

  if (error) console.error(`  DB update failed for ${githubUsername}: ${error.message}`);
}

// ── Find LinkedIn URL for one profile ─────────────────────────────────────────
async function findLinkedInForProfile(row) {
  const login      = row.github_username;
  const githubData = row.github_data ?? {};
  const name       = githubData.name ?? "";
  const company    = (githubData.company ?? "").trim().replace(/^@/, "");
  const city       = row.city ?? "";
  const email      = row.email ?? "";

  // 1. Email reverse-lookup (most precise)
  if (email) {
    try {
      const url = await services.person.linkedin.findUrl({ name, email, company: company || undefined, location: city || undefined });
      if (url) return { url, method: `email→orangeslice (${email})`, email };
    } catch (e) {
      // fall through
    }
  }

  // 2. Name + company
  if (name && company) {
    try {
      const url = await services.person.linkedin.findUrl({ name, company, location: city || undefined });
      if (url) return { url, method: "name+company→orangeslice", email };
    } catch (e) {
      // fall through
    }
  }

  // 3. Name + city fallback
  if (name && city) {
    try {
      const url = await services.person.linkedin.findUrl({ name, location: city });
      if (url) return { url, method: "name+city→orangeslice", email };
    } catch (e) {
      // fall through
    }
  }

  return { url: null, method: "not_found", email };
}

// ── Main ──────────────────────────────────────────────────────────────────────
async function main() {
  console.log(`\n${BOLD}${"═".repeat(70)}${RESET}`);
  console.log(`  ${BOLD}Mirai — LinkedIn URL Discovery (orangeslice)${RESET}`);
  console.log(`  ${new Date().toUTCString()}`);
  if (DRY_RUN) console.log(`  ${YELLOW}[dry-run] DB will NOT be updated.${RESET}`);
  console.log(`${"═".repeat(70)}\n`);

  console.log("[1/2] Loading profiles without linkedin_url…");
  const targets = await loadTargets();

  if (targets.length === 0) {
    console.log(`${GREEN}✓ All indexed profiles already have a LinkedIn URL.${RESET}\n`);
    return;
  }

  const byCountry = {};
  for (const t of targets) {
    const c = t.country_code ?? "??";
    byCountry[c] = (byCountry[c] ?? 0) + 1;
  }
  const breakdown = Object.entries(byCountry).map(([c, n]) => `${c}: ${n}`).join("  ");
  console.log(`  ${BOLD}${targets.length}${RESET} profiles need LinkedIn discovery  (${breakdown})\n`);

  console.log("[2/2] Discovering LinkedIn URLs…\n");
  const start = Date.now();

  const stats = { found: 0, not_found: 0 };

  // Process concurrently in batches of 5 to avoid hammering the API
  const BATCH = 5;
  for (let i = 0; i < targets.length; i += BATCH) {
    const batch = targets.slice(i, i + BATCH);

    await Promise.all(batch.map(async (row, bi) => {
      const idx  = i + bi + 1;
      const login = row.github_username;
      const loc  = [row.city, row.country_code].filter(Boolean).join(", ");
      const ts   = new Date().toUTCString().slice(17, 25);

      process.stdout.write(
        `  ${DIM}[${ts}]${RESET} (${idx}/${targets.length}) ${BOLD}${login}${RESET} ${DIM}(${loc})${RESET} `
      );

      const { url, method, email } = await findLinkedInForProfile(row);

      if (url) {
        console.log(`${GREEN}✓ ${url}${RESET}  ${DIM}[${method}]${RESET}`);
        stats.found++;
        if (!DRY_RUN) await updateProfile(login, url, email || null);
      } else {
        console.log(`${DIM}— not found${RESET}  ${DIM}[${method}]${RESET}`);
        stats.not_found++;
        // Still save email if we found one during commit-email lookup
        if (!DRY_RUN && email && !row.email) await updateProfile(login, null, email);
      }
    }));
  }

  const elapsed = ((Date.now() - start) / 1000).toFixed(0);
  console.log(`\n${"═".repeat(70)}`);
  console.log(`  Completed in ${elapsed}s`);
  console.log(`  ${GREEN}LinkedIn found: ${stats.found}${RESET}`);
  console.log(`  ${DIM}Not found: ${stats.not_found}${RESET}`);
  if (DRY_RUN) console.log(`  ${YELLOW}[dry-run] No DB changes made.${RESET}`);
  console.log(`${"═".repeat(70)}\n`);
}

main().catch((e) => { console.error(e); process.exit(1); });
