// Pre-script: seed functions from the Cortex-M vector table before auto-analysis.
// Raw binaries have no entry point, so Ghidra would otherwise leave the image as
// undefined bytes. Every odd word in the first 64 slots that falls inside the
// loaded image is a Thumb handler address; disassemble and create a function there
// so the analyzer can follow calls outward.

import ghidra.app.script.GhidraScript;
import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.app.cmd.function.CreateFunctionCmd;
import ghidra.program.model.address.Address;
import ghidra.program.model.mem.Memory;
import ghidra.program.model.mem.MemoryBlock;

import java.util.ArrayList;
import java.util.List;

public class Bootstrap extends GhidraScript {
    @Override
    public void run() throws Exception {
        // The headless -loader-baseAddr option did not take effect for this
        // BinaryLoader, so move the loaded block explicitly before analysis.
        // Absolute pointers inside the firmware are 0x0800xxxx, and they only
        // resolve if the bytes live at that base.
        long desiredBase = 0x08000000L;
        Memory mem = currentProgram.getMemory();
        if (currentProgram.getImageBase().getOffset() != desiredBase) {
            int tx = currentProgram.startTransaction("relocate firmware block");
            try {
                List<MemoryBlock> toMove = new ArrayList<>();
                for (MemoryBlock b : mem.getBlocks()) {
                    if (b.getStart().getOffset() < desiredBase) {
                        toMove.add(b);
                    }
                }
                for (MemoryBlock b : toMove) {
                    byte[] data = new byte[(int) b.getSize()];
                    mem.getBytes(b.getStart(), data);
                    mem.removeBlock(b, monitor);
                    mem.createInitializedBlock("firmware", toAddr(desiredBase),
                            data.length, (byte) 0, monitor, false);
                    mem.setBytes(toAddr(desiredBase), data);
                }
                // Do not call setImageBase here: it shifts blocks by the delta and
                // would move the freshly created block off 0x08000000. Absolute
                // references resolve from block addresses, which is what matters.
            } finally {
                currentProgram.endTransaction(tx, true);
            }
            for (MemoryBlock b : mem.getBlocks()) {
                println("Bootstrap: block " + b.getName() + " " + b.getStart() + "-" + b.getEnd());
            }
        }

        Address base = mem.getMinAddress();
        long min = base.getOffset();
        long max = min + mem.getSize();

        println(String.format("Bootstrap: imageBase=%s memSize=0x%X", base, mem.getSize()));
        for (int i = 0; i < 6; i++) {
            try {
                println(String.format("  vec[%d] @%s = 0x%08X",
                        i, base.add(i * 4L), mem.getInt(base.add(i * 4L))));
            } catch (Exception e) {
                println("  vec[" + i + "] read failed: " + e);
            }
        }

        int created = 0;
        for (int i = 0; i < 64; i++) {
            Address slot = base.add(i * 4L);
            long v;
            try {
                // Little-endian vector table entry.
                v = mem.getInt(slot) & 0xFFFFFFFFL;
            } catch (Exception e) {
                // Slot outside the loaded block; stop scanning.
                break;
            }
            // Handler pointers are Thumb addresses: odd, inside the image.
            if ((v & 1) == 0 || v < min || v >= max) {
                continue;
            }
            Address fn = toAddr(v & ~1L);

            DisassembleCommand dc = new DisassembleCommand(fn, null, true);
            dc.applyTo(currentProgram, monitor);
            CreateFunctionCmd fc = new CreateFunctionCmd(fn);
            fc.applyTo(currentProgram, monitor);
            created++;
        }
        println("Bootstrap: seeded " + created + " functions from the vector table");

        // Second pass: Thumb function prologues. PUSH with LR is encoded as 0xB5xx
        // (high byte 0xB5). Scanning halfwords catches functions that are only
        // reachable through dispatch tables, which recursive descent misses.
        int prologues = 0;
        for (long off = 0x100; off + 2 <= mem.getSize(); off += 2) {
            Address a = base.add(off);
            int hw;
            try {
                hw = mem.getShort(a) & 0xFFFF;
            } catch (Exception e) {
                break;
            }
            // Thumb prologue forms: PUSH (0xB4xx), PUSH with LR (0xB5xx), PUSH.W (0xE92D).
            int hi = hw & 0xFF00;
            if (hi == 0xB400 || hi == 0xB500 || hw == 0xE92D) {
                DisassembleCommand dc = new DisassembleCommand(a, null, true);
                dc.applyTo(currentProgram, monitor);
                CreateFunctionCmd fc = new CreateFunctionCmd(a);
                fc.applyTo(currentProgram, monitor);
                prologues++;
            }
        }
        println("Bootstrap: seeded " + prologues + " functions from Thumb prologues");
    }
}
