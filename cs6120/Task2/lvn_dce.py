
#!/usr/bin/env python3
import sys, json
from copy import deepcopy

EFFECT_OPS = {"print", "br", "jmp", "ret", "call", "store", "free"}

def is_pure(ins):
    return "op" in ins and "dest" in ins and ins["op"] not in EFFECT_OPS

def get_used_vars(ins):
    vars = []
    if "args" in ins:
        vars.extend(ins["args"])
    if ins.get("op") == "br" and "labels" in ins and ins.get("args"):
        vars.append(ins["args"][0])  # condition for br
    if "value" in ins and isinstance(ins["value"], str):
        vars.append(ins["value"])
    return vars


def split_blocks(instrs):
    labels = {}
    leaders = set()
    for i, ins in enumerate(instrs):
        if "label" in ins:
            labels[ins["label"]] = i
    if instrs:
        leaders.add(0)
    for i, ins in enumerate(instrs):
        if ins.get("op") in ("jmp", "br"):
            for l in ins.get("labels", []):
                if l in labels:
                    leaders.add(labels[l])
            if i + 1 < len(instrs):
                leaders.add(i + 1)
        if "label" in ins:
            leaders.add(i)
    leaders = sorted(leaders)
    blocks = []
    for i in range(len(leaders)):
        start = leaders[i]
        end = leaders[i+1] if i+1 < len(leaders) else len(instrs)
        blocks.append(instrs[start:end])
    return blocks

def join_blocks(blocks):
    return [ins for block in blocks for ins in block]
def find_globals_used_elsewhere(blocks):
    defs, uses = {}, {}

    # Track all uses and defs per block
    for bi, block in enumerate(blocks):
        for ins in block:
            for var in get_used_vars(ins):
                uses.setdefault(var, set()).add(bi)
            if "dest" in ins:
                defs.setdefault(ins["dest"], set()).add(bi)

    global_needed = set()

    # Keep all variables that are used in blocks where they weren't defined
    for var in uses:
        if var in defs:
            if not uses[var].issubset(defs[var]):
                global_needed.add(var)

    # Dests defined in multiple blocks — might be SSA-renamed but reused
    for var, def_blocks in defs.items():
        if len(def_blocks) > 1:
            global_needed.add(var)

    # Always preserve all arguments to control-flow and side-effectful ops
    for block in blocks:
        for ins in block:
            if ins.get("op") in {"print", "ret", "call", "store", "br", "jmp"}:
                global_needed.update(get_used_vars(ins))

    # Preserve any variable that's used but never defined (live-in)
    for var in uses:
        if var not in defs:
            global_needed.add(var)

    return global_needed



def dce_block(block, global_uses):
    live, seen_defs = set(), set()
    new_block = []
    for ins in reversed(block):
        if "label" in ins or "comment" in ins:
            new_block.append(ins)
            continue

        dest = ins.get("dest")
        used_vars = set(get_used_vars(ins))

        # Always keep side-effectful ops
        if not is_pure(ins):
            new_block.append(ins)
            live.update(used_vars)
            continue

        if dest in global_uses or dest in live:
            new_block.append(ins)
            live.update(used_vars)
            live.discard(dest)
        elif dest in seen_defs:
            new_block.append(ins)
            live.update(used_vars)
            live.discard(dest)
        else:
            seen_defs.add(dest)

    new_block.reverse()
    return new_block


def trivial_dce_func(func):
    changed = True
    while changed:
        changed = False
        blocks = split_blocks(func["instrs"])
        global_uses = find_globals_used_elsewhere(blocks)
        new_blocks = []
        for block in blocks:
            new_block = dce_block(block, global_uses)
            if len(new_block) < len(block):
                changed = True
            new_blocks.append(new_block)
        func["instrs"] = join_blocks(new_blocks)
    return func

def lvn_block(block):
    expr_table = {}
    var2valnum = {}
    valnum2var = {}
    var_subst = {}
    new_block = []
    valnum_counter = [0]

    def next_valnum():
        v = valnum_counter[0]
        valnum_counter[0] += 1
        return v

    def canonicalize_var(v):
        while v in var_subst:
            v = var_subst[v]
        return v

    def try_constant_folding(op, args):
        try:
            const_args = [int(arg) for arg in args]
        except ValueError:
            return None
        if len(const_args) == 1:
            a = const_args[0]
            if op == "not": return int(not a)
            return None
        a, b = const_args
        if op == "add": return a + b
        if op == "mul": return a * b
        if op == "sub": return a - b
        if op == "div": return a // b if b != 0 else 0
        if op == "eq": return int(a == b)
        if op == "lt": return int(a < b)
        if op == "le": return int(a <= b)
        if op == "gt": return int(a > b)
        if op == "ge": return int(a >= b)
        if op == "and": return int(a and b)
        if op == "or": return int(a or b)
        return None

    def canonicalize_op(op, args):
        if op in {"add", "mul", "eq", "and", "or"}:
            return tuple(sorted(args))
        return tuple(args)

    for ins in block:
        if "label" in ins or "comment" in ins:
            new_block.append(ins)
            continue

        if "args" in ins:
            ins["args"] = [canonicalize_var(arg) for arg in ins["args"]]

        if ins.get("op") == "id":
            src = ins["args"][0]
            var_subst[ins["dest"]] = src
            valnum = var2valnum.get(src, next_valnum())
            var2valnum[ins["dest"]] = valnum
            valnum2var[valnum] = src
            ins["args"] = [src]
            new_block.append(ins)
            continue

        if ins.get("op") == "const":
            val = ins["value"]
            sig = ("const", val)
            if sig in expr_table:
                var_subst[ins["dest"]] = expr_table[sig][1]
                var2valnum[ins["dest"]] = expr_table[sig][0]
            else:
                valnum = next_valnum()
                expr_table[sig] = (valnum, ins["dest"])
                var2valnum[ins["dest"]] = valnum
                valnum2var[valnum] = ins["dest"]
                new_block.append(ins)
            continue

        if is_pure(ins):
            args = ins["args"]
            if all(arg.isdigit() or (arg.startswith('-') and arg[1:].isdigit()) for arg in args):
                folded = try_constant_folding(ins["op"], args)
                if folded is not None:
                    sig = ("const", folded)
                    if sig in expr_table:
                        var_subst[ins["dest"]] = expr_table[sig][1]
                        var2valnum[ins["dest"]] = expr_table[sig][0]
                    else:
                        valnum = next_valnum()
                        expr_table[sig] = (valnum, ins["dest"])
                        var2valnum[ins["dest"]] = valnum
                        valnum2var[valnum] = ins["dest"]
                        new_block.append({
                            "op": "const",
                            "value": folded,
                            "type": ins["type"],
                            "dest": ins["dest"]
                        })
                    continue

            sig = (ins["op"], canonicalize_op(ins["op"], args))
            if sig in expr_table:
                varnum, canon = expr_table[sig]
                var_subst[ins["dest"]] = canon
                var2valnum[ins["dest"]] = varnum
                continue
            else:
                valnum = next_valnum()
                expr_table[sig] = (valnum, ins["dest"])
                var2valnum[ins["dest"]] = valnum
                valnum2var[valnum] = ins["dest"]
                new_block.append(ins)
            continue

        if "dest" in ins:
            valnum = next_valnum()
            var2valnum[ins["dest"]] = valnum
            valnum2var[valnum] = ins["dest"]

        new_block.append(ins)

    return new_block

def lvn_func(func):
    blocks = split_blocks(func["instrs"])
    new_blocks = []
    for block in blocks:
        new_blocks.append(lvn_block(block))
    func["instrs"] = join_blocks(new_blocks)
    return trivial_dce_func(func)

def main():
    prog = json.load(sys.stdin)
    for func in prog["functions"]:
        lvn_func(func)
    json.dump(prog, sys.stdout, indent=2)

if __name__ == "__main__":
    main()
