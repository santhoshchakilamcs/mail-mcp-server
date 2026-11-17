"""Deterministic agent client for mail-mcp-server.

The agent drives the MCP server over an in-process transport and completes
eval tasks by calling tools in a fixed sequence per task type. It is
deliberately deterministic (no LLM): the eval measures whether the server's
tools and the agent's use of them produce the right outcomes, with a
regression gate in eval/run_eval.py. The agent policy is swappable for an
LLM-backed one without changing the task format.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mcp import ClientSession
from mcp.client._memory import InMemoryTransport

from src.server import create_server


def _parse(result):
    """Parse tool-result content blocks as JSON.

    The MCP server emits one text block per list item, so a list result
    arrives as several blocks; a single result arrives as one block.
    Returns the single value, or the list of values.
    """
    blocks = [json.loads(c.text) for c in result.content if c.type == "text"]
    return blocks[0] if len(blocks) == 1 else blocks


async def run_task(session: ClientSession, task: dict) -> dict:
    """Complete one eval task via MCP tools. Returns a result dict."""
    ttype = task["type"]
    if ttype == "search_label":
        found = await session.call_tool(
            "search_mail",
            {"query": task["query"], "limit": task.get("limit", 50)},
        )
        messages = _parse(found)
        ids = [m["id"] for m in messages]
        labeled = []
        for mid in ids:
            r = await session.call_tool(
                "label_message", {"message_id": mid, "label": task["label"]}
            )
            if _parse(r).get("applied"):
                labeled.append(mid)
        return {"task_id": task["id"], "labeled_ids": sorted(labeled)}

    if ttype == "triage":
        await session.call_tool("get_message", {"message_id": task["message_id"]})
        cls = await session.call_tool(
            "classify_message", {"message_id": task["message_id"]}
        )
        label = _parse(cls)["label"]
        await session.call_tool(
            "label_message", {"message_id": task["message_id"], "label": label}
        )
        return {"task_id": task["id"], "label": label}

    if ttype == "priority":
        r = await session.call_tool(
            "suggest_priority", {"message_id": task["message_id"]}
        )
        out = _parse(r)
        return {
            "task_id": task["id"],
            "band": out["band"],
            "score": out["score"],
        }

    raise ValueError(f"unknown task type {ttype!r}")


async def run_tasks(db_path: str, tasks: list[dict], backend: str = "sqlite",
                    dsn: str | None = None, redis_url: str | None = None) -> list[dict]:
    """Run a list of tasks against a fresh in-process server. Returns results."""
    server = create_server(db_path, backend=backend, dsn=dsn, redis_url=redis_url)
    transport = InMemoryTransport(server)
    results = []
    try:
        async with transport as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for task in tasks:
                    results.append(await run_task(session, task))
    finally:
        mailbox = getattr(server, "_mailbox", None)
        if mailbox is not None:
            mailbox.close()
    return results


def default_db() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "data", "mailbox.db")
