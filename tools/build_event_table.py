"""Build data/faults.json from the machine board's event dispatcher.

Parses the decompiled FUN_080271b8 (re/machine/func_080271B8.c) case/subcode
structure and resolves the referenced strings through strings.txt. Event classes
and subcodes are firmware ABI, so they are recorded verbatim.

Curated content lives in tools/annotations/*.json (keyed by fault id) and is
merged over the generated base fields: severity, explanation, popup,
screen_text, and bit. An annotation whose id carries a `source_string` is a NEW
entry the dispatcher parser cannot derive (e.g. the heartbeat monitor lines);
one without a source_string that matches nothing generated is reported loudly.
Never hand-edit data/faults.json - regeneration would wipe it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RE = ROOT / "re" / "machine"
OUT = ROOT / "data" / "faults.json"
ANNOTATIONS = ROOT / "tools" / "annotations"

CLASS_NAMES = {
    0: "board_key",
    1: "safe_loop",
    2: "reserved",
    3: "mid_relay",
    4: "laser",
    5: "weld_gun",
    6: "send_wire",
    7: "cnc_platform",
    8: "lcd_screen",
    9: "gas_relay",
    10: "mcode_test",
}

# Class-4 all-clear announcements the old heuristic mislabeled as faults.
LASER_NORMAL_SUBCODES = {6, 8, 10, 12, 15, 17, 20, 22, 24, 26}

# Subcodes the dispatcher handles silently (no serial string to anchor a parse).
SILENT_SUBCODES = [(3, 7), (4, 7)]

# Strings that are protocol echoes rather than human fault text.
ECHO_RE = re.compile(r"^M\d")

# Annotation keys that carry metadata, not fault entries.
IGNORED_KEYS = {"_meta"}

ANNOTATION_FIELDS = ("severity", "explanation", "popup", "screen_text", "bit",
                     "causes", "actions", "firmware")


def severity(text: str) -> str:
    lowered = text.lower()
    if "severe" in lowered or "dangerous" in lowered:
        return "severe"
    if any(word in lowered for word in ("abnormal", "error", "overhigh", "overlow", "fault")):
        return "fault"
    if any(word in lowered for word in ("warning", "min", "max")):
        return "warning"
    return "info"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def load_strings(path: Path) -> dict[str, str]:
    table = {}
    for line in path.read_text(errors="ignore").splitlines():
        if "\t" in line:
            addr, text = line.split("\t", 1)
            table[addr.lower()] = text
    return table


def load_annotations(directory: Path) -> dict[str, dict]:
    """Merge every annotation file in the directory, keyed by fault id."""
    merged: dict[str, dict] = {}
    for path in sorted(directory.glob("*.json")):
        for fault_id, annotation in json.loads(path.read_text(encoding="utf-8")).items():
            if fault_id.startswith("_"):
                continue
            if fault_id in merged:
                print(f"duplicate annotation for {fault_id} in {path.name}")
                continue
            merged[fault_id] = annotation
    return merged


def build(source: Path, strings_path: Path, annotations: dict[str, dict], out: Path) -> None:
    strings = load_strings(strings_path)
    text = source.read_text(errors="ignore")
    parts = re.split(r"\n  case (\d+):", text)

    faults: list[dict] = []
    generated_ids: set[str] = set()
    seen = set()
    for i in range(1, len(parts), 2):
        case = int(parts[i])
        body = parts[i + 1]
        marks = list(re.finditer(r"\(local_8 \+ 4\) == (0x[0-9a-f]+|\d+)", body))
        for j, mark in enumerate(marks):
            start = mark.end()
            end = marks[j + 1].start() if j + 1 < len(marks) else len(body)
            segment = body[start:end]
            texts = []
            for hit in re.finditer(r"s_([A-Za-z0-9_.]+)_([0-9a-f]{8})", segment):
                addr = hit.group(2).lower()
                if addr in strings:
                    resolved = strings[addr].replace("\\n", "").strip()
                    if resolved and not ECHO_RE.match(resolved):
                        texts.append(resolved)
            sub = int(mark.group(1), 0)
            for resolved in dict.fromkeys(texts):
                key = (case, sub, resolved)
                if key in seen:
                    continue
                seen.add(key)
                event_id = f"event.{CLASS_NAMES.get(case, str(case))}.{sub}.{slug(resolved)}"
                if event_id in generated_ids:
                    continue
                generated_ids.add(event_id)
                entry = {
                    "id": event_id,
                    "source_board": "machine",
                    "source_string": resolved,
                    "severity": severity(resolved),
                    "explanation": f"Event class {case} ({CLASS_NAMES.get(case, 'unknown')}), "
                                   f"subcode {sub} ({hex(sub)}).",
                    "bit": None,
                    "evidence": {"offset": "0x080271b8", "event_class": case, "subcode": sub},
                    "verified": False,
                }
                if case == 4 and sub in LASER_NORMAL_SUBCODES:
                    entry["severity"] = "info"
                faults.append(entry)

    for case, sub in SILENT_SUBCODES:
        class_name = CLASS_NAMES.get(case, str(case))
        event_id = f"event.{class_name}.{sub}.silent"
        if event_id in generated_ids:
            continue
        generated_ids.add(event_id)
        faults.append({
            "id": event_id,
            "source_board": "machine",
            "source_string": "(silent)",
            "severity": "info",
            "explanation": f"Event class {case} subcode {sub} takes its side effects without "
                           "printing anything (silent dispatcher branch).",
            "bit": None,
            "evidence": {"offset": "0x080271b8", "event_class": case, "subcode": sub},
            "verified": False,
        })

    # Merge curated annotations over the generated base; report orphans loudly.
    for fault_id, annotation in annotations.items():
        if fault_id in generated_ids:
            entry = next(fault for fault in faults if fault["id"] == fault_id)
            for field in ANNOTATION_FIELDS:
                if field in annotation:
                    entry[field] = annotation[field]
            continue
        source_string = annotation.get("source_string")
        if not source_string:
            print(f"annotation for {fault_id} matches no generated event and has no source_string")
            continue
        faults.append({
            "id": fault_id,
            "source_board": "machine",
            "source_string": source_string,
            "severity": annotation.get("severity", "info"),
            "explanation": annotation.get("explanation", source_string),
            "popup": annotation.get("popup"),
            "screen_text": annotation.get("screen_text"),
            "bit": annotation.get("bit"),
            "causes": annotation.get("causes"),
            "actions": annotation.get("actions"),
            "firmware": annotation.get("firmware"),
            "evidence": annotation.get("evidence", {"offset": "0x080271b8"}),
            "verified": False,
        })

    faults.sort(key=lambda fault: fault["id"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "version": 1,
        "severity_enum": ["info", "warning", "fault", "severe"],
        "faults": faults,
    }, indent=2) + "\n")
    print(f"wrote {out} with {len(faults)} fault entries")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=RE / "func_080271B8.c")
    parser.add_argument("--strings", type=Path, default=RE / "strings.txt")
    parser.add_argument("--annotations", type=Path, default=ANNOTATIONS)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    build(args.source, args.strings, load_annotations(args.annotations), args.out)


if __name__ == "__main__":
    main(sys.argv[1:])
