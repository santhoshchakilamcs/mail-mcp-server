"""Smoke test: connect an MCP client to the server in-process and call tools."""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mcp import ClientSession
from mcp.client._memory import InMemoryTransport

from src.server import create_server


async def main() -> None:
    server = create_server(os.path.join(os.path.dirname(__file__), "..", "data", "mailbox.db"))
    transport = InMemoryTransport(server)
    async with transport as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])
            r = await session.call_tool("search_mail", {"query": "spam", "limit": 3})
            print("search:", json.dumps(json.loads(r.content[0].text), indent=2)[:600])
            r = await session.call_tool("classify_message", {"message_id": "1530"})
            print("classify 1530:", r.content[0].text)
            r = await session.call_tool("suggest_priority", {"message_id": "0035"})
            print("priority 0035:", r.content[0].text)
            r = await session.call_tool("inbox_stats", {})
            print("stats:", r.content[0].text[:300])
            res = await session.read_resource("mail://stats")
            print("resource mail://stats ok:", len(res.contents[0].text) > 0)
            pr = await session.get_prompt("triage_inbox", {})
            print("prompt ok:", pr.messages[0].content.text[:80])


asyncio.run(main())
