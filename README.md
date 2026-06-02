# Mirai AI Talent Scout

AI-powered talent scout that finds developers on GitHub, enriches profiles with LinkedIn mobility signals via orangeslice, and returns a ranked shortlist with "likelihood to move" scores.

Built on **AWS Strands** (Bedrock) · Deployed as an **MCP service** (Lambda + API Gateway) · Data in **Supabase**

---

## Architecture

```
Recruiter (Claude Desktop / API)
          |
          | MCP HTTP (streamable-HTTP, 2025-11-05 spec)
          v
  API Gateway → Lambda (provisioned concurrency: 1)
          |
  TalentScoutAgent (AWS Strands + Bedrock Sonnet)
    ├── search_github()        GitHub GraphQL + token pool
    ├── enrich_linkedin()      orangeslice → 30d cache
    ├── score_candidate()      talent scorer + hiring context
    └── rank_shortlist()       fit score (Haiku) + mobility + grade
          |
  Supabase (Postgres)
    ├── candidates             GitHub cache (24h TTL)
    ├── linkedin_enrichments   LinkedIn cache (30d TTL)
    ├── search_sessions        search history
    ├── talent_pipeline        recruiter CRM
    └── github_api_usage       rate limit counter
```

---

## Setup

### 1. Clone and install

```bash
git clone git@github.com:mirai-team25/mirai-talent-scout.git
cd mirai-talent-scout
python -m venv .venv && source .venv/bin/activate
make install-dev
```

### 2. Configure environment

```bash
cp .env.example .env
```

Fill in `.env`:
- `GITHUB_TOKENS` — one or more GitHub classic tokens (scopes: `read:user`, `read:org`)
- `SUPABASE_DB_URL` — get the DB password from Supabase Dashboard → Project Settings → Database → Connection string (use the **pooler** URL on port **6543**)
- `ORANGESLICE_API_URL` + `ORANGESLICE_API_KEY` — see `orangeslice-docs/services/index.md`
- `MCP_AUTH_SECRET` — any random secret string for API auth

### 3. Run database migration

Open [Supabase SQL Editor](https://supabase.com/dashboard/project/odnptiirmmcrxelmdikb/sql) and run:

```sql
-- paste contents of db/migrations/001_talent_scout_schema.sql
```

### 4. Run tests

```bash
make test           # unit tests only (no AWS needed)
make test-parity    # requires gitcheck-webapp running on localhost:3000
```

---

## Deploy to AWS

### First time — create secrets

```bash
# Export values from your .env first
export $(cat .env | grep -v '^#' | xargs)
make secrets-init
```

This creates 4 secrets in AWS Secrets Manager (`eu-west-1`):
- `mirai-talent-scout/supabase`
- `mirai-talent-scout/github`
- `mirai-talent-scout/orangeslice`
- `mirai-talent-scout/mcp`

### Deploy

```bash
make deploy
```

SAM deploys the Lambda + API Gateway. The MCP endpoint URL is printed in the outputs:
```
McpEndpoint: https://xxxx.execute-api.eu-west-1.amazonaws.com/prod/mcp
```

---

## Using as MCP service

### Claude Desktop

Add to `~/.claude.json`:

```json
{
  "mcpServers": {
    "mirai-talent-scout": {
      "type": "url",
      "url": "https://xxxx.execute-api.eu-west-1.amazonaws.com/prod/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_MCP_AUTH_SECRET"
      }
    }
  }
}
```

### Available MCP tools

| Tool | Description |
|---|---|
| `scout_candidates` | Full pipeline: NL query → GitHub → LinkedIn → score → ranked shortlist |
| `analyze_candidate` | Single GitHub username → full profile + mobility score |
| `add_to_pipeline` | Save candidate with stage (shortlisted / contacted / hired / ...) |
| `get_pipeline` | List recruiter's current pipeline |

### Example query

```
scout_candidates(
  query="senior React engineer in Zurich open to moving",
  limit=10,
  hiring_context="startup_growth",
  target_location="Zurich",
  job_description="We need a frontend lead for our Series A fintech..."
)
```

---

## Mobility Score

Each candidate gets a 0–100 "likelihood to move" score from 4 structural signals:

| Signal | Weight | What it measures |
|---|---|---|
| Tenure | 35% | Months at current role — peak window is 18–36 months |
| Career velocity | 25% | Title changes per year — stagnation = open signal |
| Job frequency | 25% | Avg months per role — healthy movers score highest |
| Company health | 15% | Layoffs / headcount decline at current employer |

`mobility_score: null` means **no data** (LinkedIn not found), distinct from `0` (definitely not moving).

---

## Project structure

```
agent/          TalentScoutAgent + 4 tools
scoring/        Talent scorer + hiring context + mobility scorer
mcp/            Lambda handler + MCP transport
db/             Supabase client + cache read/write
tests/          Unit, parity (TS vs Python), integration
template.yaml   SAM deployment (Lambda + API Gateway)
Makefile        install / test / deploy shortcuts
```

---

## AWS resources

- **Account:** 335400931736
- **Region:** eu-west-1
- **Bedrock model (orchestrator):** `eu.anthropic.claude-sonnet-4-5-20250929-v1:0`
- **Bedrock model (fit scoring):** `eu.anthropic.claude-haiku-4-5-20251001-v1:0`
- **IAM user:** Teddy_Claude (`arn:aws:iam::335400931736:user/Teddy_Claude`)

## Supabase

- **Project ref:** `odnptiirmmcrxelmdikb`
- **Custom domain:** `api.mirai-now.io`
- **Pooler endpoint:** `aws-0-eu-west-1.pooler.supabase.com:6543` (use this in Lambda)
- **Dashboard:** https://supabase.com/dashboard/project/odnptiirmmcrxelmdikb
