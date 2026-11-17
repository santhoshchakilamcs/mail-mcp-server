"""Postgres-backed mailbox: the same interface as src.mailbox.Mailbox.

The triage policy (labels, priority) lives in src.mailbox and is shared, so
the SQLite and Postgres stores behave identically; only the storage engine
differs. Search uses ILIKE, the Postgres spelling of the SQLite
LIKE ... COLLATE NOCASE used by the SQLite store, so eval ground truth
(which is a LIKE scan) stays valid on both backends.

DSN comes from MAIL_MCP_DSN, e.g.
    postgresql://mail_mcp:mail_mcp@localhost:5432/mail_mcp
"""
from __future__ import annotations

import os

import psycopg
from psycopg.rows import dict_row

from src.mailbox import compute_label, compute_priority, parse_message


class PostgresMailbox:
    """Postgres-backed mailbox with the Mailbox interface."""

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or os.environ.get(
            "MAIL_MCP_DSN", "postgresql://localhost/mail_mcp"
        )
        self.conn = psycopg.connect(self.dsn, row_factory=dict_row, autocommit=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    sender TEXT, sender_domain TEXT,
                    subject TEXT, date TEXT, body TEXT,
                    is_list INTEGER, list_name TEXT,
                    label TEXT, priority_score INTEGER, priority_band TEXT
                )
                """
            )

    def load_corpus(self, corpus_dir: str) -> int:
        files = sorted(
            os.path.join(corpus_dir, f)
            for f in os.listdir(corpus_dir)
            if os.path.isfile(os.path.join(corpus_dir, f))
        )
        parsed = [parse_message(f) for f in files]
        parsed = [m for m in parsed if m]
        sender_counts: dict[str, int] = {}
        for m in parsed:
            sender_counts[m["sender"]] = sender_counts.get(m["sender"], 0) + 1
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM messages")
            for m in parsed:
                label = compute_label(m)
                score, band, _ = compute_priority(m, sender_counts)
                cur.execute(
                    "INSERT INTO messages VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (m["id"], m["sender"], m["sender_domain"], m["subject"],
                     m["date"], m["body"], int(m["is_list"]), m["list_name"],
                     label, score, band),
                )
        return len(parsed)

    def count(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM messages")
            return cur.fetchone()["c"]

    def get(self, message_id: str) -> dict | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM messages WHERE id = %s", (message_id,))
            return cur.fetchone()

    def search(self, query: str, limit: int = 10) -> list[dict]:
        like = f"%{query}%"
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT id, sender, subject, date
                   FROM messages
                   WHERE subject ILIKE %s OR body ILIKE %s
                   ORDER BY date DESC LIMIT %s""",
                (like, like, limit),
            )
            rows = cur.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["snippet"] = self._snippet(d["id"], query)
            out.append(d)
        return out

    def _snippet(self, message_id: str, query: str, width: int = 120) -> str:
        import re

        msg = self.get(message_id)
        if not msg:
            return ""
        text = f"{msg['subject']} {msg['body']}"
        idx = text.lower().find(query.lower())
        if idx < 0:
            return re.sub(r"\s+", " ", text[:width]).strip()
        start = max(0, idx - 40)
        return re.sub(r"\s+", " ", text[start:start + width]).strip()

    def label_counts(self) -> dict[str, int]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT label, COUNT(*) AS c FROM messages GROUP BY label")
            return {r["label"]: r["c"] for r in cur.fetchall()}

    def priority_counts(self) -> dict[str, int]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT priority_band, COUNT(*) AS c FROM messages GROUP BY priority_band"
            )
            return {r["priority_band"]: r["c"] for r in cur.fetchall()}

    def top_senders(self, limit: int = 10) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT sender, COUNT(*) AS c FROM messages
                   GROUP BY sender ORDER BY c DESC LIMIT %s""",
                (limit,),
            )
            return [{"sender": r["sender"], "count": r["c"]} for r in cur.fetchall()]

    def set_label(self, message_id: str, label: str) -> bool:
        from src.mailbox import LABELS

        if label not in LABELS:
            return False
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE messages SET label = %s WHERE id = %s",
                (label, message_id),
            )
            return cur.rowcount == 1

    def sender_counts(self) -> dict[str, int]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT sender, COUNT(*) AS c FROM messages GROUP BY sender")
            return {r["sender"]: r["c"] for r in cur.fetchall()}

    def close(self) -> None:
        self.conn.close()
