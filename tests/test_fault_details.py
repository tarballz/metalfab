"""Structured fault details: causes, actions, and firmware behavior with evidence."""

import json
from pathlib import Path

from typer.testing import CliRunner

from metalfab.cli import app
from metalfab.contracts import load_evidence

REPO = Path(__file__).resolve().parent.parent
RUNNER = CliRunner()


def test_contracts_parse_structured_details():
    evidence = load_evidence(REPO / "data")
    assert not evidence.problems
    heartbeat = evidence.faults["event.heartbeat.1.detection_abnormal"]
    assert heartbeat["causes"]
    assert heartbeat["actions"]
    assert heartbeat["firmware"]
    # Lists of nonempty strings, evidence-tagged where firmware-internal.
    assert all(isinstance(item, str) and item for item in heartbeat["causes"])
    assert all(isinstance(item, str) and item for item in heartbeat["firmware"])


def test_key_faults_carry_actionable_guidance():
    evidence = load_evidence(REPO / "data")
    wire = evidence.faults["event.send_wire.1.send_wire_communicate_abnormal"]
    # The welder only says "contact after-sales"; we must go further.
    assert any("harness" in action or "connector" in action or "cable" in action for action in wire["actions"])
    assert any("laser-module link" in cause or "heartbeat" in cause for cause in wire["causes"])


def test_faults_cli_dumps_full_detail_for_one_id():
    result = RUNNER.invoke(app, [
        "--data-dir", str(REPO / "data"), "faults",
        "event.heartbeat.1.detection_abnormal", "--json",
    ])
    assert result.exit_code == 0, result.output
    entry = json.loads(result.stdout)
    assert entry["id"] == "event.heartbeat.1.detection_abnormal"
    assert "The heartbeat packet detection is abnormal!!!" == entry["source_string"]
    assert entry["causes"] and entry["actions"] and entry["firmware"]
    assert entry["explanation"]
    assert entry["evidence"]
