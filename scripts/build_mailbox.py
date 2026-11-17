#!/usr/bin/env python3
"""Build the demo mailbox: download the SpamAssassin public corpus
(easy_ham, 2,551 real messages) if needed, extract it, parse it into
data/mailbox.db. Re-run any time; the build is deterministic.

The corpus tarball and the SQLite DB are local build artifacts (gitignored).
A fresh clone regenerates both with: python3 scripts/build_mailbox.py
"""
from __future__ import annotations

import hashlib
import os
import sys
import tarfile
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.mailbox import Mailbox, corpus_sha256

ROOT = os.path.join(os.path.dirname(__file__), "..")
DATA_DIR = os.path.join(ROOT, "data")
TARBALL = os.path.join(DATA_DIR, "easy_ham.tar.bz2")
CORPUS_DIR = os.path.join(DATA_DIR, "easy_ham")
DB_PATH = os.path.join(DATA_DIR, "mailbox.db")
URL = "https://spamassassin.apache.org/old/publiccorpus/20021010_easy_ham.tar.bz2"

# SHA-256 of the tarball as downloaded on 2026-11-17 (recorded for provenance).
EXPECTED_SHA256 = "f9fc56e1f68780f9afdc6d23a2e24c4584af988afc58bbfabae500e51f3a2f36"


def _download() -> None:
    print(f"downloading {URL} ...")
    urllib.request.urlretrieve(URL, TARBALL)
    print(f"saved {TARBALL} ({os.path.getsize(TARBALL):,} bytes)")


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="sqlite", choices=("sqlite", "postgres"))
    ap.add_argument("--dsn", default=None,
                    help="Postgres DSN (default MAIL_MCP_DSN or postgresql://localhost/mail_mcp)")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(TARBALL):
        _download()
    sha = corpus_sha256(TARBALL)
    print(f"tarball sha256: {sha}")
    if not os.path.isdir(CORPUS_DIR) or len(os.listdir(CORPUS_DIR)) < 2500:
        print("extracting ...")
        with tarfile.open(TARBALL, "r:bz2") as tf:
            tf.extractall(DATA_DIR)

    if args.backend == "postgres":
        _build_postgres(args.dsn)
        return
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    mb = Mailbox(DB_PATH)
    n = mb.load_corpus(CORPUS_DIR)
    print(f"loaded {n} messages into {DB_PATH}")
    print(f"label distribution: {mb.label_counts()}")
    print(f"top senders: {mb.top_senders(3)}")
    mb.close()


def _build_postgres(dsn: str | None) -> None:
    """Load the corpus into Postgres, creating the database if needed."""
    import psycopg
    from urllib.parse import urlparse, urlunparse

    from src.mailbox_pg import PostgresMailbox

    dsn = dsn or os.environ.get("MAIL_MCP_DSN", "postgresql://localhost/mail_mcp")
    dbname = urlparse(dsn).path.lstrip("/") or "mail_mcp"
    try:
        mb = PostgresMailbox(dsn)
    except psycopg.errors.InvalidCatalogName:
        admin_dsn = urlunparse(urlparse(dsn)._replace(path="/postgres"))
        admin = psycopg.connect(admin_dsn, autocommit=True)
        try:
            admin.execute(f'CREATE DATABASE "{dbname}"')
            print(f"created database {dbname}")
        finally:
            admin.close()
        mb = PostgresMailbox(dsn)
    n = mb.load_corpus(CORPUS_DIR)
    print(f"loaded {n} messages into Postgres database {dbname}")
    print(f"label distribution: {mb.label_counts()}")
    print(f"top senders: {mb.top_senders(3)}")
    mb.close()


if __name__ == "__main__":
    main()
