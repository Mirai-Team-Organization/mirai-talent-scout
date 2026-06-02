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
    {"message": "...", "history": [{"role": "user"|"assistant", "content": "..."}]}

Response: text/event-stream
    data: {"type": "tool_start", "tool": "search_github", "text": "Searching GitHub..."}
    data: {"type": "text_delta", "text": "chunk of model output"}
    data: {"type": "candidates", "data": [...]}   ← parsed JSON array of candidates
    data: {"type": "error", "text": "...", "detail": "..."}
    data: {"type": "done"}

Architecture:
    FastAPI async handler
        ├── per-request asyncio.Queue (bridges sync thread → async generator)
        ├── threading.Thread runs the Strands agent synchronously
        │     ├── _ToolWithSSE wrappers emit tool_start before each tool runs
        │     └── _SSECallback emits text_delta for model token output
        └── async generator drains queue → StreamingResponse → LWA → browser
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
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from strands import Agent
from strands.models import BedrockModel
from strands.tools.decorator import DecoratedFunctionTool

from agent.tools.enrich_linkedin import enrich_linkedin
from agent.tools.rank_shortlist import rank_shortlist
from agent.tools.score_candidate import score_candidate
from agent.tools.search_github import search_github

logger = logging.getLogger(__name__)

app = FastAPI(title="Mirai Agent Stream")
# CORS is handled by the Lambda Function URL config in template.yaml.
# Do NOT add CORSMiddleware here — it would duplicate the header and browsers reject it.

_AUTH_SECRET: str = os.environ.get("MCP_AUTH_SECRET", "")

_TOOL_LABELS: dict[str, str] = {
    "search_github": "Searching GitHub for matching developers...",
    "enrich_linkedin": "Enriching LinkedIn profiles...",
    "score_candidate": "Scoring and grading candidates...",
    "rank_shortlist": "Ranking the shortlist...",
}

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


# ── SSE helpers ───────────────────────────────────────────────────────────────

def sse_event(event_type: str, **kwargs: Any) -> bytes:
    """Encode a single SSE event line."""
    payload = {"type": event_type, **kwargs}
    return f"data: {json.dumps(payload, default=str)}\n\n".encode()


def _try_extract_candidates(text: str) -> list | None:
    """Return the first JSON array of candidate objects found in text, or None."""
    match = re.search(r"\[[\s\S]*\]", text)
    if not match:
        return None
    try:
        data = json.loads(match.group())
        if isinstance(data, list) and data and isinstance(data[0], dict) and "profile" in data[0]:
            return data
    except (json.JSONDecodeError, TypeError, KeyError):
        pass
    return None


# ── Strands integration ───────────────────────────────────────────────────────

class _ToolWithSSE(DecoratedFunctionTool):
    """Wraps a @tool-decorated function to emit a tool_start SSE event before execution.

    Created per-request (not module-level) so each request gets its own queue
    reference — prevents SSE event cross-contamination between concurrent users.

    Subclasses DecoratedFunctionTool and overrides stream() to emit the event
    first, then delegate to the parent's async generator for actual execution.
    """

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
    """Strands callback handler that emits text_delta SSE events.

    Called by Strands during streaming model output. Emits one event per token
    chunk (data= kwarg). Accumulates the full response for post-run parsing.

    Callback kwargs (from strands/handlers/callback_handler.py):
        data (str): text chunk from the model
        complete (bool): True on the final chunk
        reasoningText (str): optional reasoning/thinking text
        event (dict): raw Bedrock stream event (contains toolUse metadata)
    """

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


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def healthcheck():
    """LWA readiness probe — must return 200 for Lambda to mark the instance warm."""
    return {"status": "ok"}


@app.post("/agent")
async def agent_endpoint(request: Request, body: ChatRequest):
    """Streaming chat endpoint.

    Runs the Strands agent in a daemon thread, bridges SSE events back to the
    async FastAPI response via an asyncio.Queue + loop.call_soon_threadsafe.
    """
    if _AUTH_SECRET:
        auth_header = request.headers.get("authorization", "")
        if auth_header != f"Bearer {_AUTH_SECRET}":
            raise HTTPException(status_code=401, detail="Unauthorized")

    async def generate() -> AsyncGenerator[bytes, None]:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue[bytes | None] = asyncio.Queue()

        def put(event: bytes) -> None:
            """Thread-safe event emitter — bridges sync thread → async loop."""
            loop.call_soon_threadsafe(q.put_nowait, event)

        callback = _SSECallback(put)

        def run() -> None:
            """Agent runner — executes synchronously in a daemon thread."""
            try:
                # Per-request tool wrappers — each gets this request's queue reference.
                tools = [
                    _ToolWithSSE(t, put)
                    for t in [search_github, enrich_linkedin, score_candidate, rank_shortlist]
                ]

                model = BedrockModel(
                    model_id=os.environ.get(
                        "BEDROCK_MODEL", "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
                    ),
                    region_name=os.environ.get("AWS_REGION", "eu-west-1"),
                )

                # Convert simplified text history to Strands message format.
                # Note: text-only history omits prior tool-call/result blocks —
                # acceptable for short conversations where the agent clarifies first.
                messages = [
                    {"role": msg["role"], "content": [{"text": msg["content"]}]}
                    for msg in body.history
                    if msg.get("role") in ("user", "assistant") and msg.get("content")
                ]

                agent = Agent(
                    model=model,
                    tools=tools,
                    system_prompt=CONVERSATIONAL_SYSTEM_PROMPT,
                    callback_handler=callback,
                    messages=messages or None,
                )

                agent(body.message)

                # After agent finishes, check if its response was a candidates array.
                # If so emit a structured event so the UI can render cards.
                candidates = _try_extract_candidates(callback.full_text())
                if candidates:
                    put(sse_event("candidates", data=candidates))

            except Exception as exc:
                import traceback as tb
                logger.exception("Agent run failed")
                put(sse_event("error", text=str(exc), detail=tb.format_exc()))
            finally:
                loop.call_soon_threadsafe(q.put_nowait, None)  # sentinel

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        while True:
            event = await q.get()
            if event is None:
                break
            yield event

        yield sse_event("done")

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering if behind a proxy
        },
    )
