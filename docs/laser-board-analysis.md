# Laser control board: decompilation findings

Firmware: `800W_Laser control board_APP_V40.71.001.2622.01.B1.bin` (27,816 bytes).
Runtime load base `0x08003000`; the Ghidra project imports at `0x08000000`, so every
address below is a file offset in the shipped image. Add `0x3000` for the runtime
address. 381 functions analyzed; decompiles and string tables live in `re/laser/`.

## The machine's heartbeat, from the module's side

- The M-code line parser (0x1D98) feeds the dispatcher (0x5050); case 99
  (0x4FB0) sends the version reply through the F0 frame builder and bumps a
  0..100 rolling counter at RAM 0x200001A0, published to 0x20000000.
- The supervisor (0x2DB8) runs every main-loop pass and samples the counter once
  per second. Three consecutive samples without a change set the link flag
  (0x20000001) low and drop output PB8; any change raises both. The main board
  implements the identical 3-strike rule, so a 2-3 s stall on either side is
  reported as a 3-4 s fault window.
- The responder runs synchronously in the cooperative main loop: any blocking
  work starves M99 replies. The only multi-second blocker found is the M898
  aging command (0x4C90), which dwells 2 x T ms per count (T up to 5000 ms)
  while still emitting its own `M898 Age counts` frames.

## Watchdog and reset behavior

- No watchdog is started or fed anywhere in the image. The only IWDG register
  reference (0x2530) writes 0 to 0x40003000, which is a no-op; none of the valid
  key values (0xCCCC/0x5555/0xAAAA) appear in the image, and there are no WWDG
  references.
- Fault handlers are dead loops (HardFault at 0x917: `nop; b .`, BusFault at
  0x7FA: `b .`). A crash hangs the board until power is cycled.
- No reset-cause register is ever read; startup never distinguishes reset
  sources.
- Explicit reset paths: 0x35B0 (SYSRESETREQ after a 100 ms delay), 0x3B20
  (bootloader mailbox write at 0x08002C00, then reset; upgrade/reboot path),
  and 0x6354 (bare reset wrapper, wired as binary command id 0x11).
- Measured boot time after power-on: ~52 s until the first serial answer.

## M850 A/B/C

Handler 0x424C parses A/B/C and writes GPIO levels on PB6/PB7/PB8 (0x2A60,
0x2A7E, 0x2A18 via 0x31FC, BSRR/BRR). No power cycle, no reset, no flash.
PB8 doubles as the link-status output driven by the supervisor.

## Dispatch-table offset discrepancy

Two flash dispatch-table lookups are 4 bytes off from their arrays
(`{u8 id, u8 arg, u16 pad, u32 handler}`, file offset 0x6BA8):

- 0x25C4 uses 0x9B84; the array starts at 0x9B88.
- 0x6318 uses 0x9BAC; its entries start at 0x9BA8.

Consequence: command ids 0x10/0x11 (including the reset handler at 0x6354) are
unreachable by their real ids, and the low bytes of the handler pointers
(0xD5, 0xE5, 0x55, 0xFB, 0x35) act as ids instead. A framed command with
byte1=0xE5 would spuriously trigger the reset handler; 0xD5 would jump to
address 0x10 (hang). The RAM dispatch table used by the normal M-code path
(0x200001AC) is aligned and unaffected.

## Link handshake

The main board sets its link-live byte (0x200039B1) on the first qualifying
laser frame via 0x26374, called from the M8xx forwarder (0x308D4). The module's
counterpart is the flag plus PB8 pair driven by the supervisor. The first frame
after module boot is the raw line ` Stepper_id:0x%x` (sent by init 0x2AB8).
The 7-byte binary keepalive (0x1AE4) exists but is dormant in this build.

## Verdict on the silent-window episodes

- App watchdog or crash reboots: ruled out (no watchdog, hangs do not reset,
  no reset-cause handling).
- The 3-4 s quiet windows fit a cooperative main-loop stall; the M898 aging
  command is the known multi-second blocker, but the machine never sends it in
  normal operation, so the internal trigger remains open.
- Repeated ~50 s outages match a full module reboot (boot time measured at
  ~52 s), which only an explicit reset path, a power glitch, or the
  dispatch-table defect can produce.
- The dispatch-table discrepancy is the one clear firmware bug found in this
  image.
