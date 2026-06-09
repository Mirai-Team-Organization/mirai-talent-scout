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
from pathlib import Path
from typing import Any, AsyncGenerator

# ── Load .env for local dev (no-op in Lambda where env vars come from Secrets Manager) ──
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

import botocore.config
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
from agent.tools.build_talent_brief import build_talent_brief
from agent.tools.search_internal_pool import search_internal_pool
from agent.tools.search_talent_index import search_talent_index
from agent.tools.score_candidate_rubric import score_candidate_rubric
from agent.tools.agentic_rerank import agentic_rerank
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
    "search_talent_index":   "Searching the talent index...",
    "score_candidate_rubric": "Scoring candidates against the hiring rubric...",
    "enrich_linkedin":       "Enriching LinkedIn profiles...",
    "score_candidate":       "Scoring and grading candidates...",
    "rank_shortlist":        "Ranking the shortlist...",
    "agentic_rerank":        "Ranking candidates with Sonnet reasoning...",
}

# ── System prompts ────────────────────────────────────────────────────────────

# JOB_POSTING_SYSTEM_PROMPT is kept for reference but no longer used for the initial
# search — the pipeline runs directly. Only used if Strands fallback is ever needed.
JOB_POSTING_SYSTEM_PROMPT = """You are Mirai's AI Talent Scout. Execute the workflow below silently — call tools in order, then output the final JSON. Do NOT narrate steps, do NOT explain what you are about to do. Tools only, then JSON output.

WORKFLOW (always follow this exact order, no commentary between steps):

1. build_talent_brief(job_posting_id)
2. search_internal_pool(talent_brief, limit=20)
3. search_talent_index(talent_brief)
   — returns ONLY complete profiles (GitHub + LinkedIn already attached, no enrichment needed)
4. score_candidate_rubric(all_candidates, talent_brief)  ← one call, internal + index combined
5. rank_shortlist(scored_candidates, talent_brief=talent_brief)  ← always pass talent_brief for comparative ranking

OUTPUT: Output the JSON array returned by rank_shortlist. Nothing else — no prose, no preamble, no explanation. Start your response with [ and end with ].

RULES:
- Do NOT call enrich_linkedin — LinkedIn data is already attached by search_talent_index.
- search_talent_index only returns profiles with both GitHub data AND LinkedIn enrichment.
- Exclude candidates with fit_score < 35 or dealbreaker_hit = true.
- Return top 10 candidates only (rank_shortlist default).
- Rank by fit_score descending.
- Internal pool is free — always run it.
- Never call score_candidate_rubric once per candidate — one call with the full combined list."""

FOLLOWUP_SYSTEM_PROMPT = """You are Mirai's AI Talent Scout. The candidate shortlist from the previous search is in the conversation history as a JSON array.

The current job posting ID is: {job_posting_id}

## DEFAULT: answer questions about the existing shortlist
For any question about the existing candidates — filtering, explaining scores, comparing
profiles, re-ordering — work ONLY with the candidates already in the conversation.
Call rank_shortlist if asked for a different ordering or tighter filter.
Output a JSON array starting with [ and ending with ] when returning candidates.

## ONLY run a new search when the recruiter uses explicit language like:
"new search", "search again", "re-run", "do a new research", "find new candidates",
"look for [seniority] people", "search for junior/mid/senior", "different seniority".

Vague phrases like "what about less senior?", "can we also look at...", "show me more"
do NOT trigger a new search — answer from the existing shortlist instead.

## When a new search IS explicitly requested, call these tools in order:
1. build_talent_brief(job_posting_id="{job_posting_id}")
2. Modify the returned brief dict's "seniority" field if a different level was requested
   Valid values: "junior", "mid", "senior", "lead"
   "junior to mid with potential" → "mid"  |  "more junior" → "junior"
3. search_internal_pool(talent_brief=<brief>, limit=20)
4. search_talent_index(talent_brief=<brief>)
5. score_candidate_rubric(candidates=<internal + index combined>, talent_brief=<brief>)
6. agentic_rerank(candidates=<scored>, talent_brief=<brief>)

Output the final JSON array from agentic_rerank. Nothing else — no prose, no preamble."""


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
    """Wraps a @tool-decorated function to emit tool_start / tool_done SSE events."""

    def __init__(self, original: DecoratedFunctionTool, put_event: Any) -> None:
        orig_func = original._tool_func
        put       = put_event
        tname     = original._tool_name

        def _counted(**kw: Any) -> Any:
            # Inject per-candidate progress callback for the scoring tool
            if tname == "score_candidate_rubric":
                import agent.tools.score_candidate_rubric as _scr
                def _progress_fn(done: int, total: int) -> None:
                    put(sse_event("tool_progress", tool=tname, done=done, total=total))
                _scr._progress.fn = _progress_fn
            try:
                result = orig_func(**kw)
            finally:
                if tname == "score_candidate_rubric":
                    import agent.tools.score_candidate_rubric as _scr
                    _scr._progress.fn = None
            count = len(result) if isinstance(result, list) else None
            extra: dict[str, Any] = {}
            if tname == "rank_shortlist":
                extra["input_count"] = len(kw.get("candidates") or [])
            put(sse_event("tool_done", tool=tname, count=count, **extra))
            return result

        super().__init__(original._tool_name, original._tool_spec, _counted, original._metadata)
        self._put_event = put_event

    async def stream(self, tool_use: Any, invocation_state: dict, **kwargs: Any):  # type: ignore[override]
        label = _TOOL_LABELS.get(self._tool_name, f"Running {self._tool_name}...")
        self._put_event(sse_event("tool_start", tool=self._tool_name, text=label))
        async for event in super().stream(tool_use, invocation_state, **kwargs):
            yield event


def _run_direct_pipeline(
    job_posting_id: str,
    put: Any,
    cost_info: dict,
) -> None:
    """Run the 5-step search pipeline directly without LLM orchestration between steps."""
    import agent.tools.score_candidate_rubric as _scr

    put(sse_event("tool_start", tool="build_talent_brief", text=_TOOL_LABELS["build_talent_brief"]))
    talent_brief = build_talent_brief._tool_func(job_posting_id=job_posting_id)
    put(sse_event("tool_done", tool="build_talent_brief", count=None))

    put(sse_event("tool_start", tool="search_internal_pool", text=_TOOL_LABELS["search_internal_pool"]))
    internal = search_internal_pool._tool_func(talent_brief=talent_brief, limit=20)
    put(sse_event("tool_done", tool="search_internal_pool", count=len(internal)))

    put(sse_event("tool_start", tool="search_talent_index", text=_TOOL_LABELS["search_talent_index"]))
    index_candidates = search_talent_index._tool_func(talent_brief=talent_brief)
    put(sse_event("tool_done", tool="search_talent_index", count=len(index_candidates)))

    # Deduplicate: prefer internal candidates; match on github_username then linkedin_url
    seen_keys: set[str] = set()
    deduped: list[dict] = []
    for c in internal + index_candidates:
        p = c.get("profile") or {}
        key = p.get("login") or c.get("github_username") or c.get("linkedin_url") or id(c)
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(c)
    all_candidates = deduped

    def _progress_fn(done: int, total: int) -> None:
        put(sse_event("tool_progress", tool="score_candidate_rubric", done=done, total=total))

    _scr._progress.fn = _progress_fn
    put(sse_event("tool_start", tool="score_candidate_rubric", text=_TOOL_LABELS["score_candidate_rubric"]))
    try:
        scored = score_candidate_rubric._tool_func(candidates=all_candidates, talent_brief=talent_brief)
    finally:
        _scr._progress.fn = None
    put(sse_event("tool_done", tool="score_candidate_rubric", count=len(scored)))

    put(sse_event("tool_start", tool="agentic_rerank", text=_TOOL_LABELS["agentic_rerank"]))
    ranked = agentic_rerank._tool_func(candidates=scored, talent_brief=talent_brief)
    put(sse_event("tool_done", tool="agentic_rerank", count=len(ranked), input_count=len(scored)))

    put(sse_event("candidates", data=ranked))

    num_scored = len(scored)  # all candidates that reached scoring
    cost_info.update({
        "num_candidates":     len(ranked),
        "estimated_cost_usd": round(0.020 + num_scored * 0.00008, 4),
    })


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


@app.get("/talent-index-browse")
async def browse_talent_index(
    request: Request,
    country: str | None = None,
    city: str | None = None,
    role_signal: str | None = None,
    language: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    """Return talent_index rows with optional tag filters for the Index browser UI."""
    if _AUTH_SECRET:
        auth_header = request.headers.get("authorization", "")
        if auth_header != f"Bearer {_AUTH_SECRET}":
            raise HTTPException(status_code=401, detail="Unauthorized")

    sb = get_supabase()
    # Exclude heavy github_data blob — all useful fields are denormalised columns
    q = (
        sb.table("talent_index")
        .select(
            "github_username,location_raw,country_code,city,"
            "languages,role_signals,signals,activity_score,"
            "followers,own_repo_max_stars,talent_score,"
            "email,linkedin_url,indexed_at,source",
            count="exact",
        )
        .gt("expires_at", "now()")
    )
    if country:
        q = q.eq("country_code", country)
    if city:
        q = q.ilike("city", f"%{city}%")
    if role_signal:
        q = q.contains("role_signals", [role_signal])
    if language:
        q = q.contains("languages", [language])

    q = q.order("activity_score", desc=True).range(offset, offset + limit - 1)
    result = q.execute()

    return JSONResponse(content={
        "profiles": result.data or [],
        "total":    result.count or 0,
        "offset":   offset,
        "limit":    limit,
    })


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
    # Validate UUID format before it reaches any prompt or query
    if job_posting_id:
        import re as _re
        if not _re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", job_posting_id, _re.IGNORECASE):
            raise HTTPException(status_code=400, detail="Invalid job_posting_id format")

    async def generate() -> AsyncGenerator[bytes, None]:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue[bytes | None] = asyncio.Queue()
        cost_info: dict = {}

        def put(event: bytes) -> None:
            loop.call_soon_threadsafe(q.put_nowait, event)

        callback = _SSECallback(put)

        def run() -> None:
            try:
                if not job_posting_id:
                    put(sse_event("error", text="A job posting is required. Please select a job posting to begin searching."))
                    return

                messages = [
                    {"role": msg["role"], "content": [{"text": msg["content"]}]}
                    for msg in body.history
                    if msg.get("role") in ("user", "assistant") and msg.get("content")
                ]

                # Route: pipeline if no history OR empty message (user clicked Search again)
                is_followup = bool(messages) and bool(body.message.strip())
                if not is_followup:
                    # ── Direct pipeline: first search, no LLM overhead between steps ──
                    _run_direct_pipeline(job_posting_id, put, cost_info)
                else:
                    # ── Strands agent: follow-up questions OR new search with adjusted params ──
                    model = BedrockModel(
                        model_id=os.environ.get(
                            "BEDROCK_MODEL", "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
                        ),
                        region_name=os.environ.get("AWS_REGION", "eu-west-1"),
                        boto_client_config=botocore.config.Config(
                            read_timeout=600,
                            connect_timeout=10,
                            retries={"max_attempts": 2},
                        ),
                    )
                    followup_prompt = FOLLOWUP_SYSTEM_PROMPT.format(
                        job_posting_id=job_posting_id
                    )
                    agent = Agent(
                        model=model,
                        tools=[
                            _ToolWithSSE(build_talent_brief, put),
                            _ToolWithSSE(search_internal_pool, put),
                            _ToolWithSSE(search_talent_index, put),
                            _ToolWithSSE(score_candidate_rubric, put),
                            _ToolWithSSE(agentic_rerank, put),
                            _ToolWithSSE(rank_shortlist, put),
                        ],
                        system_prompt=followup_prompt,
                        callback_handler=callback,
                        messages=messages,
                    )
                    agent(body.message.strip())

                    candidates = _try_extract_candidates(callback.full_text())
                    if candidates:
                        put(sse_event("candidates", data=candidates))
                        num_scored = sum(1 for c in candidates if "rubric_match_score" in c)
                        cost_info.update({
                            "num_candidates":     len(candidates),
                            "estimated_cost_usd": round(0.005 + num_scored * 0.00008, 4),
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
