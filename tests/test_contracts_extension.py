"""Section-scoped raw-status bits and the enriched fault fields."""

import json
from pathlib import Path

from metalfab.contracts import load_evidence

HEADER = "=== Laser Ruike Status Report ==="
STA = "--- Device Status (STA) ---"
STR = "--- Raycus Status (STR) ---"
END = "=== End of Status Report ==="
DIGEST = "a" * 64


def _evidence(tmp_path: Path, faults: list[dict], raw_bits: list[dict], require_complete=True):
    directory = tmp_path / "data"
    directory.mkdir()
    docs = {
        "commands": {
            "source": {"board": "machine", "image": "fixture.bin", "sha256": DIGEST},
            "commands": [],
        },
        "status": {
            "trigger": {"command": "M808"},
            "report": {
                "headers": [HEADER, STA, STR],
                "terminator": END,
                "fields": [
                    {"order": 1, "key": "raw_sta", "label": "Raw Status Value", "type": "hex", "section": STA},
                    {"order": 2, "key": "raw_str", "label": "Raw Status Value", "type": "hex", "section": STR},
                ],
                "raw_status": {"label": "Raw Status Value", "bits": raw_bits},
                "require_complete_report": require_complete,
            },
        },
        "faults": {
            "severity_enum": ["info", "warning", "fault", "severe"],
            "faults": faults,
        },
    }
    for name, doc in docs.items():
        (directory / f"{name}.json").write_text(json.dumps({"version": 1, **doc}))
    return load_evidence(directory)


def _fault(fault_id, **extra):
    base = {
        "id": fault_id, "source_board": "machine", "source_string": "Fixture",
        "severity": "fault", "explanation": "Fixture", "bit": None,
        "evidence": {"offset": "0x10"}, "verified": False,
    }
    base.update(extra)
    return base


def test_enriched_fault_fields_load(tmp_path):
    """popup and screen_text parse into the fault entry; unknown fields survive."""
    evidence = _evidence(tmp_path, [_fault("f.fixture", popup={"id": 6, "page": 39},
                                          screen_text={"title": "Wire feeding", "body": "Anomaly"})], [])
    fault = evidence.faults["f.fixture"]
    assert fault["popup"] == {"id": 6, "page": 39}
    assert fault["screen_text"] == {"title": "Wire feeding", "body": "Anomaly"}


def test_sectioned_bits_load_for_two_raw_words(tmp_path):
    bits = [
        {"index": 1, "fault_id": "f.high_temp", "section": STA},
        {"index": 0, "fault_id": "f.acdc", "section": STR},
    ]
    evidence = _evidence(tmp_path, [_fault("f.high_temp"), _fault("f.acdc")], bits)
    assert evidence.report is not None
    assert evidence.report.raw_bits == {1: "f.high_temp", 0: "f.acdc"}
    assert evidence.report.bit_sections == {1: STA, 0: STR}


def test_unsectioned_bits_rejected_with_multiple_raw_words(tmp_path):
    """The spurious-issue trap: flat bits cannot decode two raw words.

    The loader is lenient by design (a bad contract disables status, never
    replay), so this surfaces as a failed status load with a named problem.
    """
    bits = [{"index": 1, "fault_id": "f.high_temp"}]
    evidence = _evidence(tmp_path, [_fault("f.high_temp")], bits)
    assert evidence.report is None
    assert any("section" in problem for problem in evidence.problems)


def test_legacy_single_word_bits_still_load(tmp_path):
    """One raw word keeps the legacy flat shape for existing callers."""
    directory = tmp_path / "data"
    directory.mkdir()
    docs = {
        "commands": {"source": {"board": "machine", "image": "fixture.bin", "sha256": DIGEST}, "commands": []},
        "status": {
            "trigger": {"command": "M808"},
            "report": {
                "headers": [HEADER], "terminator": END,
                "fields": [{"order": 1, "key": "raw", "label": "Raw Status Value", "type": "hex"}],
                "raw_status": {"label": "Raw Status Value", "bits": [{"index": 3, "fault_id": "f.legacy"}]},
                "require_complete_report": False,
            },
        },
        "faults": {"severity_enum": ["fault"], "faults": [_fault("f.legacy")]},
    }
    for name, doc in docs.items():
        (directory / f"{name}.json").write_text(json.dumps({"version": 1, **doc}))
    evidence = load_evidence(directory)
    assert evidence.report is not None
    assert evidence.report.raw_bits == {3: "f.legacy"}
    assert evidence.report.bit_sections == {3: None}


def test_fault_bit_word_outside_known_sections_is_a_warning(tmp_path):
    """A mismatched bit.word must never hard-fail replay; it lands in problems."""
    evidence = _evidence(tmp_path, [_fault("f.mismatch", bit={"word": "--- Ghost ---", "index": 5, "active": 1})], [])
    assert any("Ghost" in problem for problem in evidence.problems)
    assert evidence.faults["f.mismatch"]["bit"]["word"] == "--- Ghost ---"
