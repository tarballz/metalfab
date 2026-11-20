// Post-script: for each function in a focus list, list every defined string it
// references and write one decompiled file per function.
//
// Args: <outputDir> <addressFile>
//
// Output handlers.tsv: funcAddr, stringAddr, stringText. Because each command
// handler echoes its own "M### ..." format string, this table reconstructs the
// command set from the dispatcher's callees without guessing.

import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.symbol.Reference;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.io.PrintWriter;
import java.util.ArrayList;
import java.util.List;

public class DumpHandlers extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length < 2) {
            println("DumpHandlers: usage <outputDir> <addressFile>");
            return;
        }
        File out = new File(args[0]);
        out.mkdirs();

        List<Long> targets = new ArrayList<>();
        try (BufferedReader br = new BufferedReader(new FileReader(args[1]))) {
            String line;
            while ((line = br.readLine()) != null) {
                int hash = line.indexOf('#');
                if (hash >= 0) {
                    line = line.substring(0, hash);
                }
                line = line.trim();
                if (!line.isEmpty()) {
                    targets.add(Long.parseLong(line.replace("0x", ""), 16));
                }
            }
        }

        DecompInterface di = new DecompInterface();
        di.openProgram(currentProgram);

        PrintWriter tsv = new PrintWriter(new File(out, "handlers.tsv"), "UTF-8");
        tsv.println("funcAddr\tstringAddr\tstringText");

        for (long target : targets) {
            Address addr = toAddr(target);
            Function f = getFunctionContaining(addr);
            if (f == null) {
                tsv.println(String.format("%08X", target) + "\t-\tNO-FUNCTION");
                continue;
            }

            // Walk the body: every instruction that references a defined string
            // contributes a row. A handler's own echo string is always here.
            for (Instruction ins = getInstructionAt(f.getBody().getMinAddress());
                    ins != null && f.getBody().contains(ins.getAddress());
                    ins = ins.getNext()) {
                for (Reference r : ins.getReferencesFrom()) {
                    Data d = getDataAt(r.getToAddress());
                    if (d != null && d.getValue() instanceof String) {
                        String s = (String) d.getValue();
                        tsv.println(String.format("%08X", f.getEntryPoint().getOffset())
                                + "\t" + d.getAddress() + "\t" + s.replace("\n", "\\n"));
                    }
                }
            }

            String fname = String.format("handler_%08X.c", f.getEntryPoint().getOffset());
            DecompileResults res = di.decompileFunction(f, 60, monitor);
            PrintWriter pw = new PrintWriter(new File(out, fname), "UTF-8");
            pw.println("// handler " + f.getName() + " @ " + f.getEntryPoint());
            if (res.decompileCompleted()) {
                pw.println(res.getDecompiledFunction().getC());
            } else {
                pw.println("// decompile failed: " + res.getErrorMessage());
            }
            pw.close();
        }
        tsv.close();
        println("DumpHandlers: wrote " + out.getAbsolutePath());
    }
}
