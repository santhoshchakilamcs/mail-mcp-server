#!/usr/bin/env python3
"""Run the eval: drive the deterministic agent over eval/tasks.json through
the real MCP server, score each task against ground truth, write
eval/results.json and eval/metrics.json, and enforce the regression gate
against eval/baseline.json.

The eval runs against a throwaway COPY of the mailbox so labeling tasks
never mutates the committed database, and repeated runs are identical.

Usage: python3 eval/run_eval.py [--tasks eval/tasks.json]
Exit code 0 when the run passes the gate, 1 when it regresses.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.agent import run_tasks

ROOT = os.path.join(os.path.dirname(__file__), "..")
DEFAULT_TASKS = os.path.join(ROOT, "eval", "tasks.json")
DEFAULT_DB = os.path.join(ROOT, "data", "mailbox.db")
RESULTS_PATH = os.path.join(ROOT, "eval", "results.json")
METRICS_PATH = os.path.join(ROOT, "eval", "metrics.json")
BASELINE_PATH = os.path.join(ROOT, "eval", "baseline.json")


def score(task: dict, result: dict) -> dict:
    """Score one task result against ground truth. Returns a scored record."""
    ttype = task["type"]
    rec = {"task_id": task["id"], "type": ttype, "correct": False}
    if ttype == "search_label":
        expected = set(task["expected_ids"])
        got = set(result["labeled_ids"])
        tp = len(expected & got)
        precision = tp / len(got) if got else 0.0
        recall = tp / len(expected) if expected else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        rec.update({
            "expected_ids": sorted(expected),
            "labeled_ids": sorted(got),
            "expected_count": len(expected),
            "labeled_count": len(got),
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "correct": got == expected,
        })
    elif ttype == "triage":
        rec.update({
            "expected_label": task["expected_label"],
            "got_label": result["label"],
            "correct": result["label"] == task["expected_label"],
        })
    elif ttype == "priority":
        rec.update({
            "expected_band": task["expected_band"],
            "got_band": result["band"],
            "expected_score": task["expected_score"],
            "got_score": result["score"],
            "score_abs_error": abs(result["score"] - task["expected_score"]),
            "correct": result["band"] == task["expected_band"],
        })
    return rec


def aggregate(records: list[dict]) -> dict:
    def acc(rs: list[dict]) -> float:
        return round(sum(1 for r in rs if r["correct"]) / len(rs), 4) if rs else 0.0

    by_type = {}
    for ttype in ("search_label", "triage", "priority"):
        rs = [r for r in records if r["type"] == ttype]
        by_type[ttype] = {
            "n": len(rs),
            "accuracy": acc(rs),
        }
    search = [r for r in records if r["type"] == "search_label"]
    priority = [r for r in records if r["type"] == "priority"]
    return {
        "total_tasks": len(records),
        "overall_accuracy": acc(records),
        "by_type": by_type,
        "mean_search_f1": round(
            sum(r["f1"] for r in search) / len(search), 4) if search else 0.0,
        "mean_search_precision": round(
            sum(r["precision"] for r in search) / len(search), 4) if search else 0.0,
        "mean_search_recall": round(
            sum(r["recall"] for r in search) / len(search), 4) if search else 0.0,
        "mean_priority_score_abs_error": round(
            sum(r["score_abs_error"] for r in priority) / len(priority), 4
        ) if priority else 0.0,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default=DEFAULT_TASKS)
    ap.add_argument("--backend", default="sqlite", choices=("sqlite", "postgres"))
    ap.add_argument("--dsn", default=None,
                    help="Postgres DSN (default MAIL_MCP_DSN or postgresql://localhost/mail_mcp)")
    ap.add_argument("--results", default=RESULTS_PATH)
    ap.add_argument("--metrics", default=METRICS_PATH)
    ap.add_argument("--baseline", default=BASELINE_PATH)
    args = ap.parse_args()

    with open(args.tasks) as fh:
        tasks = json.load(fh)["tasks"]

    if args.backend == "postgres":
        results = await _run_postgres(args, tasks)
    else:
        results = await _run_sqlite(args, tasks)

    records = [score(t, r) for t, r in zip(tasks, results)]
    metrics = aggregate(records)
    with open(args.results, "w") as fh:
        json.dump({"task_count": len(records), "records": records}, fh, indent=2)
    with open(args.metrics, "w") as fh:
        json.dump(metrics, fh, indent=2)
    print(json.dumps(metrics, indent=2))

    if not os.path.exists(args.baseline):
        with open(args.baseline, "w") as fh:
            json.dump(metrics, fh, indent=2)
        print(f"baseline created at {args.baseline} (first run)")
        return 0

    with open(args.baseline) as fh:
        baseline = json.load(fh)
    regressions = []
    if metrics["overall_accuracy"] < baseline["overall_accuracy"]:
        regressions.append(
            f"overall accuracy {metrics['overall_accuracy']} < baseline "
            f"{baseline['overall_accuracy']}"
        )
    for ttype, stats in metrics["by_type"].items():
        if stats["accuracy"] < baseline["by_type"][ttype]["accuracy"]:
            regressions.append(
                f"{ttype} accuracy {stats['accuracy']} < baseline "
                f"{baseline['by_type'][ttype]['accuracy']}"
            )
    if regressions:
        print("REGRESSION: " + "; ".join(regressions))
        return 1
    print("gate passed: no regression vs baseline")
    return 0


async def _run_sqlite(args, tasks):
    tmpdir = tempfile.mkdtemp(prefix="mail_mcp_eval_")
    db_copy = os.path.join(tmpdir, "mailbox.db")
    shutil.copy2(DEFAULT_DB, db_copy)
    try:
        return await run_tasks(db_copy, tasks)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _run_postgres(args, tasks):
    """Run the eval against a scratch Postgres database, dropped afterwards."""
    import psycopg
    from urllib.parse import urlparse, urlunparse

    from src.mailbox_pg import PostgresMailbox

    dsn = args.dsn or os.environ.get("MAIL_MCP_DSN", "postgresql://localhost/mail_mcp")
    scratch = f"mail_mcp_eval_{os.getpid()}"
    admin_dsn = urlunparse(urlparse(dsn)._replace(path="/postgres"))
    eval_dsn = urlunparse(urlparse(dsn)._replace(path=f"/{scratch}"))
    admin = psycopg.connect(admin_dsn, autocommit=True)
    try:
        admin.execute(f'CREATE DATABASE "{scratch}"')
    finally:
        admin.close()
    try:
        mb = PostgresMailbox(eval_dsn)
        n = mb.load_corpus(os.path.join(ROOT, "data", "easy_ham"))
        print(f"loaded {n} messages into scratch database {scratch}")
        mb.close()
        return await run_tasks(None, tasks, backend="postgres", dsn=eval_dsn)
    finally:
        admin = psycopg.connect(admin_dsn, autocommit=True)
        try:
            # Terminate any lingering sessions (e.g. a leaked server
            # connection) so DROP DATABASE cannot fail with ObjectInUse.
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (scratch,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{scratch}"')
        finally:
            admin.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
