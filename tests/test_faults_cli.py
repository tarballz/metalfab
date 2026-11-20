"""The offline fault explainer CLI."""

import json
from pathlib import Path

from typer.testing import CliRunner

from metalfab.cli import app

REPO = Path(__file__).resolve().parent.parent
RUNNER = CliRunner()


def test_faults_lists_curated_explanations():
    result = RUNNER.invoke(app, ["--data-dir", str(REPO / "data"), "faults", "--json"])
    assert result.exit_code == 0, result.output
    entries = json.loads(result.stdout)
    assert len(entries) >= 69
    wire = next(entry for entry in entries if entry["id"] == "event.send_wire.1.send_wire_communicate_abnormal")
    assert "wire-feeding module" in wire["explanation"].lower()
    assert wire["popup"] == {"id": 6, "page": 39}
    assert wire["screen_text"]["title"] == "Wire feeding module anomaly detected"


def test_faults_filters_by_event_class():
    result = RUNNER.invoke(app, ["--data-dir", str(REPO / "data"), "faults", "--class", "4", "--json"])
    assert result.exit_code == 0, result.output
    entries = json.loads(result.stdout)
    assert entries
    assert all(entry["evidence"]["event_class"] == 4 for entry in entries)
    # The ten all-clear laser events must carry info severity.
    normals = [entry for entry in entries if entry["id"].endswith("_normal") and entry["severity"] != "info"]
    assert normals == []


def test_faults_includes_heartbeat_manual_entries():
    result = RUNNER.invoke(app, ["--data-dir", str(REPO / "data"), "faults", "--json"])
    assert result.exit_code == 0, result.output
    entries = json.loads(result.stdout)
    ids = {entry["id"] for entry in entries}
    assert "event.heartbeat.1.detection_abnormal" in ids
    assert "event.voltage.1.motherboard_voltage_abnormal" in ids
