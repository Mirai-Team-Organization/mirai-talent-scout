/**
 * indexer/linkedin_search.mjs — LinkedIn alumni talent discovery
 *
 * Strategy:
 *   Primary key  : university — one actor call per school
 *   Secondary key: role — checked post-fetch against the last 3 experience titles
 *
 * Actor: harvestapi/linkedin-profile-search (Full mode)
 *   - `schools` accepts plain-text university names, up to 50
 *   - Full mode returns experience[] + education[] in a single call
 *   - No separate enrichment step needed for the fit gate
 *
 * Fit gate (5 hard filters, in order):
 *   1. Not an executive  — no CEO/CTO/Founder/MD in current role
 *   2. Not junior        — no Intern/Junior/Trainee in current role
 *   3. Current role is technical — last active position maps to a role signal
 *   4. Engineering continuity   — ≥ 2 of last 3 experiences are technical roles
 *   5. University recency       — graduated ≥ 2021 OR still in progress
 *
 * Geography: Italy-based alumni first, then Switzerland-based (one call per entry).
 *   Each top European school gets two entries — one per target country — so alumni
 *   who relocated to IT/CH from Germany, UK, France, etc. are also captured.
 *
 * Usage:
 *   node indexer/linkedin_search.mjs
 *   node indexer/linkedin_search.mjs --dry-run
 *   node indexer/linkedin_search.mjs --country IT
 *   node indexer/linkedin_search.mjs --country CH
 *   node indexer/linkedin_search.mjs --take-pages 5      # LinkedIn pages per call (25 profiles/page, default 10)
 *   node indexer/linkedin_search.mjs --max-items 250     # cap per call (default 250)
 *   node indexer/linkedin_search.mjs --concurrency 2     # parallel calls (default 1)
 *   node indexer/linkedin_search.mjs --limit 2           # first N universities only
 *   node indexer/linkedin_search.mjs --school "ETH Zurich"  # single university by name
 *
 * Cost: ~$0.10/page (25 profiles) + $0.004/profile in Full mode.
 *   10 pages × $0.10 = $1.00 search + up to 250 × $0.004 = $1.00 enrich = ~$2.00/university call.
 *
 * Environment:
 *   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
 *   APIFY_API_TOKEN (picked up by orangeslice)
 */

import { createClient } from "@supabase/supabase-js";

// ── CLI args ──────────────────────────────────────────────────────────────────
const args       = process.argv.slice(2);
const DRY_RUN    = args.includes("--dry-run");
const COUNTRY    = (() => { const i = args.indexOf("--country");     return i !== -1 ? args[i + 1] : null; })();
const TAKE_PAGES = (() => { const i = args.indexOf("--take-pages");  return i !== -1 ? parseInt(args[i + 1], 10) : 10; })();
const MAX_ITEMS  = (() => { const i = args.indexOf("--max-items");   return i !== -1 ? parseInt(args[i + 1], 10) : 250; })();
const CONCURRENCY= (() => { const i = args.indexOf("--concurrency"); return i !== -1 ? parseInt(args[i + 1], 10) : 1; })();
const LIMIT      = (() => { const i = args.indexOf("--limit");       return i !== -1 ? parseInt(args[i + 1], 10) : null; })();
const SCHOOL     = (() => { const i = args.indexOf("--school");      return i !== -1 ? args[i + 1] : null; })();
const DEBUG      = args.includes("--debug");  // print raw keys of first profile

// ── ANSI colours ──────────────────────────────────────────────────────────────
const DIM    = "\x1b[2m";
const GREEN  = "\x1b[32m";
const YELLOW = "\x1b[33m";
const RED    = "\x1b[31m";
const RESET  = "\x1b[0m";
const BOLD   = "\x1b[1m";

// ── Supabase ──────────────────────────────────────────────────────────────────
const sb = createClient(
  process.env.SUPABASE_URL,
  process.env.SUPABASE_SERVICE_ROLE_KEY,
);

// ── Direct Apify caller (no orangeslice) ─────────────────────────────────────
const APIFY_TOKEN  = process.env.APIFY_API_TOKEN;
const APIFY_BASE   = "https://api.apify.com/v2";
const POLL_INTERVAL_MS = 5_000;

async function runApifyActor(actorId, input) {
  if (!APIFY_TOKEN) throw new Error("APIFY_API_TOKEN env var not set");

  const slug = actorId.replace("/", "~");  // harvestapi/linkedin-profile-search → harvestapi~linkedin-profile-search

  // 1. Start the run
  const startRes = await fetch(
    `${APIFY_BASE}/acts/${slug}/runs?token=${APIFY_TOKEN}`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) }
  );
  if (!startRes.ok) {
    const text = await startRes.text();
    throw new Error(`Apify start failed (${startRes.status}): ${text.slice(0, 200)}`);
  }
  const { data: runData } = await startRes.json();
  const runId     = runData.id;
  const datasetId = runData.defaultDatasetId;

  // 2. Poll until SUCCEEDED / FAILED / ABORTED
  while (true) {
    await new Promise(r => setTimeout(r, POLL_INTERVAL_MS));

    const statusRes = await fetch(`${APIFY_BASE}/actor-runs/${runId}?token=${APIFY_TOKEN}`);
    if (!statusRes.ok) continue;
    const { data: run } = await statusRes.json();

    if (run.status === "SUCCEEDED") break;
    if (["FAILED", "ABORTED", "TIMED-OUT"].includes(run.status)) {
      throw new Error(`Apify run ${runId} ended with status: ${run.status}`);
    }
    // Still RUNNING / READY — keep polling
  }

  // 3. Fetch dataset items
  const itemsRes = await fetch(
    `${APIFY_BASE}/datasets/${datasetId}/items?token=${APIFY_TOKEN}&clean=true&format=json`
  );
  if (!itemsRes.ok) throw new Error(`Apify dataset fetch failed (${itemsRes.status})`);
  const items = await itemsRes.json();

  return { items: Array.isArray(items) ? items : [] };
}

// ── All engineering job titles (secondary API filter) ─────────────────────────
// Passed as currentJobTitles so LinkedIn pre-filters alumni by current role.
// People without an explicit title are still caught by the fit gate continuity check.
const ENGINEERING_TITLES = [
  // Backend / Software
  "Software Engineer", "Software Developer", "Backend Engineer", "Backend Developer",
  "API Engineer", "Go Engineer", "Rust Engineer", "Distributed Systems Engineer",
  // ML / AI / Data
  "Machine Learning Engineer", "ML Engineer", "AI Engineer", "Research Engineer",
  "NLP Engineer", "Data Scientist", "Applied Scientist", "MLOps Engineer",
  "ML Platform Engineer", "Deep Learning Engineer",
  // DevOps / Platform / Infra
  "DevOps Engineer", "Site Reliability Engineer", "SRE", "Infrastructure Engineer",
  "Cloud Engineer", "Platform Engineer", "DevSecOps Engineer", "Systems Engineer",
  // Full-stack
  "Full Stack Engineer", "Full Stack Developer", "Fullstack Engineer",
  // FDE / Solutions
  "Solutions Engineer", "Forward Deployed Engineer", "Field Engineer",
  "Implementation Engineer", "Technical Account Manager", "Sales Engineer",
  "Customer Engineer",
];

// ── University list ────────────────────────────────────────────────────────────
// Plain-text names as LinkedIn recognises them. One actor call per entry.
// `location` = where the person currently lives (IT or CH), not where the school is.
// European-school alumni who relocated to Italy/Switzerland are the primary target.
const UNIVERSITIES = [
  // ── Italy-based alumni ───────────────────────────────────────────────────
  // Domestic schools
  { school: "Politecnico di Milano",               countryCode: "IT", location: "Italy" },
  { school: "Politecnico di Torino",               countryCode: "IT", location: "Italy" },
  { school: "Università Bocconi",                  countryCode: "IT", location: "Italy" },
  { school: "Sapienza University of Rome",         countryCode: "IT", location: "Italy" },
  { school: "University of Bologna",               countryCode: "IT", location: "Italy" },
  { school: "University of Padua",                 countryCode: "IT", location: "Italy" },
  { school: "Scuola Normale Superiore",            countryCode: "IT", location: "Italy" },
  { school: "University of Trento",               countryCode: "IT", location: "Italy" },
  { school: "University of Pisa",                  countryCode: "IT", location: "Italy" },
  // European alumni now in Italy
  { school: "ETH Zurich",                          countryCode: "IT", location: "Italy" },
  { school: "EPFL",                                countryCode: "IT", location: "Italy" },
  { school: "Technical University of Munich",      countryCode: "IT", location: "Italy" },
  { school: "RWTH Aachen University",              countryCode: "IT", location: "Italy" },
  { school: "Karlsruhe Institute of Technology",   countryCode: "IT", location: "Italy" },
  { school: "TU Berlin",                           countryCode: "IT", location: "Italy" },
  { school: "University of Oxford",                countryCode: "IT", location: "Italy" },
  { school: "University of Cambridge",             countryCode: "IT", location: "Italy" },
  { school: "Imperial College London",             countryCode: "IT", location: "Italy" },
  { school: "University College London",           countryCode: "IT", location: "Italy" },
  { school: "University of Edinburgh",             countryCode: "IT", location: "Italy" },
  { school: "École Polytechnique",                 countryCode: "IT", location: "Italy" },
  { school: "École Normale Supérieure",            countryCode: "IT", location: "Italy" },
  { school: "CentraleSupélec",                     countryCode: "IT", location: "Italy" },
  { school: "Delft University of Technology",      countryCode: "IT", location: "Italy" },
  { school: "KTH Royal Institute of Technology",   countryCode: "IT", location: "Italy" },
  { school: "Chalmers University of Technology",   countryCode: "IT", location: "Italy" },
  { school: "Technical University of Denmark",     countryCode: "IT", location: "Italy" },
  { school: "TU Wien",                             countryCode: "IT", location: "Italy" },
  { school: "KU Leuven",                           countryCode: "IT", location: "Italy" },
  { school: "Aalto University",                    countryCode: "IT", location: "Italy" },
  { school: "Instituto Superior Técnico",          countryCode: "IT", location: "Italy" },
  { school: "Czech Technical University in Prague",countryCode: "IT", location: "Italy" },

  // ── Switzerland-based alumni ─────────────────────────────────────────────
  // Domestic schools
  { school: "ETH Zurich",                          countryCode: "CH", location: "Switzerland" },
  { school: "EPFL",                                countryCode: "CH", location: "Switzerland" },
  { school: "University of Zurich",                countryCode: "CH", location: "Switzerland" },
  { school: "University of Basel",                 countryCode: "CH", location: "Switzerland" },
  { school: "University of Bern",                  countryCode: "CH", location: "Switzerland" },
  { school: "University of Geneva",                countryCode: "CH", location: "Switzerland" },
  { school: "University of Lausanne",              countryCode: "CH", location: "Switzerland" },
  // European alumni now in Switzerland
  { school: "Politecnico di Milano",               countryCode: "CH", location: "Switzerland" },
  { school: "Politecnico di Torino",               countryCode: "CH", location: "Switzerland" },
  { school: "Technical University of Munich",      countryCode: "CH", location: "Switzerland" },
  { school: "RWTH Aachen University",              countryCode: "CH", location: "Switzerland" },
  { school: "Karlsruhe Institute of Technology",   countryCode: "CH", location: "Switzerland" },
  { school: "TU Berlin",                           countryCode: "CH", location: "Switzerland" },
  { school: "University of Oxford",                countryCode: "CH", location: "Switzerland" },
  { school: "University of Cambridge",             countryCode: "CH", location: "Switzerland" },
  { school: "Imperial College London",             countryCode: "CH", location: "Switzerland" },
  { school: "University College London",           countryCode: "CH", location: "Switzerland" },
  { school: "University of Edinburgh",             countryCode: "CH", location: "Switzerland" },
  { school: "École Polytechnique",                 countryCode: "CH", location: "Switzerland" },
  { school: "École Normale Supérieure",            countryCode: "CH", location: "Switzerland" },
  { school: "CentraleSupélec",                     countryCode: "CH", location: "Switzerland" },
  { school: "Delft University of Technology",      countryCode: "CH", location: "Switzerland" },
  { school: "KTH Royal Institute of Technology",   countryCode: "CH", location: "Switzerland" },
  { school: "Chalmers University of Technology",   countryCode: "CH", location: "Switzerland" },
  { school: "Technical University of Denmark",     countryCode: "CH", location: "Switzerland" },
  { school: "KU Leuven",                           countryCode: "CH", location: "Switzerland" },
  { school: "Aalto University",                    countryCode: "CH", location: "Switzerland" },
  { school: "Instituto Superior Técnico",          countryCode: "CH", location: "Switzerland" },
  { school: "Czech Technical University in Prague",countryCode: "CH", location: "Switzerland" },
];

// ── Graduation recency — no one who finished > 5 years ago ───────────────────
const CURRENT_YEAR   = new Date().getFullYear();   // 2026
const MIN_GRAD_YEAR  = CURRENT_YEAR - 5;           // 2021

// ── Seniority exclusion patterns ──────────────────────────────────────────────
const EXECUTIVE_RE = /\b(ceo|cto|cfo|coo|cpo|ciso|chief\s+\w+\s+officer|founder|co-?founder|managing\s+director|general\s+manager|executive\s+director|vice\s+president)\b/i;
const JUNIOR_RE    = /\b(junior|jr\.?|intern(ship)?|trainee|apprentice|graduate\s+engineer|entry.?level|student\s+engineer)\b/i;

// ── Role signal inference from a title string ─────────────────────────────────
function inferRoleSignals(title = "") {
  const t = title.toLowerCase();
  const signals = [];
  if (/machine.?learning|ml.?eng|ai.?eng|data.?scien|research.?eng|nlp|deep.?learn|mlops|applied.?scient/.test(t))
    signals.push("ml_engineer_signal");
  if (/devops|site.?reliab|sre|platform.?eng|infra|cloud.?eng|devsecops|systems.?eng/.test(t))
    signals.push("devops_signal");
  if (/full.?stack|fullstack/.test(t))
    signals.push("fullstack_signal");
  if (/backend|back.?end|software.?eng|software.?dev|api.?eng|distributed|golang|rust.?eng/.test(t))
    signals.push("backend_signal");
  if (/solutions?.?eng|forward.?deploy|field.?eng|implementation.?eng|technical.?account|sales.?eng|customer.?eng/.test(t))
    signals.push("fde_signal");
  return signals;
}

// ── Extract current role from experience[] ────────────────────────────────────
// "Current" = endDate is null, missing, or text === "Present".
function currentRole(experiences = []) {
  return experiences.find(exp => {
    const end = exp.endDate ?? exp.end;
    return !end || end.text === "Present" || end.year == null;
  });
}

// ── Graduation recency check ──────────────────────────────────────────────────
// Pass if any education entry ended >= MIN_GRAD_YEAR or is still in progress.
function hasRecentGraduation(profile) {
  const edus = profile.education ?? profile.educations ?? [];
  if (edus.length === 0) return false;  // no education data — can't verify
  return edus.some(edu => {
    const end = edu.endDate ?? edu.end;
    if (!end || end.year == null) return true;           // in progress / no end date
    if (end.year > CURRENT_YEAR) return true;            // future expected graduation
    return end.year >= MIN_GRAD_YEAR;
  });
}

// ── Fit gate ──────────────────────────────────────────────────────────────────
//
//   1. Not an executive        — CEO/CTO/Founder/MD disqualify
//   2. Not junior/intern       — seniority floor
//   3. University recency      — graduated 2021–2026 or in progress (fast-fail old grads)
//   4. Current role technical  — last active position maps to a role signal
//   5. Engineering continuity  — ≥ 2 of last 3 experiences are technical
//
// Returns { pass: true, roleSignals } or { pass: false, reason }
function assessFit(profile) {
  const experiences  = profile.experience ?? profile.experiences ?? profile.positions ?? [];
  const current      = currentRole(experiences);
  const currentTitle = current?.position ?? current?.title ?? current?.jobTitle
    ?? profile.jobTitle ?? profile.currentTitle ?? profile.headline ?? "";

  // 1. No executives
  if (EXECUTIVE_RE.test(currentTitle)) {
    return { pass: false, reason: "executive" };
  }

  // 2. No juniors
  if (JUNIOR_RE.test(currentTitle)) {
    return { pass: false, reason: "junior_title" };
  }

  // 3. University recency — fast-fail old graduates before doing role checks
  if (!hasRecentGraduation(profile)) {
    return { pass: false, reason: "graduation_too_old" };
  }

  // 4. Current role must be technical
  const currentSignals = inferRoleSignals(currentTitle);
  if (currentSignals.length === 0) {
    return { pass: false, reason: "current_role_not_technical" };
  }

  // 5. Engineering continuity — ≥ 2 of last 3 experiences are technical
  const lastThree = experiences.slice(0, 3);
  const engCount  = lastThree.filter(exp => {
    const t = exp.position ?? exp.title ?? exp.jobTitle ?? "";
    return inferRoleSignals(t).length > 0;
  }).length;
  if (engCount < 2) {
    return { pass: false, reason: `low_eng_continuity(${engCount}/${lastThree.length})` };
  }

  return { pass: true, roleSignals: currentSignals };
}

// ── Location helpers ──────────────────────────────────────────────────────────
function canonicalCity(locationText = "") {
  const l = locationText.toLowerCase();
  if (l.includes("milan") || l.includes("milano")) return "Milan";
  if (l.includes("rome") || l.includes("roma"))    return "Rome";
  if (l.includes("turin") || l.includes("torino")) return "Turin";
  if (l.includes("zurich") || l.includes("zürich")) return "Zurich";
  if (l.includes("geneva") || l.includes("genève")) return "Geneva";
  if (l.includes("basel"))                          return "Basel";
  if (l.includes("bern"))                           return "Bern";
  return null;
}

// ── Upsert a profile into talent_index ────────────────────────────────────────
async function upsertProfile(profile, schoolMeta, roleSignals) {
  const linkedinUrl    = profile.linkedinUrl ?? profile.linkedInUrl ?? profile.url ?? null;
  const locationText   = profile.location?.linkedinText ?? profile.location?.parsed?.city
    ?? profile.locationText ?? profile.location ?? "";
  // Prefer parsed countryCode from actor; fall back to search config
  const countryCode    = profile.location?.countryCode?.toUpperCase()
    ?? profile.location?.parsed?.country?.toUpperCase()
    ?? schoolMeta.countryCode;
  const city           = canonicalCity(typeof locationText === "string" ? locationText : "");

  const linkedinHandle = linkedinUrl
    ? linkedinUrl.replace(/\/$/, "").split("/in/").pop()?.replace(/\/$/, "")
    : null;
  if (!linkedinHandle) return { status: "skip", reason: "no_handle" };

  const experiences = profile.experience ?? profile.experiences ?? profile.positions ?? [];
  const educations  = profile.education  ?? profile.educations  ?? [];
  const currentTitle = currentRole(experiences)?.position
    ?? profile.jobTitle ?? profile.currentTitle ?? "";

  const row = {
    github_username:    `li:${linkedinHandle}`,
    github_data:        {},
    linkedin_url:       linkedinUrl,
    languages:          [],
    skills:             (profile.skills ?? []).map(s => s.name ?? s).filter(Boolean).slice(0, 20),
    location_raw:       typeof locationText === "string" ? locationText : JSON.stringify(locationText),
    country_code:       countryCode,
    city,
    own_repo_max_stars: 0,
    followers:          profile.followerCount ?? 0,
    activity_score:     10,   // elite-university gate already passed — fixed bonus
    role_signals:       roleSignals,
    signals:            profile.openToWork ? ["open_to_work"] : [],
    email:              profile.email ?? null,
    source:             "linkedin_search",
    source_details:     {
      apify_actor:      "harvestapi/linkedin-profile-search",
      school:           schoolMeta.school,
      current_title:    currentTitle,
      headline:         profile.headline ?? "",
      experience_count: experiences.length,
      educations: educations.slice(0, 3).map(e => ({
        school:  e.schoolName,
        degree:  e.degree,
        end_year: e.endDate?.year ?? null,
      })),
    },
    expires_at: new Date(Date.now() + 30 * 24 * 60 * 60 * 1000).toISOString(),
  };

  const { error } = await sb
    .from("talent_index")
    .upsert(row, { onConflict: "github_username" });

  if (error) throw new Error(`DB upsert failed for li:${linkedinHandle}: ${error.message}`);
  return { status: "ok" };
}

// ── Run one actor call (one university) ───────────────────────────────────────
async function runUniversityCall(schoolMeta, idx, total) {
  const { school, countryCode, location } = schoolMeta;

  const ts = new Date().toUTCString().slice(17, 25);
  process.stdout.write(
    `  ${DIM}[${ts}]${RESET} (${idx}/${total}) ${BOLD}${school}${RESET} ${DIM}@ ${countryCode}${RESET} `
  );

  if (DRY_RUN) {
    console.log(`${YELLOW}[dry-run]${RESET}`);
    return { school, found: 0, upserted: 0, skipped: 0, cost: 0 };
  }

  try {
    const { items } = await runApifyActor("harvestapi/linkedin-profile-search", {
      profileScraperMode: "Full",
      schools:          [school],
      locations:        [location],
      currentJobTitles: ENGINEERING_TITLES,
      takePages:        TAKE_PAGES,
      maxItems:         MAX_ITEMS,
    });
    const usageTotalUsd = 0;  // cost not returned by direct API; track in Apify dashboard

    // Debug: inspect raw shape of first profile to verify field names
    if (DEBUG && items.length > 0) {
      const p = items[0];
      console.log(`\n${YELLOW}[debug] First profile top-level keys:${RESET}`, Object.keys(p));
      console.log(`${YELLOW}[debug] experience field:${RESET}`, JSON.stringify(
        (p.experience ?? p.experiences ?? p.positions ?? p.jobs ?? []).slice(0, 2), null, 2
      ));
      console.log(`${YELLOW}[debug] education field:${RESET}`, JSON.stringify(
        (p.education ?? p.educations ?? []).slice(0, 2), null, 2
      ));
      console.log(`${YELLOW}[debug] location:${RESET}`, JSON.stringify(p.location));
      console.log(`${YELLOW}[debug] jobTitle / currentTitle / headline:${RESET}`,
        p.jobTitle, "|", p.currentTitle, "|", p.headline);
      console.log();
    }

    let upserted = 0, skipped = 0, failed = 0;
    const skipReasons = {};

    for (const profile of items) {
      const fit = assessFit(profile);
      if (!fit.pass) {
        skipped++;
        skipReasons[fit.reason] = (skipReasons[fit.reason] ?? 0) + 1;
        continue;
      }

      const name       = profile.fullName ?? `${profile.firstName ?? ""} ${profile.lastName ?? ""}`.trim();
      const profileUrl = profile.linkedinUrl ?? profile.linkedInUrl ?? profile.url ?? "";

      try {
        const { status, reason } = await upsertProfile(profile, schoolMeta, fit.roleSignals);
        if (status === "ok") {
          upserted++;
          console.log(`\n    ${GREEN}+${RESET} ${BOLD}${name}${RESET}  ${DIM}${profileUrl}${RESET}`);
        } else {
          skipped++;
          skipReasons[reason] = (skipReasons[reason] ?? 0) + 1;
        }
      } catch (e) {
        failed++;
      }
    }

    const reasonStr = Object.entries(skipReasons).map(([r, n]) => `${r}:${n}`).join(" ");
    const prefix    = upserted > 0 ? "\n  " : "";
    console.log(
      `${prefix}${GREEN}✓${RESET} ${DIM}found=${items.length} db=${upserted} skip=${skipped}` +
      `${reasonStr ? ` (${reasonStr})` : ""} $${usageTotalUsd.toFixed(3)}${RESET}`
    );

    return { school, found: items.length, upserted, skipped, failed, cost: usageTotalUsd };
  } catch (e) {
    console.log(`${RED}✗ ${e.message.slice(0, 70)}${RESET}`);
    return { school, found: 0, upserted: 0, skipped: 0, failed: 0, cost: 0, error: e.message };
  }
}

// ── Main ──────────────────────────────────────────────────────────────────────
async function main() {
  console.log(`\n${BOLD}${"═".repeat(70)}${RESET}`);
  console.log(`  ${BOLD}Mirai — LinkedIn Alumni Talent Search${RESET}`);
  console.log(`  ${new Date().toUTCString()}`);
  console.log(`  Actor: harvestapi/linkedin-profile-search (Full mode)`);
  if (DRY_RUN) console.log(`  ${YELLOW}[dry-run] No actor calls will be made.${RESET}`);
  if (COUNTRY)  console.log(`  Country filter: ${COUNTRY}`);
  console.log(`  Graduation window: ${MIN_GRAD_YEAR}–${CURRENT_YEAR} (or in progress)`);
  console.log(`  Pages per call: ${TAKE_PAGES}  Max items: ${MAX_ITEMS}  Concurrency: ${CONCURRENCY}`);
  console.log(`${"═".repeat(70)}\n`);

  let calls = COUNTRY
    ? UNIVERSITIES.filter(u => u.countryCode === COUNTRY)
    : UNIVERSITIES;

  if (SCHOOL) calls = calls.filter(u => u.school.toLowerCase().includes(SCHOOL.toLowerCase()));
  if (LIMIT)  calls = calls.slice(0, LIMIT);

  console.log(`[1/2] Universities to search (${calls.length}):\n`);
  for (const u of calls) {
    console.log(`  ${DIM}→${RESET} ${u.school} ${DIM}(${u.countryCode})${RESET}`);
  }
  console.log();

  console.log(`[2/2] Searching…\n`);

  const results = [];
  for (let i = 0; i < calls.length; i += CONCURRENCY) {
    const batch = calls.slice(i, i + CONCURRENCY);
    const batchResults = await Promise.all(
      batch.map((u, bi) => runUniversityCall(u, i + bi + 1, calls.length))
    );
    results.push(...batchResults);
  }

  const totalFound    = results.reduce((s, r) => s + (r.found    ?? 0), 0);
  const totalUpserted = results.reduce((s, r) => s + (r.upserted ?? 0), 0);
  const totalSkipped  = results.reduce((s, r) => s + (r.skipped  ?? 0), 0);
  const totalCost     = results.reduce((s, r) => s + (r.cost     ?? 0), 0);
  const errors        = results.filter(r => r.error);

  console.log(`\n${"═".repeat(70)}`);
  console.log(`  ${BOLD}Summary${RESET}`);
  console.log(`  Universities searched: ${calls.length}  (${calls.length - errors.length} OK, ${errors.length} failed)`);
  console.log(`  Profiles found:    ${BOLD}${totalFound}${RESET}`);
  console.log(`  Stored in DB:      ${GREEN}${totalUpserted}${RESET}`);
  console.log(`  Filtered out:      ${DIM}${totalSkipped}${RESET}`);
  console.log(`  Total Apify cost:  $${totalCost.toFixed(3)}`);
  if (DRY_RUN) console.log(`  ${YELLOW}[dry-run] No DB changes or Apify calls made.${RESET}`);
  console.log(`${"═".repeat(70)}\n`);
}

main().catch((e) => { console.error(e); process.exit(1); });
