#!/usr/bin/env python3
import sys, json
from copy import deepcopy

EFFECT_OPS = {"print", "br", "jmp", "ret", "call", "store", "free"}

def is_pure(ins):
    return "op" in ins and "dest" in ins and ins["op"] not in EFFECT_OPS

def get_used_vars(ins):
    return ins.get("args", [])

def split_blocks(instrs):
    """
    Split instructions into basic blocks.
    Start a block at:
    - first instruction
    - label
    - targets of jmp/br
    - instruction after jmp/br
    """
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
    for i, ins in enumerate(instrs):
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
    """
    Determine which variables are used across blocks.
    If a var is used in a different block than it's defined in, it's globally needed.
    """
    defs = {}
    uses = {}
    for bi, block in enumerate(blocks):
        for ins in block:
            for var in get_used_vars(ins):
                uses.setdefault(var, set()).add(bi)
            if "dest" in ins:
                defs.setdefault(ins["dest"], set()).add(bi)
    global_needed = set()
    for var in uses:
        if var in defs:
            if not uses[var].issubset(defs[var]):
                global_needed.add(var)
    return global_needed.union(set(var for var in uses if var not in defs))

def dce_block(block, global_uses):
    live = set()
    new_block = []
    seen_defs = set()

    for ins in reversed(block):
        if "comment" in ins:
            new_block.append(ins)
            continue

        if "label" in ins:
            new_block.append(ins)
            continue

        if "dest" in ins and "type" in ins and is_pure(ins):
            dest = ins["dest"]

            # If it's not used in this block AND not used globally → remove
            if dest not in live and dest not in global_uses:
                continue

            # If it's redefined later in this block before being used → remove
            if dest not in live and dest in seen_defs:
                continue

            seen_defs.add(dest)
            live.discard(dest)
            new_block.append(ins)
        else:
            new_block.append(ins)

        for var in get_used_vars(ins):
            live.add(var)

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
