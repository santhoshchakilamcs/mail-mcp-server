"""Postgres backend + Redis cache tests (live services; skipped when absent).

These tests need a Postgres server and a Redis server. In CI or on a fresh
machine without them they skip instead of failing; ./run.sh setup documents
the local stack (docker compose) that provides both.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = os.path.join(os.path.dirname(__file__), "..")
PG_DSN = os.environ.get("MAIL_MCP_DSN", "postgresql://localhost/mail_mcp")
REDIS_URL = os.environ.get("MAIL_MCP_REDIS_URL", "redis://localhost:6379/0")


def _pg_available() -> bool:
    try:
        import psycopg

        conn = psycopg.connect(PG_DSN, connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


def _redis_available() -> bool:
    try:
        import redis as redis_lib

        client = redis_lib.Redis.from_url(REDIS_URL, socket_connect_timeout=2)
        return bool(client.ping())
    except Exception:
        return False


@pytest.mark.skipif(not _pg_available(), reason="no Postgres at MAIL_MCP_DSN")
def test_postgres_mailbox_parity(tmp_path):
    """Postgres store loads the corpus and matches the SQLite store's counts."""
    import psycopg
    from urllib.parse import urlparse, urlunparse

    from src.mailbox import Mailbox
    from src.mailbox_pg import PostgresMailbox

    scratch = "mail_mcp_test_parity"
    admin_dsn = urlunparse(urlparse(PG_DSN)._replace(path="/postgres"))
    admin = psycopg.connect(admin_dsn, autocommit=True)
    admin.execute(f'DROP DATABASE IF EXISTS "{scratch}"')
    admin.execute(f'CREATE DATABASE "{scratch}"')
    admin.close()
    try:
        dsn = urlunparse(urlparse(PG_DSN)._replace(path=f"/{scratch}"))
        pg = PostgresMailbox(dsn)
        n = pg.load_corpus(os.path.join(ROOT, "data", "easy_ham"))
        assert n == 2551
        lite = Mailbox(os.path.join(ROOT, "data", "mailbox.db"))
        assert pg.count() == lite.count()
        assert pg.label_counts() == lite.label_counts()
        assert pg.priority_counts() == lite.priority_counts()
        for term in ("cron", "mozilla", "debian"):
            pg_ids = {m["id"] for m in pg.search(term, 100)}
            lite_ids = {m["id"] for m in lite.search(term, 100)}
            assert pg_ids == lite_ids, f"search mismatch for {term!r}"
        assert pg.get("1530")["sender"].startswith("MAILER-DAEMON")
        lite.close()
        pg.close()
    finally:
        admin = psycopg.connect(admin_dsn, autocommit=True)
        admin.execute(f'DROP DATABASE IF EXISTS "{scratch}"')
        admin.close()


@pytest.mark.skipif(not _redis_available(), reason="no Redis at MAIL_MCP_REDIS_URL")
def test_redis_cache_hit_miss():
    from src.cache import SearchCache

    cache = SearchCache(REDIS_URL)
    assert cache.enabled
    cache.client.delete("mail_mcp:cache:hits", "mail_mcp:cache:misses")
    cache.client.flushdb()
    # A query no other test uses, so this test cannot pollute shared keys.
    query = "zzz-cache-test-query-123"
    assert cache.get(query, 5) is None  # miss
    payload = [{"id": "0001", "sender": "x", "subject": "s", "date": "d"}]
    cache.set(query, 5, payload)
    assert cache.get(query, 5) == payload  # hit
    stats = cache.stats()
    assert stats["enabled"] is True
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["hit_rate"] == 0.5
    cache.client.flushdb()


@pytest.mark.asyncio
@pytest.mark.skipif(not _redis_available(), reason="no Redis at MAIL_MCP_REDIS_URL")
async def test_server_search_uses_cache(db_copy):
    """Two identical searches through the server: second one is a cache hit."""
    from mcp import ClientSession
    from mcp.client._memory import InMemoryTransport

    from src.server import create_server

    server = create_server(db_copy, redis_url=REDIS_URL)
    transport = InMemoryTransport(server)
    async with transport as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            r1 = await session.call_tool("search_mail", {"query": "debian", "limit": 5})
            r2 = await session.call_tool("search_mail", {"query": "debian", "limit": 5})
            assert [json.loads(c.text) for c in r1.content] == [
                json.loads(c.text) for c in r2.content
            ]
            stats = await session.call_tool("cache_stats", {})
            body = json.loads(stats.content[0].text)
            assert body["enabled"] is True
            assert body["hits"] >= 1
