"""Agent tests: one task of each type, completed through the real MCP server."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.agent import run_tasks

ROOT = os.path.join(os.path.dirname(__file__), "..")
TASKS = json.load(open(os.path.join(ROOT, "eval", "tasks.json")))["tasks"]


def _one(ttype):
    return next(t for t in TASKS if t["type"] == ttype)


@pytest.mark.asyncio
async def test_agent_search_labels_exact_set(db_copy):
    task = _one("search_label")
    (result,) = await run_tasks(db_copy, [task])
    assert sorted(result["labeled_ids"]) == sorted(task["expected_ids"])


@pytest.mark.asyncio
async def test_agent_triage_matches_ground_truth(db_copy):
    task = _one("triage")
    (result,) = await run_tasks(db_copy, [task])
    assert result["label"] == task["expected_label"]


@pytest.mark.asyncio
async def test_agent_priority_band_matches(db_copy):
    task = _one("priority")
    (result,) = await run_tasks(db_copy, [task])
    assert result["band"] == task["expected_band"]
    assert result["score"] == task["expected_score"]
