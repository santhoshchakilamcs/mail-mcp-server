"""MCP server tool tests over the real in-process MCP transport."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mcp import ClientSession
from mcp.client._memory import InMemoryTransport


def _parse(result):
    blocks = [json.loads(c.text) for c in result.content if c.type == "text"]
    return blocks[0] if len(blocks) == 1 else blocks


@pytest.fixture()
def session_cm(server):
    """Yield an async context manager producing a connected ClientSession."""
    transport = InMemoryTransport(server)

    class _CM:
        async def __aenter__(self):
            self._streams = await transport.__aenter__()
            read, write = self._streams
            self.session = ClientSession(read, write)
            await self.session.__aenter__()
            await self.session.initialize()
            return self.session

        async def __aexit__(self, *exc):
            await self.session.__aexit__(*exc)
            await transport.__aexit__(*exc)

    return _CM()


@pytest.mark.asyncio
async def test_lists_eight_tools(session_cm):
    async with session_cm as s:
        tools = await s.list_tools()
        names = sorted(t.name for t in tools.tools)
        assert names == sorted([
            "search_mail", "get_message", "classify_message",
            "label_message", "suggest_priority", "summarize_message",
            "inbox_stats", "cache_stats",
        ])


@pytest.mark.asyncio
async def test_search_and_read(session_cm):
    async with session_cm as s:
        found = _parse(await s.call_tool("search_mail", {"query": "cron", "limit": 5}))
        assert isinstance(found, list) and len(found) > 0
        assert all("cron" in (m["subject"] + m.get("snippet", "")).lower()
                   or "cron" in m["subject"].lower() for m in found)
        msg = _parse(await s.call_tool("get_message", {"message_id": found[0]["id"]}))
        assert msg["id"] == found[0]["id"]
        assert msg["body"]


@pytest.mark.asyncio
async def test_classify_label_priority_roundtrip(session_cm):
    async with session_cm as s:
        cls = _parse(await s.call_tool("classify_message", {"message_id": "1530"}))
        assert cls["label"] == "automated"
        r = _parse(await s.call_tool(
            "label_message", {"message_id": "1530", "label": "automated"}))
        assert r["applied"] is True
        pr = _parse(await s.call_tool("suggest_priority", {"message_id": "0035"}))
        assert pr["band"] in ("high", "medium", "low")
        assert 0 <= pr["score"] <= 100
        sm = _parse(await s.call_tool("summarize_message", {"message_id": "0035"}))
        assert len(sm["summary"]) > 20


@pytest.mark.asyncio
async def test_bad_label_and_unknown_id(session_cm):
    async with session_cm as s:
        r = _parse(await s.call_tool(
            "label_message", {"message_id": "0035", "label": "spam"}))
        assert "error" in r
        r = _parse(await s.call_tool("get_message", {"message_id": "nope-0000"}))
        assert "error" in r


@pytest.mark.asyncio
async def test_stats_resource_and_prompt(session_cm):
    async with session_cm as s:
        stats = _parse(await s.call_tool("inbox_stats", {}))
        assert stats["total_messages"] == 2551
        res = await s.read_resource("mail://stats")
        assert json.loads(res.contents[0].text)["total_messages"] == 2551
        pr = await s.get_prompt("triage_inbox", {})
        assert "Triage" in pr.messages[0].content.text
