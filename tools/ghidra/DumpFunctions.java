// Post-script: decompile a focus list of functions and their callers.
//
// Args: <outputDir> <addressFile>
//   addressFile - one hex function/target address per line; '#' comments ignored
//
// Use this to walk upward from a known handler to the dispatch site: pass the
// handler entry address, and every caller is decompiled alongside it.

import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.Reference;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.io.PrintWriter;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

public class DumpFunctions extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length < 2) {
            println("DumpFunctions: usage <outputDir> <addressFile>");
            return;
        }
        File out = new File(args[0]);
        if (!out.mkdirs() && !out.isDirectory()) {
            println("DumpFunctions: cannot create " + out);
            return;
        }

        List<Long> targets = new ArrayList<>();
        try (BufferedReader br = new BufferedReader(new FileReader(args[1]))) {
            String line;
            while ((line = br.readLine()) != null) {
                // Strip inline comments so entries can be annotated in place.
                int hash = line.indexOf('#');
                if (hash >= 0) {
                    line = line.substring(0, hash);
                }
                line = line.trim();
                if (line.isEmpty()) {
                    continue;
                }
                targets.add(Long.parseLong(line.replace("0x", "").replace("0X", ""), 16));
            }
        }

        DecompInterface di = new DecompInterface();
        di.openProgram(currentProgram);

        PrintWriter index = new PrintWriter(new File(out, "focus-index.txt"), "UTF-8");
        index.println("target\tkind\tfuncAddr\tfuncName\tfile");

        Set<Long> done = new HashSet<>();
        for (long target : targets) {
            Address addr = toAddr(target);
            Function f = getFunctionContaining(addr);
            if (f == null) {
                index.println(String.format("%08X", target) + "\tNO-FUNCTION\t-\t-\t-");
                continue;
            }
            dumpFunction(di, out, index, String.format("%08X", target), "target", f, done);

            // The dispatch site is almost always a caller of the handler.
            for (Reference r : getReferencesTo(f.getEntryPoint())) {
                Function caller = getFunctionContaining(r.getFromAddress());
                if (caller != null) {
                    dumpFunction(di, out, index, String.format("%08X", target),
                            "caller", caller, done);
                }
            }
        }
        index.close();
        println("DumpFunctions: wrote " + out.getAbsolutePath() + " (" + done.size() + " functions)");
    }

    private void dumpFunction(DecompInterface di, File out, PrintWriter index,
            String target, String kind, Function f, Set<Long> done) throws Exception {
        long fa = f.getEntryPoint().getOffset();
        String fname = String.format("%s_%08X.c", kind, fa);
        if (!done.add(fa)) {
            return;
        }
        DecompileResults res = di.decompileFunction(f, 60, monitor);
        PrintWriter pw = new PrintWriter(new File(out, fname), "UTF-8");
        pw.println("// " + kind + " of " + target + ": " + f.getName() + " @ " + f.getEntryPoint());
        if (res.decompileCompleted()) {
            pw.println(res.getDecompiledFunction().getC());
        } else {
            pw.println("// decompile failed: " + res.getErrorMessage());
        }
        pw.close();
        index.println(target + "\t" + kind + "\t" + f.getEntryPoint() + "\t"
                + f.getName() + "\t" + fname);
    }
}
