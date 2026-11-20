# MetalFab 800W — Part 2: Tool Implementation

Execution owner: Codex. Builds the Python CLI/TUI that talks to the welder over USB serial.

Depends on Part 1 (`~/code/xtool/docs/metalfab-part1-decompilation.md`) for exact command semantics,
status schema, and bootloader opcodes. Where Part 1 has not delivered a fact, implement the phase but
leave the command marked `verified: false` rather than guessing.

## Goal

A management and diagnostic suite for the xTool MetalFab Laser Welder (800W, device code HJ004) that
exposes real device status, fault codes, settings, laser control actions, and firmware flashing —
everything the vendor app hides.

## Verified protocol facts

**Transport**: USB CDC serial. Accept both `1a86:7523` (CH340) and `28e9:018a` (GD32 CDC ACM);
default baud **230400**; connect timeout 3 s; command timeout 10 s (must be per-command configurable —
aging test runs for minutes).

**Protocol 1 — line** (normal control): space-joined tokens, delimiter **LF** (auto-detect CRLF),
responses echo the command prefix; GET semantics verify the echo, strip it, return the remainder.

**Protocol 2 — f0f7** (sub-board flashing), exact layout recovered from the vendor client:

```
packet    = [0xF0] [body] [checksum] [0xF7]
body      = prefix_bytes + command_bytes + delimiter(LF)
checksum  = (sum(body_bytes) & 0x7F)
verifyLen = 1 + len(body)     # CLIENT-SIDE ONLY: how many leading response bytes must echo the
                              # request. It is not an on-wire field and must not be transmitted.
min packet size = 3 bytes
```

The separate per-board `verifyLength` values (`[2048, 128]`, `[0, 2]`) from the vendor client's board
config are **unresolved** — their device-side meaning (chunk size, ACK length, or something else) is
Part 1 W4's job. Do not hard-code an interpretation in `f0f7.py`; use the client-side echo length for
framing until Part 1 delivers the answer.

**Boards**: machine (upgrade type 36, `40.71.<n>.2425`), screen (37, `40.71.<n>.2540`, 115200 baud),
laser control board (38, `40.71.<n>.2622`), welding head (39, `40.70.<n>.2722`), wire feeder
(80, `40.211.<n>.5035`, hidden on 800W).

**Upgrade mode entry** (text commands): `M22 S3` (old path) / `M222 S6` (new path).

**Command candidates** recovered from firmware format strings (semantics pending Part 1):
`M800 A%d`, `M801 A%.5f`, `M802 A%.5f`, `M804 A%d`, `M805 A%d B%d C%d D%d E%d F%d`, `M807 A%d`,
`M808`, `M812`, `M814`, `M850 A1 B1`/`A0 B0`, `M859 A1`/`A0`/`A%d`, `M866 A%d`, `M867 A%d`, `M869`,
`M99 A%s.%s`.

**Status vocabulary**: report headers `--- Device Status (STA) ---`, `--- Raycus Status (STR) ---`,
`--- Laser Ruike Status Report ---`, terminator `=== End of Status Report ===`, plus
`Raw Status Value: 0x%08X`. Named fields: Temperature Status, Fault Status, Severe Fault, Power Status,
Interlock 1/2, Emission Status, ACDC Status, Driver 1–4 Status, Laser Status, MOD Status, GATE Mode,
Gas Relay Min/Max Error, Board Key, CNC_Platform Communication, and the full
`Laser Fault {DC_Power, AC_Power, Current, Voltage, Temp, PD Alarm} {Normal, Abnormal, Overhigh, Overlow}` set.

**Settings schema** (from firmware): `first_usage`, `language`, `imperial_units`,
`scan_width_correction`, `origin_correction`, `protective_mirror_temp_threshold`, `gas_pressure_min`,
`gas_pressure_max`, `focus_correction`, `focus_set_status`, `red_laser_set_status`,
`wide_cleaning_working_flag`, `origin_correction_power`; plus per-mode weld presets
(`continus | pulse | spot | cleaning | cutting | wcleaning`) with power, duty, frequency, pulse
duration/interval/count, off-delay, ramp durations, gun scan freq/width, feeder settings, gas
on/off delays.

## Architecture

Python 3, `uv`-managed, at `~/code/xtool`. Package `metalfab`, entry point `metalfab`.
Deps: `pyserial`, `rich`, `typer`; dev: `pytest`.

```
src/metalfab/
  ports.py       # enumerate + classify CH340/GD32 ports (VID/PID, ioreg, tty presence)
  transport.py   # serial open/read/write; exclusive lock; DTR/RTS disabled; timeouts
  session.py     # owns lock, event queue, request correlation, per-command timeouts, capture log
  protocol/line.py    # build, delimiter, echo-prefix check, transform
  protocol/f0f7.py    # build/parse, sum&0x7F checksum, board-type byte
  commands.py    # M-code registry; loads data/commands.json from Part 1
  status.py      # parse status blocks into typed dataclasses; raw-hex fallback
  faults.py      # vocabulary -> explanation + severity; loads data/faults.json
  boards.py      # board types, version parsing, upgrade config
  firmware.py    # f0f7 uploader: handshake, ID check, chunked transfer, per-chunk verify
  discovery.py   # lab harness: passive capture, bounded read-only probing
  capture.py     # record/replay raw sessions
  cli.py         # subcommands
  tui.py         # live dashboard (rich Live table)
data/            # commands.json, faults.json, status.json (Part 1);
                 # verified-overlay.json (Part 2). Contract: docs/data-schema.md
tests/
tools/           # extract_commands.py, strings_report.py, screen_unpack.py
```

### Non-negotiable implementation decisions

1. **Event-queue reader.** Every byte read enters a queue. A line matching the outstanding request's
   prefix completes that request; every other line becomes an async event (status push / heartbeat).
   Never drop non-matching lines — that would silently discard telemetry.
2. **Session layer** owns the exclusive port lock, request/response correlation, per-command timeouts,
   and the capture log. `discovery.py` composes these; it is not a second codec.
3. **Transport hardening.** Open with `exclusive=True`, `dsrdtr=False`, `rtscts=False`. On macOS two
   opens of a tty both succeed without `TIOCEXCL`, and DTR toggling can drop a board into bootloader.
   Treat unexplained checksum/echo failures as port contention first.
4. **Capture/replay is first-class.** Every live session's raw bytes are logged and replayed in tests.
5. **Codecs and parsers are pure functions**, testable without hardware.
6. **One write path.** All device writes go through a single function enforcing an `--arm` gate for
   emitting commands and a dry-run default for settings changes.
7. **Verified flags and overlay.** Every command carries `verified: bool` plus a `danger` class
   (`none | motion | emission | destructive`). A missing `danger` class, or `confidence: unknown`,
   means emission-possible.
   Part 2 records live confirmation in `data/verified-overlay.json` and never rewrites Part 1's data
   files. All data files follow `docs/data-schema.md`.

## Phases

**Phase 0 — Scaffold + transport (TDD).** `uv init`, `pyproject.toml`, pytest. Failing tests first for
port classification and a fake `SerialIO`; then `ports.py` and `transport.py`.

**Phase 1 — Passive discovery, then codecs (TDD).**
1. Quit xTool Studio, open the port at 230400 with DTR/RTS off, and **capture 60–120 s of idle traffic
   before sending anything**. The firmware's "heartbeat packet detection" string implies unsolicited
   pushes; this settles push-vs-poll, framing, delimiter, and cadence at zero risk.
2. Implement `protocol/line.py` and `protocol/f0f7.py` against captured bytes, with round-trip and
   malformed-frame tests.
3. Build `session.py`, `capture.py`, and CLI `ports`, `raw`, `probe`, `capture`.

**Phase 2 — Command registry.**
Load `data/commands.json` from Part 1. Optionally extract the HJ004 driver extension for cross-checking:
xTool Studio downloads per-device extensions to
`~/Library/Application Support/xTool Studio/atomm/exts/<CODE>/`, and installed extensions contain
literal command definitions (verified pattern: `cmd: 'M110 U0'`, `cmd: 'M535 ${...}'`). Connecting the
welder once in xTool Studio should download `HJ004`; `tools/extract_commands.py` greps its `index.js`.

**Safety rule for confirmation.** A command may only be sent unaided when `danger: none` is established
by Part 1. Every command with `danger` absent, `confidence: unknown`, or `verified: false` is treated as
**emission-possible**: `metalfab raw` refuses it unless `--arm --force-unverified` is passed with an
explicit code, and no batch/loop probing of unverified codes is ever performed. Flip `verified` flags by
writing `data/verified-overlay.json`; never edit Part 1's files.

**Phase 3 — Status, faults, live view.**
Drive the report from `data/status.json` (trigger command, field order, raw-status bit map) and map
every vocabulary term to an explanation and severity via `data/faults.json`. Emit
`metalfab status [--json]` and `metalfab monitor` (rich Live table, change highlighting, history ring
buffer). If Part 1 has not yet identified the trigger command, fall back to passive capture: the
status report may arrive unsolicited.

**Phase 4 — Settings management.**
Read/write settings and weld presets. Show a unified diff, require confirmation, verify by read-back.

**Phase 5 — Laser control.**
Test fire, guide laser, aging test. Hard-gated: `--arm` required, interlock/fault preconditions checked
immediately before emission, explicit duration limit, laser-off in a `finally` block.

**Phase 6 — Firmware flashing.**
F0F7 handshake, board ID check, chunked transfer, **read-back verify after every chunk**, explicit
NAK/abort/retry semantics, version-match gate per board, no second client on the port, laser interlock
confirmed off before touching board 38. Start with the welding head; require `--flash` naming the board
and image. The Screen image is a `0x55AA` SPI-filesystem container — do not assume the machine-board
path applies.

**Phase 7 — Docs.** `README.md` (install, command reference). `docs/protocol-notes.md` is owned by
Part 1: append live-observation findings only under a marked `## Live observations (Part 2)` section,
never rewrite Part 1 content.

## Safety design

- Emitting commands requires healthy interlock state and is refused while a severe fault is active.
- `Ctrl-C` and exceptions always send laser-off in a `finally` block.
- Flashing refuses when the image version pattern does not match the board's expected pattern.

## Verification

- `uv run pytest` green for codec, parser, session, transport, and replay tests.
- Replaying a captured idle session reproduces the same parsed events deterministically.
- `metalfab ports` identifies the welder port and reports unknown ports distinctly.
- `metalfab status --json` matches a captured transcript; `metalfab status` renders readable output.
- `metalfab monitor` updates live while idle and during a weld.
- Settings write verified by read-back and survives a power cycle.
- Laser test fire at minimum power with the workpiece removed; emission status confirms off afterwards.
- Firmware readout matches the installed images and the board table.

## Open item — the two USB ports

The documented connector re-enumerated the GD32 machine port (`28e9:018a`) and supported the
reviewed diagnostic queries. The second connector produced a host port-state change and zero
device enumerations during its test. Its signal type, pinout, and purpose remain unidentified.
See §3.9 of the consolidated handoff for the observed host state.
