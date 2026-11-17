#!/usr/bin/env python3
"""Generate eval/tasks.json with independent ground truth.

Ground truth is computed from the raw corpus files and raw SQL, WITHOUT
importing src.mailbox: the label, priority, and search policies below are
separate implementations of the documented policy in README.md. The eval
therefore measures whether the server's implementation actually follows the
policy, instead of testing the server against itself.

Deterministic: fixed seed, fixed candidate lists. Re-run any time:
    python3 eval/generate_tasks.py
"""
from __future__ import annotations

import email
import json
import os
import random
import re
import sqlite3
import sys
from email import policy
from email.message import EmailMessage

ROOT = os.path.join(os.path.dirname(__file__), "..")
CORPUS_DIR = os.path.join(ROOT, "data", "easy_ham")
DB_PATH = os.path.join(ROOT, "data", "mailbox.db")
OUT_PATH = os.path.join(os.path.dirname(__file__), "tasks.json")

LABELS = ("newsletter", "personal", "automated", "follow-up")
AUTOMATED_LOCALS = ("noreply", "no-reply", "donotreply", "do-not-reply",
                    "daemon", "mailer-daemon", "postmaster")
URGENT_KEYWORDS = ("urgent", "action required", "deadline", "asap",
                   "meeting", "follow up", "follow-up", "reminder")

CANDIDATE_TERMS = [
    "spamassassin", "mozilla", "debian", "release", "meeting", "perl",
    "patch", "digest", "unsubscribe", "kernel", "virus", "filter",
    "mailing", "conference", "upgrade", "backup", "password", "server",
    "network", "download", "registration", "exmh", "razor", "dcc",
    "bayes", "whitelist", "blacklist", "ham", "sa-dev", "cron",
    "procmail", "fetchmail", "imap", "pop3", "smtp", "sendmail",
    "postfix", "apache", "sourceforge", "evolution", "pine", "mutt",
    "header", "moderator", "quarantine", "bogofilter", "slashdot",
    "listman",
]


def parse_raw(path: str) -> dict | None:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        msg: EmailMessage = email.message_from_bytes(raw, policy=policy.default)
    except Exception:
        return None
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    body += str(part.get_content())
                except Exception:
                    pass
    else:
        try:
            c = msg.get_content()
            body = str(c) if isinstance(c, str) else ""
        except Exception:
            body = ""
    return {
        "id": os.path.splitext(os.path.basename(path))[0],
        "sender": str(msg.get("From", "")),
        "subject": str(msg.get("Subject", "")),
        "date": str(msg.get("Date", "")),
        "body": body,
        "list_id": str(msg.get("List-Id", "")),
        "list_unsub": str(msg.get("List-Unsubscribe", "")),
        "precedence": str(msg.get("Precedence", "")).lower(),
    }


def truth_label(m: dict) -> str:
    """Independent triage-label implementation of the documented policy."""
    sender = m["sender"].lower()
    subject = m["subject"].lower()
    local = sender.split("@")[0] if "@" in sender else sender
    local = re.sub(r"[<>\"']", "", local).strip()
    if local in AUTOMATED_LOCALS or "mailer-daemon" in sender \
            or subject.startswith("auto:") or "auto-reply" in subject:
        return "automated"
    if m["list_id"] or m["list_unsub"] or m["precedence"] in ("bulk", "list", "junk"):
        return "newsletter"
    if subject.startswith(("re:", "fwd:", "fw:")) and "?" in m["body"]:
        return "follow-up"
    return "personal"


def truth_priority(m: dict, sender_counts: dict[str, int]) -> tuple[int, str]:
    """Independent priority-score implementation of the documented policy."""
    score = 0
    label = truth_label(m)
    if label == "personal":
        score += 30
    if m["list_id"] or m["list_unsub"] or m["precedence"] in ("bulk", "list", "junk"):
        score -= 20
    if sender_counts.get(m["sender"], 0) >= 5:
        score += 15
    text = (m["subject"] + " " + m["body"][:500]).lower()
    if any(kw in text for kw in URGENT_KEYWORDS):
        score += 25
    if "?" in m["body"]:
        score += 10
    if "2002" in m["date"]:
        score += 10
    score = max(0, min(100, score))
    band = "high" if score >= 60 else ("medium" if score >= 30 else "low")
    return score, band


def main() -> None:
    files = sorted(
        os.path.join(CORPUS_DIR, f)
        for f in os.listdir(CORPUS_DIR)
        if os.path.isfile(os.path.join(CORPUS_DIR, f))
    )
    raw = [parse_raw(f) for f in files]
    raw = [m for m in raw if m]
    by_id = {m["id"]: m for m in raw}
    sender_counts: dict[str, int] = {}
    for m in raw:
        sender_counts[m["sender"]] = sender_counts.get(m["sender"], 0) + 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    def like_ids(term: str) -> list[str]:
        like = f"%{term}%"
        rows = conn.execute(
            """SELECT id FROM messages
               WHERE subject LIKE ? COLLATE NOCASE
                  OR body LIKE ? COLLATE NOCASE""",
            (like, like),
        ).fetchall()
        return sorted(r["id"] for r in rows)

    rng = random.Random(42)
    tasks: list[dict] = []

    # --- 15 search-and-label tasks -------------------------------------
    usable = []
    for term in sorted(CANDIDATE_TERMS):
        ids = like_ids(term)
        if 5 <= len(ids) <= 50:
            usable.append((term, ids))
    for i, (term, ids) in enumerate(usable[:15], 1):
        labels = [truth_label(by_id[mid]) for mid in ids]
        majority = max(set(labels), key=labels.count)
        tasks.append({
            "id": f"search-{i:02d}",
            "type": "search_label",
            "query": term,
            "label": majority,
            "limit": 50,
            "expected_ids": ids,
        })

    # --- 15 triage tasks ------------------------------------------------
    pools: dict[str, list[str]] = {lab: [] for lab in LABELS}
    for m in raw:
        pools[truth_label(m)].append(m["id"])
    want = {"newsletter": 5, "personal": 5, "automated": 3, "follow-up": 2}
    n = 0
    for lab in ("newsletter", "personal", "automated", "follow-up"):
        pool = sorted(pools[lab])
        rng.shuffle(pool)
        for mid in pool[: want[lab]]:
            n += 1
            tasks.append({
                "id": f"triage-{n:02d}",
                "type": "triage",
                "message_id": mid,
                "expected_label": lab,
            })

    # --- 10 priority tasks ----------------------------------------------
    band_pools: dict[str, list[str]] = {"high": [], "medium": [], "low": []}
    for m in raw:
        _, band = truth_priority(m, sender_counts)
        band_pools[band].append(m["id"])
    want_bands = {"high": 4, "medium": 3, "low": 3}
    n = 0
    for band in ("high", "medium", "low"):
        pool = sorted(band_pools[band])
        rng.shuffle(pool)
        for mid in pool[: want_bands[band]]:
            score, _ = truth_priority(by_id[mid], sender_counts)
            n += 1
            tasks.append({
                "id": f"priority-{n:02d}",
                "type": "priority",
                "message_id": mid,
                "expected_band": band,
                "expected_score": score,
            })

    doc = {
        "version": 1,
        "generated_from": "SpamAssassin easy_ham public corpus (2,551 messages)",
        "generated_by": "eval/generate_tasks.py (independent ground truth)",
        "task_count": len(tasks),
        "tasks": tasks,
    }
    with open(OUT_PATH, "w") as fh:
        json.dump(doc, fh, indent=2)
    print(f"wrote {len(tasks)} tasks to {OUT_PATH}")
    print("search terms:", [t["query"] for t in tasks if t["type"] == "search_label"])
    conn.close()


if __name__ == "__main__":
    main()
