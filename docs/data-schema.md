# Data Schema — shared contract between Part 1 and Part 2

Part 1 produces these files; Part 2 consumes them. Part 1 owns and versions them; Part 2 never edits
them in place. Live-observation results go to `data/verified-overlay.json` (owned by Part 2) and are
merged at load time.

All files carry `"version": 1`. Unknown fields are ignored by consumers; missing required fields are a
load error.

## `data/commands.json`

```json
{
  "version": 1,
  "source": { "board": "machine", "image": "800W_Machine_APP_V40.71.003.2425.01.B04.bin",
              "sha256": "<hash>" },
  "commands": [
    {
      "code": "M850",
      "summary": "short human description",
      "board": "machine",
      "cmd_type": "set",
      "params": [
        { "name": "A", "type": "int", "unit": null, "range": [0, 1], "default": 0 }
      ],
      "danger": "emission",
      "side_effects": ["turns laser emission on/off"],
      "response": { "kind": "echo+text", "fields": [] },
      "evidence": { "offset": "0x00001234", "anchor": "M850 A1 B1" },
      "confidence": "high",
      "verified": false,
      "verified_by": null
    }
  ]
}
```

Required per command: `code`, `summary`, `board`, `cmd_type`, `params`, `danger`, `side_effects`,
`response`, `evidence`, `confidence`, `verified`, `verified_by`.

- `cmd_type`: `get` | `set` | `action`
- Optional `readback` (bool): the handler reports its current value when sent
  without parameters (e.g. bare `M859` reads the CNC state while `M859 A1/A0`
  sets it). A reviewed getter may then use the bare form at `danger: none`.
- `danger`: `none` | `motion` | `emission` | `destructive` — **unknown commands default to `emission`**
  until proven otherwise (see Part 2 safety rule).
- `confidence`: `high` | `medium` | `low` | `unknown`
- The shared registry currently stores addresses from the existing Ghidra exports in
  `evidence.offset`. Subtract the import base `0x08000000` to get the byte offset in the image.
  An entry may cite a handler, dispatcher, or string. `anchor` records the associated string
  when one was available. Runtime load bases are listed in `docs/protocol-notes.md`.
- `verified` is only ever set true via `data/verified-overlay.json`, never edited here.

## `data/faults.json`

```json
{
  "version": 1,
  "severity_enum": ["info", "warning", "fault", "severe"],
  "faults": [
    {
      "id": "laser.dc_power.abnormal",
      "source_board": "machine",
      "source_string": "Laser Fault DC_Power Abnormal",
      "severity": "fault",
      "explanation": "plain-English meaning and likely causes",
      "bit": { "word": "raw_status", "index": 5, "active": 1 },
      "popup": { "id": 6, "page": 39 },
      "screen_text": { "title": "Wire feeding module anomaly detected", "body": "..." },
      "evidence": { "offset": "0x0000abcd" },
      "verified": false
    }
  ]
}
```

Required: `id`, `source_board`, `source_string`, `severity`, `explanation`, `bit` (nullable), `evidence`,
`verified`. `severity` must be one of `severity_enum`.

Optional (nullable): `popup` (`id` and `page`, both non-negative ints — see `data/popups.json`) and
`screen_text` (`title` nonempty string, `body` string; the machine's own on-screen wording, English
segment of the multilingual page).

Optional lists of nonempty strings (the explainer's own depth, beyond what the welder says):
- `causes` — likely root causes, ranked; state what the evidence rules out too.
- `actions` — concrete operator steps; never just "contact after-sales".
- `firmware` — what the firmware does for this event, with function/address evidence
  (e.g. "FUN_0800bed8(10,3) sets the fault state, FUN_0800c3c8(6) raises popup 6").

`bit.word` is either the legacy literal `"raw_status"` (single-word reports) or the owning section
header string from `data/status.json` (e.g. `"--- Device Status (STA) ---"`). A word that is not a
raw-status section is a load-time warning, never a hard failure.

Curated content (explanations, severities, popups, screen text, bits) is maintained in
`tools/fault_annotations.json` and merged in by `tools/build_event_table.py`; hand-editing
`data/faults.json` directly is a mistake the generator will overwrite.

## `data/status.json`

```json
{
  "version": 1,
  "trigger": { "command": "M869", "confidence": "medium", "evidence": { "offset": "0x00002a10" } },
  "report": {
    "headers": ["--- Device Status (STA) ---", "--- Raycus Status (STR) ---",
                "--- Laser Ruike Status Report ---"],
    "terminator": "=== End of Status Report ===",
    "fields": [
      { "order": 1, "key": "temperature_status", "label": "Temperature Status",
        "type": "enum", "values": ["High Temperature", "Low Temperature", "Fault"],
        "unit": null, "scaling": null }
    ],
    "raw_status": { "label": "Raw Status Value", "format": "0x%08X",
                    "bits": [{ "index": 0, "fault_id": "example.fault", "section": "--- Device Status (STA) ---" }] }
  }
}
```

Required: `trigger.command` (may be `null` if genuinely unknown), `report.headers`,
`report.terminator`, `report.fields` with `order` + `key` + `label` + `type`, and `report.raw_status`
with `bits` (may be empty).

Each `bits` entry: `index` (0-31, unique), `fault_id` (an id present in `data/faults.json`), and
optional `section` (one of `report.headers`). **When `require_complete_report` is true and the report
carries more than one raw word, every populated bit must name its `section`** — flat bits cannot be
decoded against two words. Single-word reports keep the legacy flat shape.

`fields[].type`: `enum` | `int` | `float` | `bool` | `string` | `hex`.
`scaling` is a number applied to the raw value, or `null`.

`report.require_complete_report` is optional and defaults to false. When true, a complete
report must contain the listed headers in order, every declared field, and a valid raw word
under the `section` header of each raw-word field.

## `data/popups.json` (generated by Part 1 tooling)

```json
{
  "version": 1,
  "source": { "image": "800W_Machine_APP_V40.71.003.2425.01.B04.bin", "descriptor_offset": 213828, "ram_address": 536892292 },
  "popups": [{ "popup": 1, "page": 35 }]
}
```

Popup id -> screen page, decoded from the scatter-load init data (`tools/decode_popup_table.py`).
Read-only input for the fault annotations and the explainer.

## `tools/fault_annotations.json` (generator input, not a runtime contract)

Keyed by fault id; each value may override `severity`, `explanation`, `popup`, `screen_text`, and
`bit`, and may introduce NEW fault ids (e.g. events the dispatcher generator cannot derive, such as
the heartbeat monitor lines). `tools/build_event_table.py` merges it over the generated base fields
(id, evidence, source_string) and reports annotations whose ids vanished or are unknown.

## `data/verified-overlay.json` (owned by Part 2)

```json
{
  "version": 1,
  "commands": { "M850": { "verified": true, "verified_by": "capture:2026-11-17T16:30:00Z" } },
  "faults":   { "laser.dc_power.abnormal": { "verified": true } }
}
```

Consumers merge: for each key present here, override `verified`/`verified_by` on the corresponding
entry from `commands.json` or `faults.json`. Unknown keys are a load warning, not an error.

## Ownership

- `commands.json`, `faults.json`, `status.json`: Part 1.
- `verified-overlay.json`: Part 2.
- `docs/protocol-notes.md`: Part 1 owns the document; Part 2 appends only under a clearly marked
  `## Live observations (Part 2)` section and never rewrites Part 1 content.
