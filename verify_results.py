#!/usr/bin/env python3
"""Independently verify eval/results.json and eval/metrics.json.

Recomputes every per-task score and every aggregate from tasks.json and
the recorded result fields, using no code from src/ or eval/run_eval.py.
If anything disagrees with the recorded metrics, it fails loudly.

Usage: python3 verify_results.py
Exit code 0 when every check passes.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__))
TASKS_PATH = os.path.join(ROOT, "eval", "tasks.json")
RESULTS_PATH = os.path.join(ROOT, "eval", "results.json")
METRICS_PATH = os.path.join(ROOT, "eval", "metrics.json")


def fail(checks: list[str], msg: str) -> None:
    checks.append(f"FAIL: {msg}")
    print(f"FAIL: {msg}")


def main() -> int:
    checks: list[str] = []
    tasks = json.load(open(TASKS_PATH))["tasks"]
    results = json.load(open(RESULTS_PATH))
    metrics = json.load(open(METRICS_PATH))
    records = {r["task_id"]: r for r in results["records"]}

    # 1. Every task has exactly one record, and vice versa.
    if set(records) != {t["id"] for t in tasks}:
        fail(checks, "record task ids do not match tasks.json")
    if len(records) != len(tasks):
        fail(checks, f"record count {len(records)} != task count {len(tasks)}")
    if results.get("task_count") != len(tasks):
        fail(checks, "results.json task_count disagrees with tasks.json")

    # 2. Recompute every per-task score from the recorded fields.
    recomputed: list[dict] = []
    for t in tasks:
        r = records.get(t["id"])
        if r is None:
            continue
        if t["type"] == "search_label":
            exp, got = set(t["expected_ids"]), set(r["labeled_ids"])
            tp = len(exp & got)
            p = tp / len(got) if got else 0.0
            rec = tp / len(exp) if exp else 0.0
            f1 = (2 * p * rec / (p + rec)) if (p + rec) else 0.0
            if abs(r["precision"] - round(p, 4)) > 1e-9:
                fail(checks, f"{t['id']}: precision {r['precision']} != {round(p, 4)}")
            if abs(r["recall"] - round(rec, 4)) > 1e-9:
                fail(checks, f"{t['id']}: recall {r['recall']} != {round(rec, 4)}")
            if abs(r["f1"] - round(f1, 4)) > 1e-9:
                fail(checks, f"{t['id']}: f1 {r['f1']} != {round(f1, 4)}")
            if r["correct"] != (got == exp):
                fail(checks, f"{t['id']}: correct flag disagrees with id sets")
            if r["expected_ids"] != sorted(exp):
                fail(checks, f"{t['id']}: recorded expected_ids != tasks.json")
            recomputed.append({"type": "search_label", "correct": got == exp,
                               "f1": f1, "precision": p, "recall": rec})
        elif t["type"] == "triage":
            ok = r["got_label"] == t["expected_label"]
            if r["correct"] != ok:
                fail(checks, f"{t['id']}: correct flag disagrees with labels")
            if r["expected_label"] != t["expected_label"]:
                fail(checks, f"{t['id']}: recorded expected label != tasks.json")
            recomputed.append({"type": "triage", "correct": ok})
        elif t["type"] == "priority":
            ok = r["got_band"] == t["expected_band"]
            if r["correct"] != ok:
                fail(checks, f"{t['id']}: correct flag disagrees with bands")
            if r["expected_score"] != t["expected_score"]:
                fail(checks, f"{t['id']}: recorded expected score != tasks.json")
            if r["score_abs_error"] != abs(r["got_score"] - t["expected_score"]):
                fail(checks, f"{t['id']}: score_abs_error miscomputed")
            recomputed.append({"type": "priority", "correct": ok,
                               "score_abs_error": r["score_abs_error"]})

    # 3. Recompute aggregates and compare with metrics.json.
    def acc(rs):
        return round(sum(1 for r in rs if r["correct"]) / len(rs), 4) if rs else 0.0

    overall = acc(recomputed)
    if metrics["overall_accuracy"] != overall:
        fail(checks, f"overall_accuracy {metrics['overall_accuracy']} != {overall}")
    if metrics["total_tasks"] != len(tasks):
        fail(checks, "metrics total_tasks disagrees with tasks.json")
    for ttype in ("search_label", "triage", "priority"):
        rs = [r for r in recomputed if r["type"] == ttype]
        if metrics["by_type"][ttype]["n"] != len(rs):
            fail(checks, f"{ttype} n disagrees")
        if metrics["by_type"][ttype]["accuracy"] != acc(rs):
            fail(checks, f"{ttype} accuracy disagrees")
    search = [r for r in recomputed if r["type"] == "search_label"]
    if abs(metrics["mean_search_f1"] - round(sum(r["f1"] for r in search) / len(search), 4)) > 1e-9:
        fail(checks, "mean_search_f1 disagrees")
    prio = [r for r in recomputed if r["type"] == "priority"]
    if abs(metrics["mean_priority_score_abs_error"] - round(sum(r["score_abs_error"] for r in prio) / len(prio), 4)) > 1e-9:
        fail(checks, "mean_priority_score_abs_error disagrees")

    if checks:
        print(f"{len(checks)} check(s) failed")
        return 1
    print(f"verify_results: {len(tasks)} tasks, {len(recomputed)} records, "
          f"overall accuracy {overall} — all checks PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
