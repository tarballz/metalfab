# MetalFab 800W — Part 1: Firmware Decompilation

Execution owner: DeepSeek. Read-only against the welder; no serial writes required.

## Objective

Recover the exact wire-level contract of the MetalFab 800W from its firmware images, so the
management/diagnostic tool (Part 2) is built on verified facts instead of guesses. Deliver a command
table, a status-report schema with a bit-to-fault map, fault thresholds, and the device-side
bootloader protocol.

## Inputs

All under `~/code/xtool/firmware/40.71.003/`:

| Image | Size | Initial SP | Reset handler | Arch |
|---|---|---|---|---|
| `1.Machine/800W_Machine_APP_V40.71.003.2425.01.B04.bin` | 225 KB | `0x2004CEB0` | `0x080101C1` | ARM Cortex-M @ `0x08010000` |
| `3.Laser control board/800W_Laser control board_APP_V40.71.001.2622.01.B1.bin` | 28 KB | `0x20001380` | `0x08003169` | ARM Cortex-M @ `0x08003000` |
| `4.Welding head/800W_Welding head_APP_V40.70.006.2722.01.B01.bin` | 25 KB | `0x20001F90` | `0x08003165` | ARM Cortex-M @ `0x08003000` |
| `2.Screen/800W_Screen_V40.71.003.2540.B03.bin` | 2.8 MB | — | — | `0x55AA` container → zlib → 1.5 MB SPI-flash filesystem image |

Use the runtime addresses above for new Cortex-M imports. The existing exports used the
synthetic import base `0x08000000`. The screen package includes assets, a page index, localized
strings, and Lua scripts.

## Tooling

Ghidra 12.2 is installed:
`/opt/homebrew/Cellar/ghidra/12.2/libexec/support/analyzeHeadless`.

Import each image as a raw binary:

```
analyzeHeadless <projectDir> <projectName> \
  -import <image.bin> \
  -processor ARM:LE:32:Cortex \
  -loader BinaryLoader -loader-baseAddr <runtime-base>
```

Firmware is stripped, so the method is **string-anchored xref hunting**: locate a known string,
follow references into the decompiler, name the function, and read outward. All findings record the
file offset of the anchor so they can be re-verified.

## Anchor strings (starting points)

Command grammar:
```
M800 A%d   M801 A%.5f   M802 A%.5f   M804 A%d
M805 A%d B%d C%d D%d E%d F%d   M807 A%d   M808   M812   M814
M850 A1 B1   M850 A0 B0   M859 A1   M859 A0   M859 A%d
M866 A%d   M867 A%d   M869   M99 A%s.%s
M107 command error   M108 command error   pwm_out error
```

Status report:
```
--- Device Status (STA) ---      --- Raycus Status (STR) ---
--- Laser Ruike Status Report ---      === End of Status Report ===
Raw Status Value: 0x%08X
Temperature Status: %s   Fault Status: %s   Severe Fault: %s   Power Status: %s
Interlock 1: %s   Interlock 2: %s   Emission Status: %s   ACDC Status: %s
Driver %d Status: %s   Laser Status: %s   MOD Status: %s   GATE Mode: %s
Gas Relay Min Error   Gas Relay Max Error
Laser Fault {DC_Power, AC_Power, Current, Voltage, Temp, PD Alarm}
Laser Fault {Normal, Abnormal, Overhigh, Overlow}
Board Key Abnormal/Normal   CNC_Platform Communication Abnormal/Normal
```

Diagnostics and safety:
```
NTC Error!!!   LASER IS ERROR!!!! Dangerous!!!!
The heartbeat packet detection is abnormal!!!
Laser aging test set: %d %d %d %d %d %d %d %d %d
Laser aging test stopped, laser turned off
LASER_SUPPLIER_SET: JPT / RUIKE
weld_gun_dc_scan_fault_detect   system_data_default
```

Settings schema:
```
first_usage  language  imperial_units  scan_width_correction  origin_correction
protective_mirror_temp_threshold  gas_pressure_min  gas_pressure_max
focus_correction  focus_set_status  red_laser_set_status
wide_cleaning_working_flag  origin_correction_power
```

## Work items

**W1 — Command dispatch table (machine board).**
Find the line-command parser (expect comparisons on `M` plus numeric decoding, with the format strings
above nearby). For each M-code produce: code, parameter count/types, likely units and range, default
value, observable side effects, and the offset of the evidence. Include commands that never appear as
plain strings (dispatch may be numeric). Explicitly cover the **settings get/set commands** that
Phase 4 of Part 2 depends on, and classify each command's `danger` (none / motion / emission /
destructive) since Part 2 gates on it.

**W2 — Status report builder, trigger command, and bit map (machine board).**
Find the function that prints `--- Device Status (STA) ---`. Recover the exact field order, format
specifiers, and scaling for every `%s`/`%d`/`0x%08X`. Decode the `Raw Status Value` bitfield: which bit
maps to which named fault, and how Normal/Abnormal/Overhigh/Overlow are derived. **Identify the command
that triggers the report** (follow the callers of the print function up to the line-command dispatch)
and record it in `data/status.json`; without this, Part 2's `status.py` cannot elicit a report.

**W3 — Thresholds and constants.**
Recover numeric limits compared against the temperature, gas-pressure, voltage, and current readings:
`protective_mirror_temp_threshold`, `gas_pressure_min/max`, laser fault cutoffs, and any hysteresis.

**W4 — Device-side F0F7 bootloader protocol (machine board).**
Locate the frame parser for start `0xF0`, end `0xF7`, checksum `sum(body) & 0x7F`. Recover every
opcode used for: device-ID check, bootloader-mode check, enter/exit upgrade mode, chunk write,
read-back verify, NAK/abort, and the board-type field values (36 machine, 37 screen, 38 laser control,
39 welding head, 80 wire feeder). Document what `verifyLength` ([2048, 128] per the desktop client)
actually means on the device side.

**W5 — Sub-board command sets (laser control board, welding head).**
Decompile both small images. Determine how they are addressed by the machine board (likely UART
frames), and whether they accept any commands directly. Recover their local fault codes.

**W6 — Screen container unpacking.**
The Screen image starts `55 AA 00 00 00 2A`; it names `3:/bin` and `3:/bin/image.bin`, then embeds
zlib streams (`78 9C`) that inflate to a 1.5 MB image. Unpack the container, extract the filesystem,
and recover UI strings and any user-facing fault/error text.

**W7 — Heartbeat and unsolicited push format (machine board).**
Part 2's discovery phase expects the device may push data unsolicited (anchor: `The heartbeat packet
detection is abnormal!!!`). Recover the heartbeat/push packet format, its cadence, and whether it is
framed as a line or an f0f7 packet. If the device only ever responds to requests, say so explicitly —
that changes how Part 2's reader is implemented.

## Deliverables

All data files follow `docs/data-schema.md`; Part 1 owns them and writes them only from decompilation
evidence.

- `docs/decompiled/<board>/<function>.c` — annotated decompiler exports for every function touched.
- `docs/protocol-notes.md` — command table, status schema, bit map, thresholds, bootloader opcodes,
  each row carrying an evidence offset and a confidence level. **Part 1 owns this file**; Part 2
  appends only under a marked `## Live observations (Part 2)` section.
- `data/commands.json` — machine-readable seed consumed by Part 2's `commands.py`.
- `data/faults.json` — bit/name → severity → plain-English explanation, consumed by `faults.py`.
- `data/status.json` — status-report contract and trigger command, consumed by `status.py`.
- `tools/ghidra/dump_anchors.py` — headless script that reproduces the anchor→xref→decompile pass.

## Acceptance criteria

- Every M-code string found in the machine image has a table entry with inferred semantics and an
  evidence offset; unknown ones are marked unknown rather than guessed.
- Status parser field order and format specifiers match the decompiled printf sequence exactly.
- The raw-status bitfield is fully mapped, or the unmapped bits are explicitly listed.
- Bootloader opcode table is complete enough to implement a safe client, including NAK semantics.
- No claim in **Part 1's sections** of `docs/protocol-notes.md` lacks a code offset. Claims that still
  need live confirmation are marked `pending-capture` and listed for Part 2; Part 1 does not fabricate
  capture references. (Part 2's live-observation section is capture-referenced instead.)

## Risks and mitigations

- **Stripped binary / inlining.** Anchor strings survive; use their xrefs, not symbol names. Accept
  that some helpers are inlined and reconstruct from call sites.
- **Two firmware variants** (JPT vs RUIKE laser sources). Both string sets ship in one image; record
  which branch belongs to which supplier.
- **Time.** W1–W4 are on the critical path; W5–W6 are independent and can be deferred.
- **Do not modify firmware images.** Work on copies only; originals stay untouched in `firmware/`.

## Out of scope

Serial transport, CLI, TUI, settings writes, laser control, flashing client, tests — all Part 2.
