"""Section-scoped raw-status bit decoding against two-word reports."""

import json
from pathlib import Path

from metalfab.contracts import load_evidence
from metalfab.reports import Decoder

HEADER = "=== Laser Ruike Status Report ==="
STA = "--- Device Status (STA) ---"
STR = "--- Raycus Status (STR) ---"
END = "=== End of Status Report ==="
DIGEST = "a" * 64


def _decoder(tmp_path: Path, sta_bits: list[dict], str_bits: list[dict]):
    directory = tmp_path / "data"
    directory.mkdir()
    faults = [
        {"id": "f.high_temp", "source_board": "machine", "source_string": "Temp High",
         "severity": "fault", "explanation": "Hot",
         "bit": {"word": STA, "index": 1, "active": 1},
         "popup": {"id": 12, "page": 58},
         "screen_text": {"title": "Machine temperature too high", "body": "Cool down"},
         "evidence": {"offset": "0x10"}, "verified": False},
        {"id": "f.acdc", "source_board": "machine", "source_string": "ACDC Bad",
         "severity": "fault", "explanation": "Power stage",
         "bit": {"word": STR, "index": 0, "active": 1},
         "evidence": {"offset": "0x20"}, "verified": False},
    ]
    docs = {
        "commands": {"source": {"board": "machine", "image": "fixture.bin", "sha256": DIGEST}, "commands": []},
        "status": {
            "trigger": {"command": "M808"},
            "report": {
                "headers": [HEADER, STA, STR], "terminator": END,
                "fields": [
                    {"order": 1, "key": "raw_sta", "label": "Raw Status Value", "type": "hex", "section": STA},
                    {"order": 2, "key": "raw_str", "label": "Raw Status Value", "type": "hex", "section": STR},
                ],
                "raw_status": {"label": "Raw Status Value", "bits": sta_bits + str_bits},
                "require_complete_report": True,
            },
        },
        "faults": {"severity_enum": ["fault"], "faults": faults},
    }
    for name, doc in docs.items():
        (directory / f"{name}.json").write_text(json.dumps({"version": 1, **doc}))
    return Decoder(load_evidence(directory))


def _report(sta_word: str, str_word: str) -> str:
    return (
        f"{HEADER}\n{STA}\nRaw Status Value: {sta_word}\n"
        f"{STR}\nRaw Status Value: {str_word}\n{END}\n"
    )


def test_sectioned_bits_decode_both_words(tmp_path):
    """STA bit 1 and STR bit 0 each attach their own fault."""
    decoder = _decoder(tmp_path,
                       [{"index": 1, "fault_id": "f.high_temp", "section": STA}],
                       [{"index": 0, "fault_id": "f.acdc", "section": STR}])
    events = list(decoder.feed(_report("0x00000002", "0x00000001").encode()))
    report = next(event.report for event in events if event.report is not None)
    assert sorted(fault["id"] for fault in report.faults) == ["f.acdc", "f.high_temp"]
    assert not report.issues


def test_wrong_polarity_does_not_attach(tmp_path):
    """A clear bit on an active=1 fault produces no fault entry."""
    decoder = _decoder(tmp_path,
                       [{"index": 1, "fault_id": "f.high_temp", "section": STA}],
                       [{"index": 0, "fault_id": "f.acdc", "section": STR}])
    events = list(decoder.feed(_report("0x00000000", "0x00000000").encode()))
    report = next(event.report for event in events if event.report is not None)
    assert report.faults == []
    assert not report.issues


def test_fault_details_carry_popup_and_screen_text(tmp_path):
    """The explainer needs the machine's own words on the fault entry."""
    decoder = _decoder(tmp_path,
                       [{"index": 1, "fault_id": "f.high_temp", "section": STA}],
                       [])
    events = list(decoder.feed(_report("0x00000002", "0x00000000").encode()))
    report = next(event.report for event in events if event.report is not None)
    fault = report.faults[0]
    assert fault["popup"] == {"id": 12, "page": 58}
    assert fault["screen_text"] == {"title": "Machine temperature too high", "body": "Cool down"}
