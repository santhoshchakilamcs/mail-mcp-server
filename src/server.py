"""MCP server for mail-mcp-server (Model Context Protocol, Python SDK 2.x).

Exposes the demo mailbox over MCP: tools for search, read, classify, label,
priority, summaries, and stats; resources for inbox stats and individual
messages; prompts for triage and follow-ups.

Run over stdio (the standard MCP transport):
    python3 -m src.server
or:
    ./run.sh serve

The server never touches a live mailbox: it reads only the local demo
database built by scripts/build_mailbox.py from the public SpamAssassin
corpus. Labels written by label_message persist in that local database.

Backends (env):
    MAIL_MCP_BACKEND=sqlite (default) | postgres
    MAIL_MCP_DSN=postgresql://... (postgres only; default postgresql://localhost/mail_mcp)
    MAIL_MCP_REDIS_URL=redis://... (optional search cache; unset = no cache)
    MAIL_MCP_DB=<path> (sqlite file; default data/mailbox.db)

Transports: stdio by default; --http serves streamable HTTP on port 8000
(for Docker/Kubernetes).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mcp.server.mcpserver import MCPServer

from src.cache import SearchCache
from src.mailbox import LABELS, Mailbox, compute_label, compute_priority, summarize

DEFAULT_DB = os.path.join(
    os.path.dirname(__file__), "..", "data", "mailbox.db"
)


def _open_mailbox(db_path: str | None, backend: str | None, dsn: str | None):
    backend = (backend or os.environ.get("MAIL_MCP_BACKEND", "sqlite")).lower()
    if backend == "postgres":
        from src.mailbox_pg import PostgresMailbox

        return PostgresMailbox(dsn)
    if backend != "sqlite":
        raise ValueError(f"unknown backend {backend!r} (want sqlite or postgres)")
    return Mailbox(db_path or os.environ.get("MAIL_MCP_DB", DEFAULT_DB))


def create_server(db_path: str | None = None, backend: str | None = None,
                  dsn: str | None = None, redis_url: str | None = None) -> MCPServer:
    """Build the MCP server bound to the mailbox at db_path (sqlite) or dsn
    (postgres). The Redis search cache is used only when MAIL_MCP_REDIS_URL
    (or redis_url) is set."""
    db = _open_mailbox(db_path, backend, dsn)
    redis_env = redis_url or os.environ.get("MAIL_MCP_REDIS_URL")
    namespace = dsn or db_path or os.environ.get("MAIL_MCP_DB", DEFAULT_DB)
    if (backend or os.environ.get("MAIL_MCP_BACKEND", "sqlite")).lower() == "postgres":
        namespace = dsn or os.environ.get("MAIL_MCP_DSN", "")
    cache = SearchCache(redis_env, namespace=namespace) if redis_env else None
    mcp = MCPServer(
        name="mail-mcp-server",
        instructions=(
            "A demo MCP server over a local mailbox of 2,551 real public "
            "emails (SpamAssassin easy_ham corpus). Use search_mail to find "
            "messages, get_message to read one, classify_message for the "
            "server's triage label, label_message to apply a label, "
            "suggest_priority for a scored priority band, and "
            "summarize_message for an extractive summary."
        ),
    )
    # Stash the mailbox so callers (eval, tests) can close its connection.
    mcp._mailbox = db  # type: ignore[attr-defined]

    @mcp.tool()
    async def search_mail(query: str, limit: int = 10) -> list[dict]:
        """Search the mailbox for messages whose subject or body contains query.

        Args:
            query: Substring to search for (case-insensitive).
            limit: Maximum messages to return (default 10).
        """
        limit = max(1, min(limit, 50))
        if cache is not None and cache.enabled:
            hit = cache.get(query, limit)
            if hit is not None:
                return hit
            results = db.search(query, limit)
            cache.set(query, limit, results)
            return results
        return db.search(query, limit)

    @mcp.tool()
    async def get_message(message_id: str) -> dict:
        """Read one message by id: sender, subject, date, body, and stored label."""
        msg = db.get(message_id)
        if msg is None:
            return {"error": f"no message with id {message_id!r}"}
        return msg

    @mcp.tool()
    async def classify_message(message_id: str) -> dict:
        """Return the server's triage label for a message (newsletter, personal,
        automated, or follow-up) with the reason it was chosen."""
        msg = db.get(message_id)
        if msg is None:
            return {"error": f"no message with id {message_id!r}"}
        label = compute_label(msg)
        reasons = []
        sender = msg["sender"].lower()
        if msg["is_list"]:
            reasons.append("mailing-list headers present")
        if "mailer-daemon" in sender or "postmaster" in sender:
            reasons.append("automated sender address")
        if msg["subject"].lower().startswith(("re:", "fwd:", "fw:")):
            reasons.append("reply/forward subject")
        if not reasons:
            reasons.append("single-recipient personal mail")
        return {"message_id": message_id, "label": label, "reasons": reasons}

    @mcp.tool()
    async def label_message(message_id: str, label: str) -> dict:
        """Apply a triage label to a message. Label must be one of:
        newsletter, personal, automated, follow-up."""
        if label not in LABELS:
            return {"error": f"label must be one of {list(LABELS)}"}
        ok = db.set_label(message_id, label)
        if not ok:
            return {"error": f"no message with id {message_id!r}"}
        return {"message_id": message_id, "label": label, "applied": True}

    @mcp.tool()
    async def suggest_priority(message_id: str) -> dict:
        """Score a message's priority (0-100) and return its band
        (high/medium/low) with the reasons behind the score."""
        msg = db.get(message_id)
        if msg is None:
            return {"error": f"no message with id {message_id!r}"}
        score, band, reasons = compute_priority(msg, db.sender_counts())
        return {
            "message_id": message_id,
            "score": score,
            "band": band,
            "reasons": reasons,
        }

    @mcp.tool()
    async def summarize_message(message_id: str) -> dict:
        """Return an extractive summary (first sentences) of a message body."""
        msg = db.get(message_id)
        if msg is None:
            return {"error": f"no message with id {message_id!r}"}
        return {"message_id": message_id, "summary": summarize(msg["body"])}

    @mcp.tool()
    async def inbox_stats() -> dict:
        """Counts by label and priority band, total messages, top senders."""
        return {
            "total_messages": db.count(),
            "by_label": db.label_counts(),
            "by_priority_band": db.priority_counts(),
            "top_senders": db.top_senders(5),
        }

    @mcp.tool()
    async def cache_stats() -> dict:
        """Redis search-cache status: enabled, hits, misses, hit rate, TTL."""
        if cache is None:
            return {"enabled": False, "hits": 0, "misses": 0}
        return cache.stats()

    @mcp.resource("mail://stats")
    async def stats_resource() -> str:
        """Inbox statistics as JSON text."""
        import json

        return json.dumps(await inbox_stats(), indent=2)

    @mcp.resource("mail://message/{message_id}")
    async def message_resource(message_id: str) -> str:
        """One message as JSON text."""
        import json

        msg = db.get(message_id)
        if msg is None:
            return json.dumps({"error": f"no message with id {message_id!r}"})
        return json.dumps(msg, indent=2)

    @mcp.prompt()
    async def triage_inbox() -> str:
        """Guide an agent through triaging the inbox: search, classify,
        label, and prioritize."""
        return (
            "Triage this inbox. For each message: call get_message to read it, "
            "classify_message for the triage label, label_message to apply it, "
            "and suggest_priority for its priority band. Newsletter means "
            "mailing-list or bulk mail; personal means a message written to "
            "one person; automated means a daemon or no-reply sender; "
            "follow-up means a reply that asks a question. Report a short "
            "table of message id, label, and priority band when done."
        )

    @mcp.prompt()
    async def draft_follow_up(message_id: str) -> str:
        """Draft a short follow-up reply to a message."""
        msg = db.get(message_id)
        if msg is None:
            return f"No message with id {message_id!r}."
        return (
            f"Draft a short, polite follow-up reply to this message.\n"
            f"From: {msg['sender']}\nSubject: {msg['subject']}\n\n"
            f"{summarize(msg['body'], 5)}"
        )

    return mcp


def main() -> None:
    import argparse
    import asyncio

    ap = argparse.ArgumentParser()
    ap.add_argument("--http", action="store_true",
                    help="serve streamable HTTP on 0.0.0.0:8000 instead of stdio")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    mcp = create_server()
    if args.http:
        asyncio.run(mcp.run_streamable_http_async(host="0.0.0.0", port=args.port))
    else:
        asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    main()
