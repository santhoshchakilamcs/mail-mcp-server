"""Redis search cache for mail-mcp-server.

Caches search_mail results (query + limit -> serialized result list) with a
TTL, and counts hits/misses so the cache's effect is measurable. The cache
is transparent: a miss computes the result exactly as without Redis, and
label changes invalidate nothing because search results carry the message
ids, not the labels (labels are read live by get_message/classify_message).

URL comes from MAIL_MCP_REDIS_URL, e.g. redis://localhost:6379/0.
When Redis is unreachable the cache disables itself (fail-open) instead of
breaking searches; stats report enabled=false in that case.
"""
from __future__ import annotations

import hashlib
import json
import os

TTL_SECONDS = 300


class SearchCache:
    def __init__(self, url: str | None = None, namespace: str = "default"):
        self.url = url or os.environ.get("MAIL_MCP_REDIS_URL", "redis://localhost:6379/0")
        # The namespace (sqlite path or Postgres DSN) keeps cached results
        # from different mailboxes from colliding on the same key.
        self.namespace = hashlib.sha256(namespace.encode()).hexdigest()[:12]
        self.enabled = False
        self.client = None
        try:
            import redis as redis_lib

            self.client = redis_lib.Redis.from_url(
                self.url, socket_connect_timeout=2, decode_responses=True
            )
            self.client.ping()
            self.enabled = True
        except Exception:
            self.enabled = False
            self.client = None

    def _key(self, query: str, limit: int) -> str:
        digest = hashlib.sha256(f"{query}\x00{limit}".encode()).hexdigest()[:16]
        return f"mail_mcp:search:{self.namespace}:{digest}"

    def get(self, query: str, limit: int) -> list[dict] | None:
        if not self.enabled:
            return None
        raw = self.client.get(self._key(query, limit))
        if raw is None:
            self.client.incr("mail_mcp:cache:misses")
            return None
        self.client.incr("mail_mcp:cache:hits")
        return json.loads(raw)

    def set(self, query: str, limit: int, results: list[dict]) -> None:
        if not self.enabled:
            return
        self.client.set(self._key(query, limit), json.dumps(results), ex=TTL_SECONDS)

    def stats(self) -> dict:
        if not self.enabled:
            return {"enabled": False, "hits": 0, "misses": 0}
        hits = int(self.client.get("mail_mcp:cache:hits") or 0)
        misses = int(self.client.get("mail_mcp:cache:misses") or 0)
        total = hits + misses
        return {
            "enabled": True,
            "hits": hits,
            "misses": misses,
            "hit_rate": round(hits / total, 4) if total else 0.0,
            "ttl_seconds": TTL_SECONDS,
        }
