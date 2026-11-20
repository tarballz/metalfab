# Probe runbook: unclassified M-codes

The goal is to turn the unreviewed registry entries into verified observations.
Safety invariants, in order of importance:

1. One command per invocation. There is no batch or loop anywhere in the raw
   path, and nothing in this file changes that.
2. Never probe while xTool Studio runs (the tool refuses), and never probe
   while a severe fault is active on the machine.
3. Getters first. Only after a code answers harmlessly as a getter do you
   consider its parameterized forms.
4. Every probe is recorded: a capture is written automatically, and a
   successful reply is written to the verified overlay with
   `--record` (or `metalfab verify`).

## How to run a probe

```sh
uv run metalfab raw "M112" --port /dev/cu.usbmodemYOURDEVICE \
  --device-version 40.71.003.2425.01.B04 --record
```

The unaided path only admits the reviewed probe getters (`M100`, `M112`).
Everything else needs the armed path, which requires the exact wire retyped
plus explicit flags:

```sh
# Registry danger motion, confidence high, unreviewed getter/setter forms:
uv run metalfab raw "M812" --arm --force-unverified --port ... --device-version ...

# Registry danger emission or destructive (screen-cluster codes, laser forwards):
uv run metalfab raw "M820 A1" --arm --force-unverified --allow-emission \
  --port ... --device-version ...
```

If the code is not in the registry at all, `--record` will refuse to write the
overlay (the registry is authoritative); run without `--record` and consider
adding the code to `tools/build_command_table.py` first.

## Probe order

Group 1: reviewed probe getters, unaided.

| Code | Registry | Notes |
|---|---|---|
| M112 | get/none/high | 24-bit value via FUN_0802702c |
| M100 | get/none/high | Sensor float via FUN_0800acfc/FUN_08000c8c |

Group 2: armed, registry motion or high-confidence unreviewed.

| Code | Registry | Notes |
|---|---|---|
| M812 | get/none/high | Single state value (observed live: A1) |
| M804 | get/none/high | Settings +0x1ac short |
| M805 | get/none/high | Six settings values |
| M810 | get/none/high | State value (observed live: A5) |
| M811 | get/none/high | Five status values |
| M813 | get/none/high | Two state values |
| M815 | get/none/high | Settings +0x388 |
| M817 | get/none/high | NTC temperature (observed live: A30) |
| M818 | get/none/high | Six status values (observed live) |
| M829/M831/M832/M833 | get/none/high | Status bundles and floats |
| M840/M845/M848 | get/none/high | State bundles |
| M864/M865 | get/none/high | State values (observed live) |
| M857 | get/none/high | Settings +0x1b4 |
| M114 | set/motion/high | Boolean with readback echo |
| M118 | action/motion/high | Zeroes settings 0x43/0x44 and applies |
| M157 | set/motion/high | Writes settings 0x9d |
| M113 | set/motion/high | Screen language cycle: watch the touchscreen |

Group 3: armed with `--allow-emission` (registry emission, unreviewed).

| Code | Registry | Notes |
|---|---|---|
| M816, M819-M828, M834, M835, M837, M838 | set/emission/medium | Screen-channel cluster: verify USB dispatch first, or the probe is a silent no-op |
| M841, M843, M846, M851, M852, M868 | set/emission/medium | Dispatched; decompiles exist; semantics unknown |
| M870-M898 | set/motion/medium | Forwarded to the laser control board; probe getters only, one at a time |

Group 4: do not probe without a specific reason.

| Code | Reason |
|---|---|
| M222 | destructive; service-mode entry |
| M842, M850 | emission; the machine self-issues M850 around interlocks |

## After each probe

1. Note the response in the capture (the tool prints the path).
2. `--record` writes the overlay entry with the capture id.
3. Append one line under `## Live observations (Part 2)` in
   `docs/protocol-notes.md` describing the response.
4. If the response establishes the semantics, move the code into
   `tools/build_command_table.py` (SEMANTICS with a real summary and danger)
   and, for getters, consider `PROBE_GETTERS` after hand review.

## Known traps

- Several codes share format strings with the screen-channel dispatcher; a
  USB probe can be a safe no-op. Check `re/machine/handler_080209BC.c` dispatch
  before spending a probe.
- M350 answers `Failed to open index file: -2` until the machine has run a
  machining job; that is a valid response, not a fault.
- The laser board may be inside a silent heartbeat window; a probe that times
  out is retryable after the episode, never in a loop.
