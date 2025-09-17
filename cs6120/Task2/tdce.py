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

    # Track uses and defs per block
    for bi, block in enumerate(blocks):
        for ins in block:
            for var in get_used_vars(ins):
                uses.setdefault(var, set()).add(bi)
            if "dest" in ins:
                defs.setdefault(ins["dest"], set()).add(bi)

    global_needed = set()

    # Used in a different block than it was defined
    for var in uses:
        if var in defs:
            if not uses[var].issubset(defs[var]):
                global_needed.add(var)

    # Defined in multiple blocks
    for var, def_blocks in defs.items():
        if len(def_blocks) > 1:
            global_needed.add(var)

    # Used but never defined (e.g. function parameters)
    for var in uses:
        if var not in defs:
            global_needed.add(var)

    # Used in side-effectful or control-flow ops
    for block in blocks:
        for ins in block:
            if ins.get("op") in EFFECT_OPS:
                global_needed.update(get_used_vars(ins))

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

        if not is_pure(ins):  # side-effectful op
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

def main():
    prog = json.load(sys.stdin)
    for func in prog["functions"]:
        trivial_dce_func(func)
    json.dump(prog, sys.stdout, indent=2)

if __name__ == "__main__":
    main()
