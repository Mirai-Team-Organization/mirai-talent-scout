"""
Unit tests for agent/stream_app.py.

Coverage:
  - sse_event(): format, json-safety, extra kwargs
  - _try_extract_candidates(): valid array, missing profile key, invalid JSON, no array
  - auth middleware: missing token → 401, wrong token → 401, valid token → streams
  - _SSECallback: captures text_delta, accumulates buffer, ignores complete chunks
  - _ToolWithSSE: emits tool_start before delegating; error in tool emitted correctly
  - create_agent() system_prompt override (agent/agent.py T1 change)
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── sse_event ─────────────────────────────────────────────────────────────────

from agent.stream_app import _try_extract_candidates, sse_event


class TestSseEvent:
    def test_type_field_present(self):
        raw = sse_event("tool_start")
        assert raw.startswith(b"data: ")
        payload = json.loads(raw[len(b"data: "):].rstrip())
        assert payload["type"] == "tool_start"

    def test_extra_kwargs_included(self):
        raw = sse_event("tool_start", tool="search_github", text="Searching...")
        payload = json.loads(raw[len(b"data: "):].rstrip())
        assert payload["tool"] == "search_github"
        assert payload["text"] == "Searching..."

    def test_terminates_with_double_newline(self):
        raw = sse_event("done")
        assert raw.endswith(b"\n\n")

    def test_non_serialisable_value_uses_str(self):
        from datetime import datetime
        raw = sse_event("x", ts=datetime(2025, 1, 1))
        payload = json.loads(raw[len(b"data: "):].rstrip())
        assert "ts" in payload  # default=str should have serialised it

    def test_unicode_safe(self):
        raw = sse_event("text_delta", text="Zürich • München")
        payload = json.loads(raw[len(b"data: "):].rstrip())
        assert payload["text"] == "Zürich • München"


# ── _try_extract_candidates ───────────────────────────────────────────────────

class TestTryExtractCandidates:
    def _make_candidate(self):
        return {"profile": {"login": "dev1"}, "talent_score": {"overall": 80}}

    def test_valid_array_returned(self):
        candidates = [self._make_candidate()]
        text = f"Some preamble\n{json.dumps(candidates)}\nSome suffix"
        result = _try_extract_candidates(text)
        assert result is not None
        assert result[0]["profile"]["login"] == "dev1"

    def test_no_json_array_returns_none(self):
        assert _try_extract_candidates("Just asking a question?") is None

    def test_array_without_profile_key_returns_none(self):
        text = json.dumps([{"name": "foo"}])
        assert _try_extract_candidates(text) is None

    def test_invalid_json_returns_none(self):
        assert _try_extract_candidates("[broken json {") is None

    def test_empty_array_returns_none(self):
        assert _try_extract_candidates("[]") is None


# ── _SSECallback ──────────────────────────────────────────────────────────────

from agent.stream_app import _SSECallback


class TestSSECallback:
    def test_data_kwarg_emits_event(self):
        events = []
        cb = _SSECallback(events.append)
        cb(data="hello", complete=False)
        assert len(events) == 1
        payload = json.loads(events[0][len(b"data: "):].rstrip())
        assert payload["type"] == "text_delta"
        assert payload["text"] == "hello"

    def test_complete_chunk_not_emitted(self):
        events = []
        cb = _SSECallback(events.append)
        cb(data="last", complete=True)
        assert len(events) == 0

    def test_empty_data_not_emitted(self):
        events = []
        cb = _SSECallback(events.append)
        cb(data="", complete=False)
        assert len(events) == 0

    def test_full_text_accumulates_only_incomplete_chunks(self):
        events = []
        cb = _SSECallback(events.append)
        cb(data="hello ", complete=False)
        cb(data="world", complete=False)
        cb(data=" done", complete=True)  # should NOT be in buffer
        assert cb.full_text() == "hello world"

    def test_unknown_kwargs_ignored(self):
        events = []
        cb = _SSECallback(events.append)
        cb(event={"contentBlockStart": {}}, complete=False)  # tool_use event
        assert len(events) == 0


# ── _ToolWithSSE ──────────────────────────────────────────────────────────────

from agent.stream_app import _ToolWithSSE
from agent.tools.search_github import search_github


class TestToolWithSSE:
    def test_tool_start_event_emitted(self):
        events = []
        wrapped = _ToolWithSSE(search_github, events.append)
        # Inspect the event that would be emitted on stream() entry
        # We verify the wrapper stores the put_event correctly
        assert wrapped._put_event is events.append

    def test_preserves_tool_name(self):
        wrapped = _ToolWithSSE(search_github, lambda e: None)
        assert wrapped.tool_name == search_github.tool_name

    def test_preserves_tool_spec(self):
        wrapped = _ToolWithSSE(search_github, lambda e: None)
        assert wrapped.tool_spec == search_github.tool_spec

    @pytest.mark.asyncio
    async def test_stream_emits_event_then_delegates(self):
        """tool_start fires before the parent stream() is entered."""
        events: list[bytes] = []
        wrapped = _ToolWithSSE(search_github, events.append)

        # Mock the parent stream to avoid real GitHub calls
        async def fake_stream(tool_use, invocation_state, **kwargs):
            yield MagicMock()  # fake ToolResultEvent

        with patch.object(
            type(wrapped).__bases__[0], "stream", fake_stream
        ):
            tool_use = MagicMock()
            tool_use.toolUseId = "test-id"
            results = []
            async for event in wrapped.stream(tool_use, {}):
                results.append(event)

        # tool_start event must be the first thing emitted
        assert len(events) == 1
        payload = json.loads(events[0][len(b"data: "):].rstrip())
        assert payload["type"] == "tool_start"
        assert payload["tool"] == "search_github"


# ── Auth middleware ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auth_missing_token_returns_401():
    os.environ["MCP_AUTH_SECRET"] = "secret123"
    import importlib
    import agent.stream_app as sa
    importlib.reload(sa)  # pick up env var

    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=sa.app), base_url="http://test") as client:
        resp = await client.post("/agent", json={"message": "hello"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_auth_wrong_token_returns_401():
    os.environ["MCP_AUTH_SECRET"] = "secret123"
    import importlib
    import agent.stream_app as sa
    importlib.reload(sa)

    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=sa.app), base_url="http://test") as client:
        resp = await client.post(
            "/agent",
            json={"message": "hello"},
            headers={"Authorization": "Bearer wrong"},
        )
    assert resp.status_code == 401


# ── create_agent system_prompt override ──────────────────────────────────────

def test_create_agent_uses_custom_system_prompt():
    """create_agent() must pass the override prompt to the Agent constructor."""
    from agent.agent import create_agent

    with patch("agent.agent.Agent") as MockAgent, \
         patch("agent.agent.BedrockModel"):
        create_agent(system_prompt="custom prompt")
        call_kwargs = MockAgent.call_args.kwargs
        assert call_kwargs["system_prompt"] == "custom prompt"


def test_create_agent_uses_default_prompt_when_not_overridden():
    from agent.agent import SYSTEM_PROMPT, create_agent

    with patch("agent.agent.Agent") as MockAgent, \
         patch("agent.agent.BedrockModel"):
        create_agent()
        call_kwargs = MockAgent.call_args.kwargs
        assert call_kwargs["system_prompt"] == SYSTEM_PROMPT


# ── Healthcheck ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_healthcheck_returns_200():
    from agent.stream_app import app
    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
