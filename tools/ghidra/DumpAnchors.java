// Post-script: dump strings, resolve anchor references, and decompile the
// containing functions.
//
// Args: <outputDir> <anchorFile>
//   outputDir  - directory for strings.txt, index.txt, func_XXXXXXXX.c
//   anchorFile - one anchor substring per line; '#' comments ignored
//
// The index maps each anchor occurrence to its string address, the referencing
// instruction, and the owning function, so every finding in protocol-notes.md
// can cite a reproducible address.

import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.listing.Data;
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

public class DumpAnchors extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length < 2) {
            println("DumpAnchors: usage <outputDir> <anchorFile>");
            return;
        }
        File out = new File(args[0]);
        if (!out.mkdirs() && !out.isDirectory()) {
            println("DumpAnchors: cannot create " + out);
            return;
        }

        // Collect every defined string once; anchor matching scans this list.
        List<Data> stringData = new ArrayList<>();
        PrintWriter strings = new PrintWriter(new File(out, "strings.txt"), "UTF-8");
        for (Data d : currentProgram.getListing().getDefinedData(true)) {
            Object v = d.getValue();
            if (v instanceof String) {
                String s = (String) v;
                strings.println(d.getAddress() + "\t" + s.replace("\n", "\\n"));
                stringData.add(d);
            }
        }
        strings.close();

        List<String> anchors = new ArrayList<>();
        try (BufferedReader br = new BufferedReader(new FileReader(args[1]))) {
            String line;
            while ((line = br.readLine()) != null) {
                line = line.trim();
                if (!line.isEmpty() && !line.startsWith("#")) {
                    anchors.add(line);
                }
            }
        }

        // Report analysis coverage so a thin result is visible rather than silent.
        int funcCount = currentProgram.getFunctionManager().getFunctionCount();
        int instrCount = 0;
        for (ghidra.program.model.listing.InstructionIterator it =
                currentProgram.getListing().getInstructions(true); it.hasNext(); it.next()) {
            instrCount++;
        }
        println("DumpAnchors: functions=" + funcCount + " instructions=" + instrCount
                + " strings=" + stringData.size());

        DecompInterface di = new DecompInterface();
        di.openProgram(currentProgram);

        PrintWriter index = new PrintWriter(new File(out, "index.txt"), "UTF-8");
        index.println("anchor\tstringAddr\trefAddr\tfuncAddr\tfuncName\tfile");

        Set<Long> dumped = new HashSet<>();
        for (String anchor : anchors) {
            boolean found = false;
            for (Data d : stringData) {
                String s = (String) d.getValue();
                if (!s.contains(anchor)) {
                    continue;
                }
                found = true;
                Reference[] refs = getReferencesTo(d.getAddress());
                if (refs.length == 0) {
                    index.println(anchor + "\t" + d.getAddress() + "\t-\t-\t-\t-");
                    continue;
                }
                for (Reference r : refs) {
                    Function f = getFunctionContaining(r.getFromAddress());
                    if (f == null) {
                        index.println(anchor + "\t" + d.getAddress() + "\t"
                                + r.getFromAddress() + "\t-\t-\t-");
                        continue;
                    }
                    long fa = f.getEntryPoint().getOffset();
                    String fname = String.format("func_%08X.c", fa);

                    // Decompile each function once even if several anchors land in it.
                    if (dumped.add(fa)) {
                        DecompileResults res = di.decompileFunction(f, 60, monitor);
                        PrintWriter pw = new PrintWriter(new File(out, fname), "UTF-8");
                        pw.println("// " + f.getName() + " @ " + f.getEntryPoint());
                        if (res.decompileCompleted()) {
                            pw.println(res.getDecompiledFunction().getC());
                        } else {
                            pw.println("// decompile failed: " + res.getErrorMessage());
                        }
                        pw.close();
                    }
                    index.println(anchor + "\t" + d.getAddress() + "\t" + r.getFromAddress()
                            + "\t" + f.getEntryPoint() + "\t" + f.getName() + "\t" + fname);
                }
            }
            if (!found) {
                index.println(anchor + "\tNOT-FOUND\t-\t-\t-\t-");
            }
        }
        index.close();
        println("DumpAnchors: wrote " + out.getAbsolutePath()
                + " (" + stringData.size() + " strings, " + anchors.size() + " anchors)");
    }
}
