"""The fault generator must preserve curated annotations and add manual entries."""

import importlib.util
import json
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent


pytestmark = pytest.mark.skipif(
    not (REPO / "re" / "machine" / "func_080271B8.c").exists(),
    reason="decompiled firmware artifacts not present",
)


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "build_event_table", REPO / "tools" / "build_event_table.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build(tool, tmp_path: Path, annotations: dict[str, dict]) -> Path:
    annotations_dir = tmp_path / "annotations"
    annotations_dir.mkdir()
    (annotations_dir / "curated.json").write_text(json.dumps(annotations, indent=2))
    out = tmp_path / "faults.json"
    tool.main(["--source", str(REPO / "re" / "machine" / "func_080271B8.c"),
               "--strings", str(REPO / "re" / "machine" / "strings.txt"),
               "--annotations", str(annotations_dir),
               "--out", str(out)])
    return out


@pytest.fixture(scope="module")
def tool():
    return _load_tool()


def _entry(faults: list[dict], fault_id: str) -> dict:
    return next(fault for fault in faults if fault["id"] == fault_id)


def test_annotation_overrides_survive_regeneration(tool, tmp_path):
    """Curated severity/explanation/popup/screen_text/bit replace generated fields."""
    annotations = {
        "event.send_wire.1.send_wire_communicate_abnormal": {
            "severity": "fault",
            "explanation": "Curated wire-feeder explanation.",
            "popup": {"id": 6, "page": 39},
            "screen_text": {"title": "Wire feeding module anomaly detected", "body": "..."},
            "bit": None,
        },
    }
    out = _build(tool, tmp_path, annotations)
    faults = json.loads(out.read_text())["faults"]
    entry = _entry(faults, "event.send_wire.1.send_wire_communicate_abnormal")
    assert entry["explanation"] == "Curated wire-feeder explanation."
    assert entry["popup"] == {"id": 6, "page": 39}
    assert entry["screen_text"]["title"] == "Wire feeding module anomaly detected"


def test_new_annotation_ids_are_appended(tool, tmp_path):
    """Annotations with a source_string introduce entries the dispatcher lacks."""
    annotations = {
        "event.heartbeat.1.detection_abnormal": {
            "source_string": "The heartbeat packet detection is abnormal!!!",
            "severity": "fault",
            "explanation": "Monitor tripped.",
            "popup": None,
            "screen_text": None,
            "bit": None,
        },
    }
    out = _build(tool, tmp_path, annotations)
    faults = json.loads(out.read_text())["faults"]
    entry = _entry(faults, "event.heartbeat.1.detection_abnormal")
    assert entry["source_string"] == "The heartbeat packet detection is abnormal!!!"
    assert entry["explanation"] == "Monitor tripped."
    assert entry["verified"] is False


def test_orphan_annotation_is_reported(tool, tmp_path, capsys):
    """An annotation whose id matches nothing generated is a loud error."""
    out = _build(tool, tmp_path, {"event.ghost.1.nothing": {"severity": "info",
                                                            "explanation": "gone"}})
    captured = capsys.readouterr().out
    assert "event.ghost.1.nothing" in captured
    assert json.loads(out.read_text())["faults"]  # generation still succeeds


def test_laser_normal_events_default_to_info(tool, tmp_path):
    """The ten class-4 all-clear subcodes must not be mislabeled fault."""
    out = _build(tool, tmp_path, {})
    faults = json.loads(out.read_text())["faults"]
    for sub in (6, 8, 10, 12, 15, 17, 20, 22, 24, 26):
        entry = next((f for f in faults
                      if f["evidence"].get("event_class") == 4
                      and f["evidence"].get("subcode") == sub
                      and "normal" in f["id"]), None)
        assert entry is not None, f"missing class-4 subcode {sub} normal entry"
        assert entry["severity"] == "info", f"subcode {sub} normal mislabeled {entry['severity']}"


def test_silent_subcodes_are_present(tool, tmp_path):
    """Class 3 sub 7 and class 4 sub 7 exist with the (silent) marker."""
    out = _build(tool, tmp_path, {})
    faults = json.loads(out.read_text())["faults"]
    ids = {fault["id"] for fault in faults}
    assert "event.mid_relay.7.silent" in ids
    assert "event.laser.7.silent" in ids
