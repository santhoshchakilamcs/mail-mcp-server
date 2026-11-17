"""HTTP-mode test: the server over streamable HTTP, exercised by a real MCP client."""
import json
import os
import socket
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = os.path.join(os.path.dirname(__file__), "..")
PORT = 8123


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.asyncio
async def test_http_mode_serves_tools():
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    port = _free_port()
    env = dict(os.environ)
    proc = subprocess.Popen(
        [os.path.join(ROOT, ".venv", "bin", "python"), "-m", "src.server",
         "--http", "--port", str(port)],
        cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        # Wait for the port to accept connections.
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            pytest.fail("HTTP server did not start")

        url = f"http://127.0.0.1:{port}/mcp"
        async with streamable_http_client(url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = sorted(t.name for t in tools.tools)
                assert "search_mail" in names
                assert "cache_stats" in names
                r = await session.call_tool(
                    "search_mail", {"query": "cron", "limit": 2})
                found = [json.loads(c.text) for c in r.content if c.type == "text"]
                assert len(found) == 2
                assert all("cron" in (m["subject"] + m.get("snippet", "")).lower()
                           for m in found)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
