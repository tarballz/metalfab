# MetalFab 800W — Protocol Notes (Part 1)

Recovered by static analysis of the four firmware images in `firmware/40.71.003/` and by live
capture. The existing decompiler exports use a synthetic Ghidra import base of `0x08000000`.
Their function and string addresses are file offsets plus that import base. Explicit RAM addresses
and image file offsets use their stated coordinate system.

Part 1 owns this document. Part 2 appends only under `## Live observations (Part 2)`.

> **Every entry in `data/commands.json` and `data/faults.json` is `verified: false`.** These are
> static-analysis findings, not confirmed device behavior. Treat unverified commands as
> emission-possible and confirm each against hardware before relying on it. The bootloader items in
> section 12 have no on-image evidence and are pending-capture only.

## 1. Boards

| Board | Upgrade type | Version | Image |
|---|---|---|---|
| Machine | 36 | `40.71.003.2425.01` (`B04`) | `1.Machine/...B04.bin` |
| Screen | 37 | `40.71.003.2540` (`B03`) | `2.Screen/...B03.bin` |
| Laser control board | 38 | `40.71.001.2622.01` (`B1`) | `3.Laser control board/...B1.bin` |
| Welding head | 39 | `40.70.006.2722.01` (`B01`) | `4.Welding head/...B01.bin` |
| Wire-feeder upgrade entry | 80 | `40.211.*` | Hidden for HJ004 in Studio; no separate version line in the tested `M99` reply |

The three MCU images are raw Cortex-M Thumb-2 applications. Compare the reset vectors at file
offset 4 with the actual reset stubs to recover the runtime load bases:

| Image | Reset vector | Stub file offset | Runtime load base |
|---|---|---|---|
| Machine | `0x080101c1` | `0x1c0` | `0x08010000` |
| Laser control | `0x08003169` | `0x168` | `0x08003000` |
| Welding head | `0x08003165` | `0x164` | `0x08003000` |

xTool documents a [wire-feeding system for the 800W](https://support.xtool.com/article/2054).
The updater's board list and the observed version count describe firmware reporting.

## 2. Transport

- USB CDC serial. The vendor client accepts `1a86:7523` (CH340) and `28e9:018a` (GD32 CDC ACM).
- Default baud **230400**; client connect timeout 3 s; command timeout 10 s.
- Current host enumeration shows only the GD32 device (`/dev/cu.usbmodem3770DAA904B61`).

## 3. Line protocol

- A command is `M<number>` followed by optional ` <letter><value>` parameters, terminated by LF.
- Responses echo the command prefix (e.g. `M808`), so a client matches on the echoed prefix and reads
  the remainder.
- Parameter parsing is centralised in `FUN_08016bb2(buf, len, index, letter, out)`; values are parsed
  as floats and converted (`VectorFloatToUnsigned`) when the handler wants an integer.
- Negative or missing float parameters fall back to `-1.0` sentinels; setters clamp to configured
  min/max before storing.

## 4. Machine command set

`data/commands.json` is the machine-readable table (49 commands). Every handler body was decompiled
and read, so all entries are `confidence: high`; `verified` stays false until each is confirmed against
hardware. Danger split: 24 getters `none`, 22 settings/actuator writes `motion`, 2 laser-control
sequences `emission` (`M842`, `M850`), and 1 `destructive` (`M222`). **Rule for Part 2: emission and
destructive commands require explicit arming; getters are safe to probe.** Highlights:

| Code | Type | Meaning | Evidence |
|---|---|---|---|
| `M99` | get | Application and available bootloader versions for the four boards | `FUN_080208f8`, consolidated handoff §3.8 |
| `M117` | set | Undocumented integer | `FUN_0801e9c0` |
| `M222` | action | Service/upgrade entry: `S1`, `S2`, `S6`, `S7`, `S8` | `FUN_0801ea80` |
| `M800` | set | Integer setting, clamped to configured range, stored at settings `+0xf8` | `FUN_0801ebe4` |
| `M801` | set | Float setting, clamped, stored at `+0xb8` with scaled copy at `+0xc8` | `FUN_0801ebe4` |
| `M802` | set | Float setting, clamped, stored at `+0x1d8` with scaled copy at `+0x1e8` | `FUN_0801ebe4` |
| `M804` | get | Reads short at `+0x1ac` | `FUN_0801ebe4` |
| `M805` | get | Reads six values via `FUN_0800a618(0..5)` | `FUN_0801ebe4` |
| `M807` | set | Boolean flag via `FUN_08026d1c`, then readback | `FUN_0801ef20` |
| `M808` | get | **Full status report** (section 6) | `FUN_0801ef70` |
| `M812` | get | Single state value via `FUN_0800b3f4` | `FUN_0801f0a4` |
| `M814` | action | `A1` → `FUN_08026f04` + `FUN_08016178`; `B1` → `FUN_0802f384` + `FUN_080292da` | `FUN_0801f0fc` |
| `M817` | get | NTC temperature; prints `NTC Error!!!` when `FUN_0801af68` fails | `FUN_0801f190` |
| `M818` | get | Two shorts + three getters | `FUN_0801f1d8` |
| `M839` | get | Model query, constant `A800` (800W) | `FUN_0801fd48` |
| `M844` | set | uint16, uint8, uint16 via `FUN_0800fae0`, echoed as hex | `FUN_0801fe1c` |
| `M850` | set | Three byte parameters A, B, C (emission-related) | `FUN_08020218` |
| `M859` | set/get | Board key / CNC enable, `0`/`1` via `FUN_080292e4`, readback when no arg | `FUN_080205d0` |
| `M869` | get | Acknowledged no-op / keepalive | `FUN_080208b8` |

Undocumented codes with confirmed formats: `M810`, `M811`, `M813`, `M815`, `M829`, `M831`, `M832`,
`M833`, `M836`, `M840`, `M842`, `M845`, `M847`, `M848`, `M849`, `M853`–`M867` (see the JSON).

**Emission sequences.** `M850 A1 B1` is handled internally by `FUN_0800cfc4`: it calls
`FUN_0800f294(1)`, `FUN_080268bc(1)`, `FUN_08026400(1)`, `FUN_0802645c(1)`, echoes `M850 A1 B1` and
`M812`, then runs the status worker. `M850 A0 B0` is `FUN_0800d004` with `0` for the first, second and
fourth calls and `1` retained for `FUN_08026400`.

The main dispatcher is `FUN_080209bc`: it parses the number after the command letter and switches on
the numeric code, so case values equal the M-code numbers.

### 4.1 Language and display units

- `language` and `imperial_units` exist only as JSON keys inside the default-config blob (`language`
  at `0x08033ef8`, `imperial_units` at `0x08033f8c`, blob at `0x08033fdc`). They are parsed by key
  name at runtime, so no per-key code reference exists.
- The dispatcher at `0x080209bc` subtracts 99 and uses a byte jump table at `0x08020a18`. Entry
  14 (M113) branches to `0x08020b6e` and calls `FUN_0801e750` at `0x08020b72`.
  The handler contains both screen-navigation and language-cycling branches. A PC-relative
  `ADR` at `0x0801e89e` points to `language_switch: %d`. The language branch calls
  `FUN_0802e010`, whose callee `FUN_08005ec8` sends a language-index update to the screen.
  The handler provides neither an absolute language selection nor a standalone read-back.
  See `re/machine/caller_0801E750.c` for the decompiled handler.
- The settings accessor `FUN_0800a424()` is used by `M800`, `M801`, `M802`, `M836`, `M844`, `M856`,
  `M866`, and `M867`, but individual struct field names are not recoverable statically.

The serial getter and absolute setter required by the CLI remain unverified for both preferences.

## 5. Settings and persistence

- Settings live in a RAM struct pointed to by the word at `0x0801be58` (`0x2002085c` at boot).
- `FUN_0800a424()` returns the settings struct; setters clamp against `+0x100`/`+0x104` (M800),
  `+0xc0`/`+0xc4` (M801), `+0x1e0`/`+0x1e4` (M802). These bounds are populated at runtime, **not**
  compile-time constants.
- Configuration is persisted to flash as JSON; the firmware carries defaults for `first_usage`,
  `language`, `imperial_units`, `scan_width_correction`, `origin_correction`,
  `protective_mirror_temp_threshold`, `gas_pressure_min`, `gas_pressure_max`, `focus_correction`,
  `focus_set_status`, `red_laser_set_status`, `wide_cleaning_working_flag`, `origin_correction_power`.

## 6. Status report (`M808`)

`FUN_0801ef70` prints `M808` and calls `FUN_0801bb9c`. Section order:

1. `=== Laser Ruike Status Report ===`
2. `--- Device Status (STA) ---`
3. Device fields (below)
4. `--- Raycus Status (STR) ---`
5. Raycus fields
6. `=== End of Status Report ===`

Field order, struct offsets and value sets are in `data/status.json`. Summary:

**Device (STA)** — raw status `+0x50`; Temperature Status `+0xcf` (High) with low flag `+0xda`;
Laser Status `+0xd0`; External AD `+0xd1`; Fault Status `+0xd2`; Red Light `+0xd3`; MOD `+0xd4`;
GATE Mode `+0xd5`; External Control `+0xd6`; Ready Status `+0xd7`; Laser Emission `+0xd8`;
WAVE Mode `+0xd9`; Red Light Control `+0xdb`; Severe Fault `+0xdc`.

**Raycus (STR)** — raw status `+0x54`; ACDC `+0xde`; Driver 1–4 `+0xdf`–`+0xe2`; Emission `+0xe3`;
Scattered Light 1 `+0xe4`; Power `+0xe5`; Interlock 1 `+0xe6`; Interlock 2 `+0xe7`; Interlock Error
`+0xe8`; Raycus Ready `+0xe9`; Laser Lock `+0xea`.

String constants used by the builder: `ON` at `0x0801bee4`, `OFF` at `0x0801bee8`, `Yes` at
`0x0801bff0`, `No` at `0x0801bff4`, `Closed` at `0x0801c0e4`, `Open` at `0x0801c0ec`.

**The two `Raw Status Value` words have separate firmware parsers.** Cases `0x22` and `0x23`
in `FUN_08021b30` dispatch to `FUN_08021920` and `FUN_08021a24`. Each parses a decimal value
after the colon and saves it directly at the offset printed by the report builder. The same
functions decode these associations, numbered from the least significant bit:

| Report word | Bit-to-field associations recovered from the parser |
|---|---|
| Device (STA) | 1 high temperature, 24 low temperature, 2 Laser Status, 4 External AD, 8 Red Light, 12 MOD, 16 GATE, 18 External Control, 19 Ready, 21 Laser Emission, 22 WAVE, 27 Red Light Control |
| Raycus (STR) | 0 ACDC, 8–11 Drivers 1–4, 12 Emission Status, 13 Scattered Light 1, 14 Power, 16–17 Interlocks 1–2, 18 Interlock Error, 19 Raycus Ready, 20 Laser Lock |

The report builder reads STA Fault Status and Severe Fault from separate bytes. Their full
derivation remains open. Safe Loop events update a flag in a separate machine-state struct
at `0x20015358` through `FUN_080271b8` class 1. Emulating the two raw-word parsers with one bit at
a time confirmed all 25 associations in the table. The two saved reports both contained zero
in the raw words. The CLI displays firmware-decoded fields and requires every section, named
field, and raw word before marking the response complete.

## 7. Event / fault taxonomy

`FUN_080271b8` takes an event struct (`class` byte at `+0`, `code` int at `+4`) and dispatches:

| Class | Meaning | Codes |
|---|---|---|
| 0 | Board Key | 0 Normal, 1 Abnormal |
| 1 | Safe Loop / Stop Switch / Stop Mode | 0 Abnormal, 1 Normal; 2 Stop Normal, 3 Stop Abnormal; 4 Stop Mode Link, 5 Alone |
| 2 | reserved | — |
| 3 | Mid relay / AC touch | 0–1 KA_AB Link/Alone; 2–3 Ctrl Mode SW Link/Alone; 4–5 Safe SW Rely Link/Alone; 6 AC Touch Normal |
| 4 | Laser | 0 LOCK Now, 1 LOCK Cancel, 2 EN enable, 3 EN Disable, 4 Red Light EN enable, 5 Disable, 6 Fault Normal, 8/9 DC Power Normal/Abnormal, 0xA/0xB AC Power Normal/Abnormal, 0xC/0xD/0xE Voltage Normal/Overhigh/Overlow, 0xF/0x10 Current Normal/Overhigh, 0x11/0x12/0x13 Temp Normal/Overhigh/Overlow, 0x14/0x15 PD Alarm Normal/Abnormal, 0x16/0x17 ACDC Normal/Abnormal, 0x18/0x19 Ruike PD Alarm Normal/Abnormal, 0x1A/0x1B Ruike General Fault Normal/Abnormal |
| 5 | Welding gun | 0/1 Power Normal/Abnormal; 2/3 Mirror Temp Normal/Abnormal; 4/5 Communication Normal/Abnormal |
| 6 | Wire feeder | 0/1 Communication Normal/Abnormal |
| 7 | CNC platform | 0/1 Communication Normal/Abnormal |
| 8 | LCD screen | 0 idle, 1 work, 2 error |
| 9 | Gas relay | 0 Normal, 1 Min Error, 2 Max Error |
| 10 | Factory test | 0 Lock Pin, 1 En Pin, 2 DAC Pin, 3 PWM Pin |

`data/faults.json` contains the full machine-readable table with severities and explanations.

Related diagnostics: `LASER IS ERROR!!!! Dangerous!!!!` (`FUN_0801f4e0`), `NTC Error!!!`
(`FUN_0801f190`). The heartbeat strings (`0x0802572b`, `0x0802575c`) sit beside `M99\n`.
The route and cadence of that heartbeat remain unverified. The 90-second idle USB trace was silent.
Other saved receive-only traces contain asynchronous configuration and Safe Loop events.

## 8. Upgrade path and the bootloader finding

- The client enters upgrade mode with the text command `M22 S3` (old path) or `M222 S6` (new path).
- `M222` handling (`FUN_0801ea80`): `S1` performs `FUN_080168ac` plus GPIO/power setup; `S2` and `S6`
  only echo; `S7`/`S8` undo the setup and echo.
- **The F0F7 bootloader is not in the distributed application image.** No frame parser for start
  `0xF0`, end `0xF7`, checksum `sum(body) & 0x7F` exists in these files, and `M222 S6` does not jump
  to a bootloader. The device-side ACK/NAK opcodes and the meaning of the client-side
  `verifyLength` values (`[2048,128]`, `[0,2]`) cannot be recovered from these images.
- The client code includes board identification, transfer progress, final status, and an
  error-exit branch. Those host branches leave the device's actual behavior on interrupted
  transfers unverified.
- `metalfab firmware plan` verifies each candidate image against digests independently matched
  to the publisher's HJ004 `40.71.003` ZIP. It checks the target board against installed versions.
  Live preflight capture `20261118T020440Z-39a009c9` identified the welding head as the sole
  update candidate on the tested machine.

### HJ004 update audit

The published `40.71.003` ZIP (SHA-256
`96c6698687dddf91de7261e461f0061643239606d65371e3464c964fdb3c5d9a`) has four firmware files.

| Board | Stock image evidence | Installed comparison |
|---|---|---|
| Machine | Cortex-M application, firmware family `2425` | Current |
| Screen | `55 AA` resource container with its own validation branch | Current |
| Laser control | Cortex-M application, firmware family `2622` | Current |
| Welding head | Cortex-M application, firmware family `2722` | `40.70.005.2722.01.B02` to `40.70.006.2722.01.B01` |

The desktop client's `verifyFirmware` and `verifyScreenFirmware` routines examine file
content and compare embedded versions to the expected family. This is evidence of host-side
compatibility checking. The ZIP has four application/resource files, with no separate
bootloader binary or detached signature. The evidence collected so far leaves device-side
image authentication and interrupted-transfer recovery unresolved for all four boards.
The CLI has verified stock-image preflight and installed-version read-back for this unit.

### Welding-head update candidate

The live version capture reports head application `40.70.005.2722.01.B02` and head bootloader
`40.70.001.2722.00.B01`. Head-only preflight against that capture identifies the publisher-matched
`40.70.006.2722.01.B01` application as the upgrade candidate. The image is 24,836 bytes and its
reset stub resolves to application base `0x08003000`.

The host assigns the welding head its own firmware-family check and routes its update through
the machine connection. The application image leaves the installed bootloader's authentication
and post-interruption recovery behavior unverified. Head-only preflight used the saved version
capture and did not write to the welder.

The inspected host routine checks the target and asks for final transfer status before
requesting a reboot. Its error-exit branch retries a cleanup request. The routine does not
explicitly query the head's application version after reboot. Our CLI can independently
compare that reported version with the selected stock image through
`firmware plan --verify-installed`.

The host's head transfer sends a file checksum as transfer metadata. The image
validation routine checks the embedded version and board family. The code-signature
helper in the desktop bundle is used for desktop installer downloads. These checks
provide evidence about the host and installer. The published head image and support
material reviewed here do not establish the installed `40.70.001.2722.00.B01`
bootloader's image-authentication or interrupted-write recovery behavior.

The installed desktop bundle includes both a native `serialport` binding for macOS and
Web Serial code in the firmware renderer. The desktop `upgradeBoard` routine lives in
the packaged main-process JavaScript. The Electron preload bridge forwards messages,
and the native binding supplies generic serial I/O. Board matching and update
orchestration happen in the host application. The bootloader's authorization and
recovery behavior are separate from that transport layer.

A follow-up search covered the installed app archive, its bundled extension archive, and
the publisher's `40.70.013`, `40.71.003`, and `40.72.002` releases. The releases supplied head
application images. The search yielded no separately identified head bootloader, signature
manifest, or recovery specification. The `40.72.002` release contains a byte-identical copy of
the HJ004 head application. That match is specific to the head image. The audited update target
remains the HJ004 package.

## 9. Sub-board command sets

**Welding head** (`FUN` table in `re/head/`): `M100 A%.1f%c`, `M101 A"%s"`, `M800 A%d`,
`M801 A%.5f`, `M802 A%.5f`, `M803 A%d`, `M804 A%d`, `M805 A%d B%d C%d D%d E%d F%d`,
`M806 A%d B%d C%d D%d`, `M99 A%s.%s B%s`. Version string `40.70.006.2722.01` at `0x08004b18`.
Factory-data manager strings at `0x080023bc`–`0x080025fb`.

**Laser control board** (string constants only in `re/laser/strings.txt`; no function references were
resolved, so this list is lower confidence than the machine or welding-head sets): `M101`,
`M812 A%d`, `M850 A%d`/`B%d`/`C%d`, then
`M870`–`M898`. Notable: `M898 Age counts:%d` at `0x08004e2c`, `M897 A%d B%d C%d`,
`M896`, `M885 A%d B%.2f`, `M886 A%d B%.2f`, `M887`, `M888`, `M890`, `M891`,
`M99 A%s.%s B%s`. Diagnostics strings: `Stepper_id:0x%x`, `Edec_distance:%.16f`.

## 10. Screen image

`2.Screen/...B03.bin` is a `55 AA` container: header `55 AA 00 00 00 2A`, then named entries
(`3:/bin`, `3:/bin/image.bin`), then a sequence of **19 zlib streams** starting at offset 62. Three
streams inflate to exactly 1,572,864 bytes (1.5 MB) each — SPI-flash images, mostly `0xFF` with
embedded compressed assets; the other sixteen inflate to 54–150 KB asset blobs.

The container also carries PNG assets, an uncompressed `3:/bin/screen.bin` page bundle, and Lua
scripts. The `screen.bin` payload starts at file offset `0x2508dd`. Its `DC10` header gives the
page count and index offset. Each eight-byte page entry contains a little-endian length and a
payload-relative offset. The pages include UTF-8 text in multiple languages. See the wire-feeding
alert below for a verified page lookup.

## 11. Evidence index

| Address | What |
|---|---|
| `0x08016bb2` | parameter parser (`letter` + float) |
| `0x0801bb9c` | status report builder |
| `0x0801ef70` | `M808` handler → status builder |
| `0x0801ebe4` | `M800`/`M801`/`M802`/`M804`/`M805`/`M806` |
| `0x08020218` | `M850` A/B/C setter |
| `0x0800cfc4` / `0x0800d004` | emission on / off sequences |
| `0x080209bc` | main command dispatcher |
| `0x080271b8` | event/fault dispatcher |
| `0x0801ea80` | `M222` service modes |
| `0x0800a424` | settings struct accessor |

## 12. Open items

- STA Fault Status and Severe Fault derivation and any raw bits outside the field table.
- Bootloader ACK/NAK and `verifyLength` semantics (not in image; pending-capture).
- Heartbeat route and cadence.
- Screen UI strings — **confirmed**: the uncompressed `screen.bin` page bundle contains
  multilingual plaintext.

## Live observations (Part 2)

The touchscreen showed the following installed versions on the tested HJ004.

| Board | Installed version |
|---|---|
| Machine | `40.71.003.2425.01.B04` |
| Screen | `40.71.003.2540.01.B03` |
| Laser control | `40.71.001.2622.01.B01` |
| Welding head | `40.70.005.2722.01.B02` |

- The documented USB cable reconnect produced a new GD32 USB device instance with the same serial
  path, `/dev/cu.usbmodem3770DAA904B61`. Lasering was disabled and xTool Studio was closed.
- Capture `20261118T005147Z-2cb2e29f` recorded zero RX and TX bytes over 90.07 seconds at
  230400 baud while the machine was idle.
- Capture `20261118T005338Z-ee22f64e` contains a single `M808` request and its complete report
  after the earlier touchscreen fault cleared. The actual opening heading is
  `=== Laser Ruike Status Report ===`. Replay reproduced 26 named fields and two raw status words,
  both `0x00000000`. The report showed lasering disabled, emission off, general fault status
  normal, and no severe fault. No wire-feeder event appeared in either capture.
- The live check used a temporary registry copy with the SHA-256 computed from the named stock
  machine image and matched against the reviewed profile. The generator now records that SHA-256
  in the shared registry. The captures are kept in the local MetalFab logs directory, and the
  command observation is recorded in the verification overlay.

### Installed firmware version query

Capture `20261118T015442Z-15adc177` confirmed the standard `metalfab firmware versions`
entry point on the physical machine. The capture contains one `M99` request and four board
responses. All application versions match the touchscreen table above. The machine, laser
control, and welding head also returned bootloader versions. The screen response contains its
application version only. The verification overlay records this observation.

### Touchscreen display units

During passive capture `20261118T011106Z-3a3350c8`, the operator switched from inches to
millimeters and restored inches. At 27.17 s and 39.40 s the controller logged the start and
successful completion of a configuration flash save. The USB stream contained no setting name,
unit value, or corresponding command for either change. The 120-second capture transmitted zero
serial bytes. This confirms persistence activity and leaves the USB read/write contract unknown.

### Touchscreen language

The operator switched from Spanish back to English during passive capture
`20261118T011455Z-2b3d9cc3`. The controller logged the start and successful completion of a
configuration flash save at 11.39 s. The two messages match those observed for display units.
The stream did not include a language name, index, or command.

### Administrative touchscreen tabs

Capture `20261118T011923Z-5f96b04a` began after the administrative menu was open and
recorded zero RX and TX bytes over 75.01 s. A separate capture,
`20261118T012110Z-4ce3467b`, recorded zero RX and TX bytes over 50.10 s while the operator
switched to **Machine information** and back to **Machine settings**.

The photo shows calibration and machine-limit controls under **Machine settings**. The
tab-switch capture establishes that this touchscreen navigation does not produce a USB
settings readout during the observed interval.

### Safety interlock connection

The operator connected the safety interlock device during passive capture
`20261118T012308Z-e314ab84`. At 11.31 s, one serial read contained these events in order:

1. `Safe Loop Normal`
2. `Safe Loop Abnormal`
3. `Safe Loop Normal`

The recording gives the event order and the chunk timestamp. The duration between events
is unknown. The final reported event was `Safe Loop Normal`. The capture sent zero bytes,
and the verification overlay records both observed event strings.

After the operator detached the workpiece connection while leaving the device plugged in,
capture `20261118T012543Z-d43f1f7a` recorded one read-only `M808` request and a complete
reply. All 26 named fields and both raw words matched the earlier report
`20261118T005338Z-ee22f64e`. Both reported interlock fields were `Open`, lasering was
`Disabled`, and emission was `OFF`. These are snapshots before attachment and after
detachment. The passive attachment trace is the evidence for the intervening events.

### Wire-feeding alert mapping

The historical touchscreen photo shows **Wire feeding module anomaly detected**. The
machine dispatcher `FUN_080271b8` handles **event class 6, subcode 1** by requesting popup
ID 6 via `FUN_0800c3c8` and printing `Send Wire Communicate Abnormal` over serial.
Class 6, subcode 0 clears popup ID 6 and prints the normal counterpart.

The machine's initialized popup table at RAM `0x2000c384` maps popup 6 to screen page
39 (`0x27`). The initialization data is recovered from the scatter-load descriptor at image
offset `0x34344`. Both emulating its data decompressor and independently decoding the
compressed stream produced the same table. The screen page index points to file offset
`0x2725b0`; the exact title and body from the photo begin at `0x2725dd` and `0x27284a`.
This establishes the firmware path from the communication fault to the pictured alert.
The original episode has no concurrent serial capture, so its underlying cause is unknown.

Cross-checking the same dispatch and screen index exposed several many-to-one and paired mappings:

| Event class and subcode | Popup ID | Screen page | Display text |
|---|---|---|---|
| 5, 1 or 5 | 9 | 42 | Welding head communication exceptions |
| 5, 3 | 8 | 41 | Lens protector temperature exceptions |
| 6, 1 | 6 | 39 | Wire feeding module anomaly detected |
| 9, 1 | 1 | 35 | Gas pressure too low |
| 9, 2 | 2 | 36 | Gas pressure too high |

The welding-head popup can represent a power fault or a communication fault. The serial event
distinguishes those cases.
