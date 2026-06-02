"""
Lambda entry point — exposes TalentScoutAgent as an MCP service over HTTP/SSE.

Deploy: SAM template.yaml → API Gateway + Lambda (provisioned concurrency: 1)
Transport: MCP streamable-HTTP (spec 2025-11-05)

MCP tools exposed:
  scout_candidates   — full pipeline: query → GitHub → LinkedIn → score → ranked shortlist
  analyze_candidate  — single GitHub username → full profile + mobility score
  add_to_pipeline    — save candidate to talent_pipeline table
  get_pipeline       — list recruiter's current pipeline
"""

from __future__ import annotations

import json
import os

from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.types import Tool, TextContent

from agent.agent import create_agent
from agent.tools.search_github import search_github, RateLimitQueuedError
from agent.tools.enrich_linkedin import enrich_linkedin
from agent.tools.score_candidate import score_candidate
from agent.tools.rank_shortlist import rank_shortlist
from scoring.mobility_scorer import detect_move_signals
from db.client import get_supabase

# Module-level agent — persists across warm Lambda invocations
_agent = None


def get_agent():
    global _agent
    if _agent is None:
        _agent = create_agent()
    return _agent


# ── MCP Server ────────────────────────────────────────────────────────────────

server = Server("mirai-talent-scout")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="scout_candidates",
            description=(
                "Find developer candidates matching a recruiter query. "
                "Searches GitHub, enriches with LinkedIn mobility signals, scores and ranks."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language query, e.g. 'senior React engineer in Zurich open to moving'"},
                    "limit": {"type": "integer", "default": 10, "description": "Max candidates to return"},
                    "hiring_context": {"type": "string", "enum": ["startup_early", "startup_growth", "enterprise"], "description": "Adjusts scoring weights for your stage"},
                    "target_location": {"type": "string", "description": "Target office location for location fit scoring"},
                    "job_description": {"type": "string", "description": "Job description for AI fit scoring"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="analyze_candidate",
            description="Fully analyze a single GitHub developer: talent score + LinkedIn mobility.",
            inputSchema={
                "type": "object",
                "properties": {
                    "github_username": {"type": "string"},
                    "hiring_context": {"type": "string", "enum": ["startup_early", "startup_growth", "enterprise"]},
                    "target_location": {"type": "string"},
                },
                "required": ["github_username"],
            },
        ),
        Tool(
            name="add_to_pipeline",
            description="Save a candidate to your talent pipeline with a stage label.",
            inputSchema={
                "type": "object",
                "properties": {
                    "github_username": {"type": "string"},
                    "stage": {"type": "string", "enum": ["shortlisted", "contacted", "interviewing", "hired", "rejected"]},
                    "notes": {"type": "string"},
                    "recruiter_id": {"type": "string", "description": "Supabase user UUID"},
                },
                "required": ["github_username", "stage", "recruiter_id"],
            },
        ),
        Tool(
            name="get_pipeline",
            description="List your current talent pipeline.",
            inputSchema={
                "type": "object",
                "properties": {
                    "recruiter_id": {"type": "string"},
                    "stage": {"type": "string", "description": "Filter by stage"},
                },
                "required": ["recruiter_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "scout_candidates":
        return await _scout_candidates(arguments)
    elif name == "analyze_candidate":
        return await _analyze_candidate(arguments)
    elif name == "add_to_pipeline":
        return await _add_to_pipeline(arguments)
    elif name == "get_pipeline":
        return await _get_pipeline(arguments)
    else:
        raise ValueError(f"Unknown tool: {name}")


async def _scout_candidates(args: dict) -> list[TextContent]:
    query = args["query"]
    limit = args.get("limit", 10)
    hiring_context = args.get("hiring_context")
    target_location = args.get("target_location")
    job_description = args.get("job_description")

    try:
        profiles = search_github(query=query, limit=limit * 2, hiring_context=hiring_context)
        enriched = enrich_linkedin(candidates=profiles)

        scored = []
        for c in enriched:
            ts = score_candidate(
                profile=c,
                hiring_context=hiring_context,
                target_location=target_location,
            )
            c["talent_score"] = ts
            scored.append(c)

        ranked = rank_shortlist(
            candidates=scored,
            job_description=job_description,
            limit=limit,
        )

        return [TextContent(type="text", text=json.dumps(ranked, indent=2, default=str))]

    except RateLimitQueuedError as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e), "retry_after": "5 minutes"}))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def _analyze_candidate(args: dict) -> list[TextContent]:
    username = args["github_username"]
    from agent.tools.search_github import _github_graphql, PROFILE_QUERY, _parse_profile, _get_tokens
    from datetime import datetime, timezone, timedelta

    token = _get_tokens()[0]
    now = datetime.now(timezone.utc)
    result = _github_graphql(
        PROFILE_QUERY,
        {"login": username, "from": (now - timedelta(days=365)).isoformat(), "to": now.isoformat()},
        token,
    )

    if not result.get("data", {}).get("user"):
        return [TextContent(type="text", text=json.dumps({"error": f"GitHub user '{username}' not found"}))]

    profile = _parse_profile(result["data"]["user"])
    enriched = enrich_linkedin(candidates=[profile])
    talent_score = score_candidate(
        profile=enriched[0],
        hiring_context=args.get("hiring_context"),
        target_location=args.get("target_location"),
    )
    enriched[0]["talent_score"] = talent_score

    return [TextContent(type="text", text=json.dumps(enriched[0], indent=2, default=str))]


async def _add_to_pipeline(args: dict) -> list[TextContent]:
    sb = get_supabase()

    # Get candidate UUID
    cand = sb.table("candidates").select("id").eq("github_username", args["github_username"]).maybe_single().execute()
    if not cand.data:
        return [TextContent(type="text", text=json.dumps({"error": "Candidate not in database. Run analyze_candidate first."}))]

    sb.table("talent_pipeline").upsert({
        "recruiter_id": args["recruiter_id"],
        "candidate_id": cand.data["id"],
        "stage": args["stage"],
        "notes": args.get("notes"),
    }, on_conflict="recruiter_id,candidate_id").execute()

    return [TextContent(type="text", text=json.dumps({"success": True, "stage": args["stage"]}))]


async def _get_pipeline(args: dict) -> list[TextContent]:
    sb = get_supabase()
    query = (
        sb.table("talent_pipeline")
        .select("stage, notes, updated_at, candidates(github_username, talent_score)")
        .eq("recruiter_id", args["recruiter_id"])
    )
    if args.get("stage"):
        query = query.eq("stage", args["stage"])

    result = query.order("updated_at", desc=True).execute()
    return [TextContent(type="text", text=json.dumps(result.data, indent=2, default=str))]


# ── Lambda handler ─────────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    """
    API Gateway → Lambda handler for MCP HTTP transport.
    Handles MCP JSON-RPC 2.0 over HTTP POST.
    """
    import asyncio
    from mcp.server.streamable_http import StreamableHTTPServerTransport

    # Auth check
    auth_header = event.get("headers", {}).get("authorization", "")
    expected = f"Bearer {os.environ.get('MCP_AUTH_SECRET', '')}"
    if os.environ.get("MCP_AUTH_SECRET") and auth_header != expected:
        return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized"})}

    body = event.get("body", "{}")
    if isinstance(body, str):
        body = json.loads(body)

    # Run async MCP handler
    async def run():
        transport = StreamableHTTPServerTransport()
        async with server.run(transport):
            return await transport.handle_request(body)

    try:
        result = asyncio.run(run())
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(result),
        }
    except Exception as e:
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)}),
        }
