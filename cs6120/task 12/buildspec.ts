import { readStdin } from "../../bril-ts/util.ts";
import * as bril from "../../bril-ts/bril.ts";

type Instr = bril.Instruction;
type Func = bril.Function;
type Program = bril.Program;


function optimizeTrace(instrs: Instr[]): Instr[] {
  const env = new Map<string, bigint | number>(); // var -> constant
  const used = new Set<string>();
  const out: Instr[] = [];

  // First pass: constant folding
  for (const instr of instrs) {
    if (!("op" in instr)) {
     
      continue;
    }

    if (instr.op === "const" && instr.dest !== undefined) {
      env.set(instr.dest, instr.value as any);
      out.push(instr);
      continue;
    }

    if (
      (instr.op === "add" || instr.op === "sub" || instr.op === "mul") &&
      instr.args &&
      instr.dest
    ) {
      const [x, y] = instr.args;
      const vx = env.get(x);
      const vy = env.get(y);

      if (vx !== undefined && vy !== undefined) {
        let val: number;
        if (instr.op === "add") val = Number(vx) + Number(vy);
        else if (instr.op === "sub") val = Number(vx) - Number(vy);
        else val = Number(vx) * Number(vy);

        // fold into const
        out.push({
          op: "const",
          dest: instr.dest,
          type: instr.type ?? "int",
          value: val,
        } as bril.ValueOperation);
        env.set(instr.dest, val);
        continue;
      }
    }

    out.push(instr);
  }

  // Second pass: collect used variables
  for (const instr of out) {
    if ("op" in instr && instr.args) {
      for (const a of instr.args) {
        used.add(a);
      }
    }
  }

  // Third pass: DCE – drop assignments whose dest is never used
  return out.filter((instr) => {
    if ("op" in instr && "dest" in instr && instr.dest) {
      if (!used.has(instr.dest)) {
        if (instr.op === "const" || instr.op === "add" || instr.op === "sub" || instr.op === "mul") {
          return false;
        }
      }
    }
    return true;
  });
}


function traceToGuarded(trace: Instr[], bailLabel: string): Instr[] {
  const out: Instr[] = [];

  for (const instr of trace) {
    if (!("op" in instr)) continue;

    if (instr.op === "jmp") {
      continue;
    }

    if (instr.op === "br") {
      const condVar = instr.args?.[0];
      if (!condVar) {
        throw new Error("br in trace without condition arg");
      }
      const guardInstr: bril.Operation = {
        op: "guard",
        args: [condVar],
        labels: [bailLabel],
      };
      out.push(guardInstr);
      continue;
    }

    out.push(instr);
  }

  return out;
}

async function main() {
  const tracePath = Deno.args[0];
  if (!tracePath) {
    console.error("usage: deno run buildspec.ts trace.json < orig.json\n       or: deno run buildspec.ts trace.json prog.json");
    Deno.exit(1);
  }

  let prog: Program;
  if (Deno.args.length >= 2) {
    const progPath = Deno.args[1];
    const rawProg = await Deno.readTextFile(progPath);
    prog = JSON.parse(rawProg) as Program;
  } else {
    prog = JSON.parse(await readStdin()) as Program;
  }

  const raw = await Deno.readTextFile(tracePath);
  const lines = raw
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l.length > 0);

  if (lines.length === 0) {
    console.error(
      `Trace file ${tracePath} is empty or contains only whitespace. Did you generate it?`,
    );
    Deno.exit(2);
  }

  // Each non-empty line of trace.json is a JSON instruction
  let rawTrace: Instr[];
  try {
    rawTrace = lines.map((line) => JSON.parse(line));
  } catch (e) {
    console.error(`Failed to parse trace file ${tracePath}:`, e);
    Deno.exit(2);
  }

  const bailLabel = ".bail";
  const optimized = optimizeTrace(rawTrace);

  // Build a map of constants observed in the trace: var -> value
  const constMap = new Map<string, any>();
  for (const ins of optimized) {
    if ("op" in ins && ins.op === "const" && "dest" in ins && ins.dest) {
      constMap.set(ins.dest, (ins as any).value);
    }
  }

  const guarded: Instr[] = [];
  const postCommitPrints: Instr[] = [];
  let synthCounter = 0;
  for (const ins of optimized) {
    if (!("op" in ins)) continue;
    if (ins.op === "jmp") {
      // drop jmps in trace
      continue;
    }
    if (ins.op === "call" || ins.op === "ret") {
      const falseName = `_spec_false_${synthCounter++}`;
      guarded.push({ op: "const", dest: falseName, type: "bool", value: false } as any);
      guarded.push({ op: "guard", args: [falseName], labels: [bailLabel] } as bril.Operation);
      break;
    }
    if (ins.op === "br") {
      const condVar = ins.args?.[0];
      if (!condVar) throw new Error("br in trace without condition arg");
      guarded.push({ op: "guard", args: [condVar], labels: [bailLabel] } as bril.Operation);
      continue;
    }

    if (ins.op === "print") {
      // For each printed argument, if we observed a constant value in the
      // trace, synthesize an equality check and guard on it. Otherwise, force
      // a bail (conservative).
      const args = ins.args || [];
      let willAlwaysBail = false;
      for (const a of args) {
        if (constMap.has(a)) {
          const val = constMap.get(a);
          const constName = `_spec_exp_${synthCounter++}`;
          guarded.push({ op: "const", dest: constName, type: typeof val === "boolean" ? "bool" : "int", value: val } as any);
          const cmpName = `_spec_cmp_${synthCounter++}`;
          const cmpOp = (typeof val === "string") ? "ceq" : "eq";
          guarded.push({ op: cmpOp as any, dest: cmpName, args: [a, constName] } as any);
          guarded.push({ op: "guard", args: [cmpName], labels: [bailLabel] } as bril.Operation);
        } else {
          // Unknown printed value: be conservative and bail.
          const falseName = `_spec_false_${synthCounter++}`;
          guarded.push({ op: "const", dest: falseName, type: "bool", value: false } as any);
          guarded.push({ op: "guard", args: [falseName], labels: [bailLabel] } as bril.Operation);
          willAlwaysBail = true;
        }
      }
      if (!willAlwaysBail) postCommitPrints.push(ins as Instr);
      continue;
    }

    // Default: keep instruction.
    guarded.push(ins);
  }

  const mainIdx = prog.functions.findIndex((f) => f.name === "main");
  if (mainIdx === -1) {
    console.error("No main function found");
    Deno.exit(1);
  }
  const origMain = prog.functions[mainIdx];

  const fallbackBody = origMain.instrs; // original main body

  const newInstrs: Instr[] = [];

  // speculative fast path
  newInstrs.push({ op: "speculate" } as bril.Operation);
  newInstrs.push(...guarded);

  let hasGuard = guarded.some((ins: any) => "op" in ins && ins.op === "guard");

  if (!hasGuard) {
    let boolVar: string | null = null;
    for (let i = guarded.length - 1; i >= 0; --i) {
      const ins = guarded[i] as any;
      if (
        ins &&
        typeof ins === "object" &&
        "dest" in ins &&
        ins.dest &&
        ins.type === "bool"
      ) {
        boolVar = ins.dest;
        break;
      }
    }

    if (boolVar) {
      newInstrs.push({
        op: "guard",
        args: [boolVar],
        labels: [bailLabel],
      } as bril.Operation);
    } else {
      const guardVar = "_spec_true_guard";
      newInstrs.push({
        op: "const",
        dest: guardVar,
        type: "bool",
        value: true,
      } as any);
      newInstrs.push({
        op: "guard",
        args: [guardVar],
        labels: [bailLabel],
      } as bril.Operation);
    }
  }

  newInstrs.push({ op: "commit" } as bril.Operation);
  for (const p of postCommitPrints) {
    newInstrs.push(p as any);
  }
  newInstrs.push({ op: "ret" } as bril.Operation);

  // bailout path (guards will abort here)
  newInstrs.push({ label: bailLabel } as any);
  const fallbackInstrs = [...(fallbackBody as any)];
  if (
    !(
      fallbackInstrs.length &&
      "op" in fallbackInstrs[fallbackInstrs.length - 1] &&
      (fallbackInstrs[fallbackInstrs.length - 1] as any).op === "ret"
    )
  ) {
    fallbackInstrs.push({ op: "ret" } as bril.Operation);
  }
  newInstrs.push(...(fallbackInstrs as any));

  const newMain: Func = {
    ...origMain,
    instrs: newInstrs,
  };

  const newFuncs = [...prog.functions];
  newFuncs[mainIdx] = newMain;
  prog.functions = newFuncs;

  function validateBrilProgram(prog: Program): void {
    if (!Array.isArray(prog.functions)) {
      throw new Error("Invalid Bril program: 'functions' must be an array.");
    }
    for (const func of prog.functions) {
      if (typeof func.name !== "string" || !Array.isArray(func.instrs)) {
        throw new Error(`Invalid function in Bril program: ${JSON.stringify(func)}`);
      }
      for (const instr of func.instrs) {
        if (!("op" in instr) && !("label" in instr)) {
          throw new Error(`Invalid instruction in Bril program: ${JSON.stringify(instr)}`);
        }
      }
    }
  }

  validateBrilProgram(prog);
  function brilToText(prog: Program): string {
    let text = "";
    for (const func of prog.functions) {
      let header = `@${func.name}`;
      if (func.args && func.args.length) {
        const argList = func.args.map((a: any) => `${a.name}: ${a.type}`).join(", ");
        header += `(${argList})`;
      }
      if ((func as any).type) {
        header += `:${(func as any).type}`;
      }
      text += header + ` {\n`;
      for (const instr of func.instrs) {
        if ("label" in instr) {
          const lab = String((instr as any).label);
          const outLab = lab.startsWith(".") ? lab : `.${lab}`;
          text += `${outLab}:\n`;
          text += `  nop;\n`; 
        } else {
          const dest = "dest" in instr && instr.dest ? `${instr.dest}: ${instr.type} = ` : "";
          const op = instr.op;
          let body = "";

          if (op === "call") {
            const funcName = "funcs" in instr && instr.funcs && instr.funcs.length ? `@${instr.funcs[0]}` : "";
            const args = "args" in instr && instr.args ? instr.args.join(" ") : "";
            body = (funcName ? ` ${funcName}` : "") + (args ? ` ${args}` : "");
          } else if (op === "ret") {
            const args = "args" in instr && instr.args && instr.args.length ? ` ${instr.args.join(" ")}` : "";
            body = args;
          } else {
            const argsStr = "args" in instr && instr.args && instr.args.length ? instr.args.join(" ") : "";
            const value = op === "const" && "value" in instr ? String((instr as any).value) : "";
            const labelsStr = "labels" in instr && instr.labels && instr.labels.length
              ? instr.labels.map((l: string) => (l.startsWith(".") ? l : `.${l}`)).join(" ")
              : "";
            const parts: string[] = [];
            if (argsStr) parts.push(argsStr);
            if (value) parts.push(value);
            if (labelsStr) parts.push(labelsStr);
            body = parts.length ? " " + parts.join(" ") : "";
          }

          text += `  ${dest}${instr.op}${body};\n`;
        }
      }
      text += `}\n\n`;
    }
    return text;
  }

  const brilText = brilToText(prog);
  console.log(brilText);
}

main();