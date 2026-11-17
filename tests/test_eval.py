"""Eval artifact tests: the recorded metrics are what verify_results says."""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = os.path.join(os.path.dirname(__file__), "..")


def test_metrics_match_recorded_run():
    metrics = json.load(open(os.path.join(ROOT, "eval", "metrics.json")))
    assert metrics["total_tasks"] == 40
    assert metrics["overall_accuracy"] == 1.0
    assert metrics["by_type"]["search_label"]["accuracy"] == 1.0
    assert metrics["by_type"]["triage"]["accuracy"] == 1.0
    assert metrics["by_type"]["priority"]["accuracy"] == 1.0
    assert metrics["mean_search_f1"] == 1.0
    assert metrics["mean_priority_score_abs_error"] == 0.0


def test_verify_results_passes():
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "verify_results.py")],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "all checks PASS" in proc.stdout


def test_baseline_matches_metrics():
    baseline = json.load(open(os.path.join(ROOT, "eval", "baseline.json")))
    metrics = json.load(open(os.path.join(ROOT, "eval", "metrics.json")))
    assert baseline == metrics
