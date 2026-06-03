"""
agent/stream_app.py — FastAPI streaming chat agent for the Mirai UI.

Deployed via Lambda Web Adapter (LWA) on a Lambda Function URL with
InvokeMode: RESPONSE_STREAM. LWA intercepts the Lambda invocation, starts
uvicorn (via run.sh), and forwards HTTP to FastAPI — enabling true SSE
streaming to browser clients.

Request:
    POST /agent
    Authorization: Bearer <MCP_AUTH_SECRET>
    Content-Type: application/json
    {"message": "...", "history": [...], "job_posting_id": "<uuid>|null"}

Response: text/event-stream
    data: {"type": "tool_start", "tool": "search_github", "text": "..."}
    data: {"type": "text_delta", "text": "chunk of model output"}
    data: {"type": "candidates", "data": [...]}
    data: {"type": "error", "text": "...", "detail": "..."}
    data: {"type": "done"}

GET /job-postings
    Returns companies and their job postings for the context selector UI.
    Response: [{"company_name": "...", "postings": [{id, title, location, seniority}]}]
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from typing import Any, AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from strands import Agent
from strands.models import BedrockModel
from strands.tools.decorator import DecoratedFunctionTool

from agent.tools.enrich_linkedin import enrich_linkedin
from agent.tools.rank_shortlist import rank_shortlist
from agent.tools.score_candidate import score_candidate
from agent.tools.search_github import search_github
from agent.tools.build_talent_brief import build_talent_brief
from agent.tools.search_internal_pool import search_internal_pool
from agent.tools.score_candidate_rubric import score_candidate_rubric
from db.client import get_supabase

logger = logging.getLogger(__name__)

app = FastAPI(title="Mirai Agent Stream")

# CORS: only enabled for local dev (DEV_CORS=true). In production, Lambda Function
# URL handles CORS — adding it here too would duplicate the header and browsers reject it.
if os.environ.get("DEV_CORS") == "true":
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Serve the UI at /ui — resolve path relative to this file so it works from any cwd.
_UI_DIR = os.path.join(os.path.dirname(__file__), "..", "ui")
if os.path.isdir(_UI_DIR):
    app.mount("/ui", StaticFiles(directory=_UI_DIR, html=True), name="ui")

_AUTH_SECRET: str = os.environ.get("MCP_AUTH_SECRET", "")

_TOOL_LABELS: dict[str, str] = {
    "build_talent_brief":    "Reading job posting and building search brief...",
    "search_internal_pool":  "Checking Mirai's internal talent pool...",
    "score_candidate_rubric": "Scoring candidates against the hiring rubric...",
    "search_github":         "Searching GitHub for matching developers...",
    "enrich_linkedin":       "Enriching LinkedIn profiles...",
    "score_candidate":       "Scoring and grading candidates...",
    "rank_shortlist":        "Ranking the shortlist...",
}

# ── System prompts ────────────────────────────────────────────────────────────

# Mode B: used when there is no job_posting_id
CONVERSATIONAL_SYSTEM_PROMPT = """You are an AI talent scout for Mirai, a recruiting platform.

Your job: help recruiters find software developers who fit a role AND are likely open to new opportunities.

WORKFLOW:
1. If the recruiter's query is ambiguous (missing location OR missing role/tech), ask ONE short clarifying question. Do not run any tools yet.
2. If the query is clear enough (has a location and a rough role or tech stack), run all four tools in sequence:
   - search_github(query, limit=15)
   - enrich_linkedin(candidates) on all results
   - score_candidate(profile) for each candidate
   - rank_shortlist(candidates, limit=10)
3. Return the ranked candidates as a JSON array — nothing else, just the array.

After showing candidates, the recruiter may ask follow-up questions ("only Python", "remove junior", "try Berlin instead"). Re-run the tools with the refined criteria and return an updated JSON array.

RULES:
- Never invent data. If LinkedIn enrichment is missing, say "mobility data unavailable".
- Keep clarifying questions to one sentence.
- When returning candidates, output ONLY the JSON array. No prose before or after the array.
- When asking a clarifying question, output ONLY the question text. No JSON."""

# Mode A: used when job_posting_id is provided in the request
JOB_POSTING_SYSTEM_PROMPT = """You are Mirai's AI Talent Scout. A specific job posting has been provided — use it to drive the entire search.

WORKFLOW (always follow this order):

1. build_talent_brief(job_posting_id)
   Loads the hiring rubric, skills, salary range, and location.
   Produces a TalentBrief you must pass to all subsequent tools.

2. search_internal_pool(talent_brief, limit=20)
   Checks Mirai's internal database first — zero cost, highest data quality.
   Do NOT call enrich_linkedin on internal candidates.

3. search_github(query=talent_brief["github_query"], limit=20)
   Use the pre-translated GitHub query from the TalentBrief exactly as-is.

4. score_candidate_rubric(profile, talent_brief) — for every candidate (internal + GitHub)
   Candidates with dealbreaker_hit=True are excluded from the shortlist.

5. enrich_linkedin(usernames) — ONLY for GitHub candidates with fit_score >= 40
   Never call for internal candidates.

6. rank_shortlist(candidates) — sorts by fit_score → mobility → grade.

OUTPUT: Return a JSON array of ranked candidates. Nothing else — no prose, no explanation.

Each candidate must be included in the array with all fields returned by the tools.
Internal candidates have source="internal_mirai". GitHub candidates have source="github" or no source field.

COST RULES:
- Internal pool first — it's free.
- Only enrich LinkedIn for fit_score >= 40.
- Dealbreakers auto-exclude — don't spend rubric scoring on disqualified candidates."""


# ── SSE helpers ───────────────────────────────────────────────────────────────

def sse_event(event_type: str, **kwargs: Any) -> bytes:
    payload = {"type": event_type, **kwargs}
    return f"data: {json.dumps(payload, default=str)}\n\n".encode()


def _try_extract_candidates(text: str) -> list | None:
    """Return the first JSON array of candidate objects found in text, or None."""
    match = re.search(r"\[[\s\S]*\]", text)
    if not match:
        return None
    try:
        data = json.loads(match.group())
        if isinstance(data, list) and data and isinstance(data[0], dict):
            # Accept if it has a profile key (GitHub + internal) or fit_score (rubric-scored)
            first = data[0]
            if "profile" in first or "fit_score" in first or "talent_score" in first:
                return data
    except (json.JSONDecodeError, TypeError, KeyError):
        pass
    return None


# ── Strands integration ───────────────────────────────────────────────────────

class _ToolWithSSE(DecoratedFunctionTool):
    """Wraps a @tool-decorated function to emit a tool_start SSE event before execution."""

    def __init__(self, original: DecoratedFunctionTool, put_event: Any) -> None:
        super().__init__(
            original._tool_name,
            original._tool_spec,
            original._tool_func,
            original._metadata,
        )
        self._put_event = put_event

    async def stream(self, tool_use: Any, invocation_state: dict, **kwargs: Any):  # type: ignore[override]
        label = _TOOL_LABELS.get(self._tool_name, f"Running {self._tool_name}...")
        self._put_event(sse_event("tool_start", tool=self._tool_name, text=label))
        async for event in super().stream(tool_use, invocation_state, **kwargs):
            yield event


class _SSECallback:
    """Strands callback handler that emits text_delta SSE events."""

    def __init__(self, put_event: Any) -> None:
        self._put_event = put_event
        self._buffer: list[str] = []

    def __call__(self, **kwargs: Any) -> None:
        data: str = kwargs.get("data", "")
        if data and not kwargs.get("complete"):
            self._put_event(sse_event("text_delta", text=data))
            self._buffer.append(data)

    def full_text(self) -> str:
        return "".join(self._buffer)


# ── Request model ─────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []
    job_posting_id: str | None = None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def healthcheck():
    return {"status": "ok"}


@app.get("/job-postings")
async def list_job_postings(request: Request):
    """Return companies + their active job postings for the context selector UI."""
    if _AUTH_SECRET:
        auth_header = request.headers.get("authorization", "")
        if auth_header != f"Bearer {_AUTH_SECRET}":
            raise HTTPException(status_code=401, detail="Unauthorized")

    sb = get_supabase()
    result = (
        sb.table("company_job_postings")
        .select("id, company_name, title, location, seniority, work_model, status")
        .in_("status", ["active", "paused"])
        .order("company_name")
        .order("title")
        .execute()
    )

    # Group by company_name
    companies: dict[str, list] = {}
    for row in (result.data or []):
        company = row["company_name"] or "Unknown"
        if company not in companies:
            companies[company] = []
        companies[company].append({
            "id":         row["id"],
            "title":      row["title"],
            "location":   row["location"] or "",
            "seniority":  row["seniority"] or "",
            "work_model": row["work_model"] or "",
            "status":     row["status"] or "active",
        })

    payload = [
        {"company_name": name, "postings": postings}
        for name, postings in sorted(companies.items())
    ]
    return JSONResponse(content=payload)


@app.post("/agent")
async def agent_endpoint(request: Request, body: ChatRequest):
    """Streaming chat endpoint.

    When job_posting_id is provided, uses Mode A (job-posting-aware) tools and
    system prompt. Otherwise falls back to Mode B (NL natural language search).
    """
    if _AUTH_SECRET:
        auth_header = request.headers.get("authorization", "")
        if auth_header != f"Bearer {_AUTH_SECRET}":
            raise HTTPException(status_code=401, detail="Unauthorized")

    job_posting_id = body.job_posting_id

    async def generate() -> AsyncGenerator[bytes, None]:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue[bytes | None] = asyncio.Queue()
        cost_info: dict = {}

        def put(event: bytes) -> None:
            loop.call_soon_threadsafe(q.put_nowait, event)

        callback = _SSECallback(put)

        def run() -> None:
            try:
                model = BedrockModel(
                    model_id=os.environ.get(
                        "BEDROCK_MODEL", "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
                    ),
                    region_name=os.environ.get("AWS_REGION", "eu-west-1"),
                )

                messages = [
                    {"role": msg["role"], "content": [{"text": msg["content"]}]}
                    for msg in body.history
                    if msg.get("role") in ("user", "assistant") and msg.get("content")
                ]

                if job_posting_id:
                    # Mode A: job-posting-aware
                    tools = [
                        _ToolWithSSE(t, put)
                        for t in [
                            build_talent_brief,
                            search_internal_pool,
                            score_candidate_rubric,
                            search_github,
                            enrich_linkedin,
                            rank_shortlist,
                        ]
                    ]
                    system_prompt = JOB_POSTING_SYSTEM_PROMPT

                    # Inject job_posting_id into the message so the agent calls build_talent_brief first.
                    # Append user's free-text query as an extra filter instruction if provided.
                    user_query = body.message.strip()
                    if user_query:
                        effective_message = (
                            f"Find candidates for job posting ID: {job_posting_id}\n"
                            f"Additional filter from recruiter: {user_query}"
                        )
                    else:
                        effective_message = f"Find candidates for job posting ID: {job_posting_id}"
                else:
                    # Mode B: NL query
                    tools = [
                        _ToolWithSSE(t, put)
                        for t in [search_github, enrich_linkedin, score_candidate, rank_shortlist]
                    ]
                    system_prompt = CONVERSATIONAL_SYSTEM_PROMPT
                    effective_message = body.message

                agent = Agent(
                    model=model,
                    tools=tools,
                    system_prompt=system_prompt,
                    callback_handler=callback,
                    messages=messages or None,
                )

                agent(effective_message)

                candidates = _try_extract_candidates(callback.full_text())
                if candidates:
                    put(sse_event("candidates", data=candidates))

                    # ── Estimate search cost ───────────────────────────────
                    # Bedrock Sonnet (agent session): ~$0.020 flat
                    # Haiku (rubric scoring, 2 calls/candidate): ~$0.00006/call
                    # LinkedIn Apify enrichment: $0.004/profile
                    num_candidates = len(candidates)
                    num_enriched   = sum(1 for c in candidates if c.get("linkedin"))
                    num_scored     = sum(1 for c in candidates if "rubric_match_score" in c)
                    estimated_cost = round(
                        0.020
                        + num_scored   * 2 * 0.00006
                        + num_enriched * 0.004,
                        4,
                    )
                    cost_info.update({
                        "num_candidates":    num_candidates,
                        "num_enriched":      num_enriched,
                        "estimated_cost_usd": estimated_cost,
                    })

            except Exception as exc:
                import traceback as tb
                logger.exception("Agent run failed")
                put(sse_event("error", text=str(exc), detail=tb.format_exc()))
            finally:
                loop.call_soon_threadsafe(q.put_nowait, None)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        while True:
            event = await q.get()
            if event is None:
                break
            yield event

        yield sse_event("done", **cost_info)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
