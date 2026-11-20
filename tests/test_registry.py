"""The real data/commands.json must carry every reviewed operation the tool transmits."""

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMMANDS = json.loads((REPO / "data" / "commands.json").read_text())


def _by_code(code: str) -> dict:
    return next(command for command in COMMANDS["commands"] if command["code"] == code)


def test_m350_job_index_getter_is_reviewed():
    """Phase 2 job browser depends on a registry entry the activation gate accepts."""
    entry = _by_code("M350")
    assert entry["cmd_type"] == "get"
    assert entry["danger"] == "none"
    assert entry["confidence"] == "high"
    assert "Index file content" in entry["evidence"]["anchor"]


def test_m351_system_log_getter_is_reviewed():
    """Phase 2 log viewer depends on a registry entry the activation gate accepts."""
    entry = _by_code("M351")
    assert entry["cmd_type"] == "get"
    assert entry["danger"] == "none"
    assert entry["confidence"] == "high"
    assert "System Log Records" in entry["evidence"]["anchor"]
