"""Mailbox unit tests: parsing, search, labels, priority, summaries."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.mailbox import Mailbox, compute_label, compute_priority, summarize

DB = os.path.join(os.path.dirname(__file__), "..", "data", "mailbox.db")


def test_corpus_count():
    mb = Mailbox(DB)
    assert mb.count() == 2551
    mb.close()


def test_search_matches_independent_scan():
    mb = Mailbox(DB)
    conn = sqlite3.connect(DB)
    for term in ("cron", "mozilla", "debian"):
        got = {m["id"] for m in mb.search(term, 100)}
        like = f"%{term}%"
        rows = conn.execute(
            "SELECT id FROM messages WHERE subject LIKE ? COLLATE NOCASE "
            "OR body LIKE ? COLLATE NOCASE",
            (like, like),
        ).fetchall()
        assert got == {r[0] for r in rows}, f"search mismatch for {term!r}"
    mb.close()
    conn.close()


def test_mailer_daemon_is_automated():
    mb = Mailbox(DB)
    assert compute_label(mb.get("1530")) == "automated"
    assert compute_label(mb.get("1601")) == "automated"
    mb.close()


def test_label_distribution_sane():
    mb = Mailbox(DB)
    counts = mb.label_counts()
    assert counts["newsletter"] > 1000  # corpus is mailing-list heavy
    assert counts["personal"] > 500
    assert set(counts) == {"newsletter", "personal", "automated", "follow-up"}
    mb.close()


def test_priority_score_and_band_consistent():
    mb = Mailbox(DB)
    counts = mb.sender_counts()
    for mid in ("0035", "1530", "1173"):
        msg = mb.get(mid)
        score, band, reasons = compute_priority(msg, counts)
        assert 0 <= score <= 100
        assert band == ("high" if score >= 60 else ("medium" if score >= 30 else "low"))
        assert reasons
    mb.close()


def test_summarize_nonempty():
    import re

    mb = Mailbox(DB)
    msg = mb.get("0035")
    s = summarize(msg["body"])
    assert len(s) > 20
    # summarize() collapses whitespace, so compare against the normalized body
    normalized = re.sub(r"\s+", " ", msg["body"]).strip()
    assert s in normalized
    mb.close()
