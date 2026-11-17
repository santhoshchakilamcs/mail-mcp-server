"""Mailbox store for mail-mcp-server.

Parses the Apache SpamAssassin public corpus (easy_ham: 2,551 real messages)
into a SQLite database with a full-text index, and implements the triage
policy the MCP server exposes: labels, priority scoring, extractive
summaries.

The triage policy is documented in README.md. The eval ground truth in
eval/generate_tasks.py is generated from the same documented policy but
written independently, so the eval measures whether this implementation
actually follows the policy.
"""
from __future__ import annotations

import email
import hashlib
import os
import re
import sqlite3
from email import policy
from email.message import EmailMessage

LABELS = ("newsletter", "personal", "automated", "follow-up")

# Priority scoring weights (documented policy; see README.md).
W_PERSONAL = 30
W_LIST = -20
W_FREQUENT_SENDER = 15
W_URGENT_KEYWORD = 25
W_QUESTION = 10
W_RECENT = 10
URGENT_KEYWORDS = ("urgent", "action required", "deadline", "asap",
                   "meeting", "follow up", "follow-up", "reminder")

# A sender counts as frequent when it sent at least this many messages.
FREQUENT_SENDER_MIN = 5

# Messages from 2002; "recent" means dated in the last year of the corpus.
RECENT_YEAR = 2002


def parse_message(path: str) -> dict | None:
    """Parse one corpus file into a message dict. Returns None if unparsable."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    try:
        msg: EmailMessage = email.message_from_bytes(raw, policy=policy.default)
    except Exception:
        return None
    sender = str(msg.get("From", "")).strip()
    subject = str(msg.get("Subject", "")).strip()
    date = str(msg.get("Date", "")).strip()
    body = _extract_body(msg)
    if not sender and not subject and not body:
        return None
    msg_id = os.path.splitext(os.path.basename(path))[0]
    return {
        "id": msg_id,
        "sender": sender,
        "sender_domain": _domain_of(sender),
        "subject": subject,
        "date": date,
        "body": body,  # stored in full: truncating hid content the policy reads
        "is_list": _is_list(msg),
        "list_name": _list_name(msg),
    }


def _extract_body(msg: EmailMessage) -> str:
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    parts.append(str(part.get_content()))
                except Exception:
                    continue
        return "\n".join(parts).strip()
    try:
        content = msg.get_content()
    except Exception:
        return ""
    return str(content).strip() if isinstance(content, str) else ""


def _domain_of(sender: str) -> str:
    m = re.search(r"@([\w.\-]+)", sender)
    return m.group(1).lower() if m else ""


def _is_list(msg: EmailMessage) -> bool:
    if msg.get("List-Id") or msg.get("List-Unsubscribe"):
        return True
    precedence = str(msg.get("Precedence", "")).lower()
    return precedence in ("bulk", "list", "junk")


def _list_name(msg: EmailMessage) -> str:
    list_id = str(msg.get("List-Id", "")).strip()
    if list_id:
        return re.sub(r"[<>]", "", list_id).strip()
    return ""


def compute_label(msg: dict) -> str:
    """Triage label for a message dict (documented policy)."""
    sender = msg.get("sender", "").lower()
    subject = msg.get("subject", "").lower()
    body = msg.get("body", "").lower()
    local = sender.split("@")[0] if "@" in sender else sender
    local = re.sub(r"[<>\"']", "", local).strip()
    if local in ("noreply", "no-reply", "donotreply", "do-not-reply",
                 "daemon", "mailer-daemon", "postmaster") or \
            "mailer-daemon" in sender or "auto-reply" in subject:
        return "automated"
    if msg.get("is_list"):
        return "newsletter"
    if subject.startswith(("re:", "fwd:", "fw:")) and "?" in body:
        return "follow-up"
    return "personal"


def compute_priority(msg: dict, sender_counts: dict[str, int]) -> tuple[int, str, list[str]]:
    """Priority score (0-100), band, and human-readable reasons."""
    reasons: list[str] = []
    score = 0
    label = compute_label(msg)
    if label == "personal":
        score += W_PERSONAL
        reasons.append("personal (not list mail)")
    if msg.get("is_list"):
        score += W_LIST
        reasons.append("mailing-list mail")
    sender = msg.get("sender", "")
    if sender_counts.get(sender, 0) >= FREQUENT_SENDER_MIN:
        score += W_FREQUENT_SENDER
        reasons.append(f"frequent sender ({sender_counts[sender]} messages)")
    subject = msg.get("subject", "").lower()
    body_full = msg.get("body", "")
    body = body_full[:500].lower()
    text = subject + " " + body
    hits = [kw for kw in URGENT_KEYWORDS if kw in text]
    if hits:
        score += W_URGENT_KEYWORD
        reasons.append(f"urgent keywords: {', '.join(sorted(set(hits)))}")
    if "?" in body_full:
        score += W_QUESTION
        reasons.append("asks a question")
    if str(RECENT_YEAR) in msg.get("date", ""):
        score += W_RECENT
        reasons.append(f"dated {RECENT_YEAR}")
    score = max(0, min(100, score))
    band = "high" if score >= 60 else ("medium" if score >= 30 else "low")
    return score, band, reasons


def summarize(body: str, max_sentences: int = 3) -> str:
    """Extractive summary: the first sentences of the body."""
    text = re.sub(r"\s+", " ", body).strip()
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 10]
    return " ".join(sentences[:max_sentences])


class Mailbox:
    """SQLite-backed mailbox with FTS5 search."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                sender TEXT, sender_domain TEXT,
                subject TEXT, date TEXT, body TEXT,
                is_list INTEGER, list_name TEXT,
                label TEXT, priority_score INTEGER, priority_band TEXT
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
                USING fts5(subject, body, content='messages',
                           content_rowid='rowid');
            """
        )
        self.conn.commit()

    def load_corpus(self, corpus_dir: str) -> int:
        """Parse every file in corpus_dir into the DB. Returns message count."""
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
        cur = self.conn.cursor()
        cur.execute("DELETE FROM messages")
        cur.execute("DELETE FROM messages_fts")
        for m in parsed:
            label = compute_label(m)
            score, band, _ = compute_priority(m, sender_counts)
            cur.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (m["id"], m["sender"], m["sender_domain"], m["subject"],
                 m["date"], m["body"], int(m["is_list"]), m["list_name"],
                 label, score, band),
            )
            rowid = cur.lastrowid
            cur.execute(
                "INSERT INTO messages_fts(rowid, subject, body) VALUES (?,?,?)",
                (rowid, m["subject"], m["body"]),
            )
        self.conn.commit()
        return len(parsed)

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def get(self, message_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        return dict(row) if row else None

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Substring search over subject and body (case-insensitive)."""
        like = f"%{query}%"
        rows = self.conn.execute(
            """SELECT id, sender, subject, date
               FROM messages
               WHERE subject LIKE ? COLLATE NOCASE
                  OR body LIKE ? COLLATE NOCASE
               ORDER BY date DESC LIMIT ?""",
            (like, like, limit),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["snippet"] = self._snippet(d["id"], query)
            out.append(d)
        return out

    def _snippet(self, message_id: str, query: str, width: int = 120) -> str:
        row = self.conn.execute(
            "SELECT subject, body FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        if not row:
            return ""
        text = f"{row['subject']} {row['body']}"
        idx = text.lower().find(query.lower())
        if idx < 0:
            return re.sub(r"\s+", " ", text[:width]).strip()
        start = max(0, idx - 40)
        return re.sub(r"\s+", " ", text[start:start + width]).strip()

    def label_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT label, COUNT(*) c FROM messages GROUP BY label"
        ).fetchall()
        return {r["label"]: r["c"] for r in rows}

    def priority_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT priority_band, COUNT(*) c FROM messages GROUP BY priority_band"
        ).fetchall()
        return {r["priority_band"]: r["c"] for r in rows}

    def top_senders(self, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            """SELECT sender, COUNT(*) c FROM messages
               GROUP BY sender ORDER BY c DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [{"sender": r["sender"], "count": r["c"]} for r in rows]

    def set_label(self, message_id: str, label: str) -> bool:
        if label not in LABELS:
            return False
        cur = self.conn.execute(
            "UPDATE messages SET label = ? WHERE id = ?", (label, message_id)
        )
        self.conn.commit()
        return cur.rowcount == 1

    def sender_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT sender, COUNT(*) c FROM messages GROUP BY sender"
        ).fetchall()
        return {r["sender"]: r["c"] for r in rows}

    def close(self) -> None:
        self.conn.close()


def corpus_sha256(tarball_path: str) -> str:
    h = hashlib.sha256()
    with open(tarball_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
