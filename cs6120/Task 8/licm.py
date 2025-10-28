#!/usr/bin/env python3
# licm.py — Loop-Invariant Code Motion for Bril (SSA)
#
# Usage:
#   python licm.py < in.json > out.json
#   python licm.py --in in.json --out out.json [--clean]
#
# Notes:
# - Assumes SSA-ish Bril (unique defs). We never hoist phi/effectful ops.
# - Treats 'div' as non-hoistable to avoid divide-by-zero exceptions.
# - Safe speculation for pure ops (no side effects/exceptions).
# - Creates a preheader when needed.
#
# (c) 2025 — provided for CS6120 HW use.

import sys, json, argparse, copy
from collections import defaultdict, deque

EFFECT_OPS = {"print","br","jmp","ret","call","store","free"}
# Consider pure & non-exception ops hoistable; treat 'div' as non-hoistable for safety.
PURE_OPS = {
    "const","id",
    "add","sub","mul",           # int arithmetic (no overflow trapping)
    "eq","lt","gt","le","ge",
    "not","and","or",
    # Add float variants if you use them and accept their semantics:
    # "fadd","fsub","fmul","feq","flt","fgt","fle","fge"
}
NON_HOISTABLE_EXTRA = {"phi","div"}  # be conservative
TERMINATORS = {"br","jmp","ret"}

# ---------------------------
# Helpers: Bril function model
# ---------------------------

def read_program(path):
    if path:
        with open(path, "r") as f:
            return json.load(f)
    return json.load(sys.stdin)

def write_program(p, path):
    if path:
        with open(path, "w") as f:
            json.dump(p, f, indent=2)
    else:
        json.dump(p, sys.stdout, indent=2)

def instr_uses(ins):
    uses = []
    op = ins.get("op")
    if "args" in ins and isinstance(ins["args"], list):
        uses.extend(ins["args"])
    # For "phi", Bril uses args paired with labels; already in "args".
    # For "br", condition is first arg (already included).
    if "value" in ins and isinstance(ins["value"], str):
        uses.append(ins["value"])
    return uses

def instr_def(ins):
    return ins.get("dest")

def is_terminator(ins):
    return ins.get("op") in TERMINATORS

def is_effectful(ins):
    op = ins.get("op")
    if not op:
        return False
    if op in EFFECT_OPS:
        return True
    return False

def is_hoistable_pure(ins):
    op = ins.get("op")
    if not op:
        return False
    if op in NON_HOISTABLE_EXTRA:
        return False
    if op in PURE_OPS and "dest" in ins:
        return True
    return False

# ---------------------------
# Build basic blocks
# ---------------------------

class Block:
    def __init__(self, name):
        self.name = name
        self.instrs = []   # list of instruction dicts
        self.preds = set()
        self.succs = set()

def split_blocks(func):
    """Split a Flat instr list into labeled basic blocks."""
    blocks = []
    cur = None
    name_counter = [0]

    def fresh():
        n = name_counter[0]; name_counter[0] += 1
        return f".L{n}"

    # First pass: create blocks on label or after terminator.
    for ins in func["instrs"]:
        if "label" in ins:
            # start a new block at each explicit label
            cur = Block(ins["label"])
            blocks.append(cur)
            cur.instrs.append(ins)
            continue
        if cur is None:
            cur = Block(fresh())
            blocks.append(cur)
        cur.instrs.append(ins)
        if is_terminator(ins):
            # force new block after terminator (unless next has label)
            cur = None

    # Ensure at least one block
    if not blocks:
        blocks = [Block("entry")]

    # Make sure each block has a label as first thing (Bril convention)
    for b in blocks:
        if not b.instrs or "label" not in b.instrs[0]:
            # prepend an explicit label
            b.instrs = [{"label": b.name}] + b.instrs

    return blocks

def build_cfg(blocks):
    label2idx = { b.instrs[0]["label"]: i for i,b in enumerate(blocks) }
    n = len(blocks)

    for i,b in enumerate(blocks):
        # find terminator
        term = None
        for ins in reversed(b.instrs):
            if "op" in ins:
                if is_terminator(ins):
                    term = ins
                break
        if term:
            if term["op"] == "br":
                # conditional branch has labels list
                for lab in term.get("labels", []):
                    j = label2idx[lab]
                    b.succs.add(j)
                    blocks[j].preds.add(i)
            elif term["op"] == "jmp":
                lab = term.get("labels",[None])[0]
                if lab is not None:
                    j = label2idx[lab]
                    b.succs.add(j)
                    blocks[j].preds.add(i)
            # ret: no fallthrough
        else:
            # fallthrough to next block if exists
            if i+1 < n:
                b.succs.add(i+1)
                blocks[i+1].preds.add(i)
    return label2idx

# ---------------------------
# Dominators
# ---------------------------

def compute_dominators(blocks, entry=0):
    n = len(blocks)
    dom = [ set(range(n)) for _ in range(n) ]
    dom[entry] = {entry}

    changed = True
    while changed:
        changed = False
        for b in range(n):
            if b == entry:
                continue
            new = set(range(n))
            if blocks[b].preds:
                for p in blocks[b].preds:
                    new &= dom[p]
            # include self
            new.add(b)
            if new != dom[b]:
                dom[b] = new
                changed = True
    # immediate dominator optional; for now we only need query:
    def dominates(a,b):
        return a in dom[b]
    return dom, dominates

# ---------------------------
# Loops: backedges & natural loops
# ---------------------------

def find_backedges(blocks, dominates):
    backs = []
    for u,bu in enumerate(blocks):
        for v in bu.succs:
            if dominates(v, u):
                backs.append((u,v))  # u -> v where v dominates u
    return backs

def natural_loop(blocks, u, h):
    """Return set of block indices in natural loop of backedge u->h."""
    loop = {h}
    work = [u]
    while work:
        x = work.pop()
        if x in loop:
            continue
        loop.add(x)
        for p in blocks[x].preds:
            if p not in loop:
                work.append(p)
    return loop

# ---------------------------
# Preheader creation
# ---------------------------

def ensure_preheader(blocks, header_idx, loop_set):
    """Ensure header has a unique predecessor outside the loop."""
    header = blocks[header_idx]
    outside_preds = [p for p in header.preds if p not in loop_set]
    # If exactly one outside pred and it has only this succ, we can reuse it.
    if len(outside_preds) == 1:
        p = outside_preds[0]
        # ok if p has unique succ == header
        if blocks[p].succs == {header_idx}:
            return p  # already a preheader

    # Create new empty preheader block
    new_name = f"{header.instrs[0]['label']}_preheader"
    pre = Block(new_name)
    pre.instrs = [{"label": new_name}]  # empty block with just label

    # Insert preheader just before header (position isn’t semantically important)
    insert_idx = header_idx
    blocks.insert(insert_idx, pre)

    # Rebuild indices and update preds/succs later; for now, remember we inserted.
    # We must rewire edges: all outside_preds should point to pre, pre -> header.
    # Because inserting shifts indices, do a full CFG rebuild after rewiring labels.
    return "NEEDS_REBUILD"

def rebuild_all(blocks):
    # Recompute preds/succs from scratch
    for b in blocks:
        b.preds.clear()
        b.succs.clear()
    label2idx = build_cfg(blocks)
    return label2idx

def insert_preheader(blocks, header_idx, loop_set):
    """
    Create actual preheader by:
     - Ensuring block exists (via ensure_preheader),
     - Rewiring edges from outside preds to the preheader,
     - Adding jmp from preheader to header,
     - Rebuilding CFG (because indices shift).
    Returns (blocks, pre_idx, header_idx_after).
    """
    result = ensure_preheader(blocks, header_idx, loop_set)
    if result != "NEEDS_REBUILD":
        # already have preheader
        return blocks, result, header_idx

    # We inserted a new labeled block before header; find it & rewire.
    # After insertion, header moved to header_idx+1
    pre_idx = header_idx
    header_idx_after = header_idx + 1
    pre = blocks[pre_idx]
    header = blocks[header_idx_after]
    header_label = header.instrs[0]["label"]
    pre_label = pre.instrs[0]["label"]

    # Find current outside preds (by label) before rebuild
    # We need a temporary mapping from labels to indices
    lab2idx_tmp = { b.instrs[0]["label"]: i for i,b in enumerate(blocks) }
    # Determine old outside preds by scanning all blocks that branch/jmp to header_label
    outside_preds = []
    for i,b in enumerate(blocks):
        if i == pre_idx: continue
        for ins in b.instrs:
            if ins.get("op") == "br":
                labs = ins.get("labels", [])
                if header_label in labs and (i not in range(header_idx, header_idx_after+1)):  # rough
                    outside_preds.append(i)
            elif ins.get("op") == "jmp":
                labs = ins.get("labels", [])
                if labs and labs[0] == header_label and (i not in range(header_idx, header_idx_after+1)):
                    outside_preds.append(i)
    # Dedup
    outside_preds = sorted(set(outside_preds))

    # Rewire branches/jmps that *target header* from outside the loop to target preheader instead.
    for i in outside_preds:
        for ins in blocks[i].instrs:
            if ins.get("op") in ("br","jmp"):
                labs = ins.get("labels", [])
                for k,lab in enumerate(labs):
                    if lab == header_label:
                        labs[k] = pre_label

    # Add explicit jump from pre → header
    pre.instrs.append({"op": "jmp", "labels": [header_label]})

    # Rebuild CFG fully so indices/preds/succs are correct
    rebuild_all(blocks)
    # Recompute header_idx_after and pre_idx after rebuild by label
    lab2idx = { b.instrs[0]["label"]: i for i,b in enumerate(blocks) }
    return blocks, lab2idx[pre_label], lab2idx[header_label]

# ---------------------------
# Def / Use maps
# ---------------------------

def build_def_block_map(blocks):
    """Return var -> (block_idx, instr_idx) of its unique definition (SSA)."""
    m = {}
    for bi,b in enumerate(blocks):
        for ii,ins in enumerate(b.instrs):
            d = instr_def(ins)
            if d:
                m[d] = (bi, ii)
    return m

def build_uses_map(blocks):
    """var -> list of (block_idx, instr_idx) where it's used."""
    uses = defaultdict(list)
    for bi,b in enumerate(blocks):
        for ii,ins in enumerate(b.instrs):
            for u in instr_uses(ins):
                uses[u].append((bi, ii))
    return uses

# ---------------------------
# Loop-invariant detection
# ---------------------------

def find_loop_invariants(blocks, loop_set, defmap):
    """
    Classic iterative marking:
    I in loop is invariant if:
      - I is hoistable_pure, and
      - For every arg x:
           def(x) is outside loop, OR def(x) is invariant (inside loop)
    """
    invariant = set()  # (block_idx, instr_idx)
    changed = True
    loop_blocks = [i for i in loop_set]
    while changed:
        changed = False
        for bi in loop_blocks:
            b = blocks[bi]
            for ii,ins in enumerate(b.instrs):
                if not is_hoistable_pure(ins):
                    continue
                # never hoist phis
                if ins.get("op") == "phi":
                    continue
                # Skip labels/terminators
                if "op" not in ins:
                    continue
                if is_terminator(ins):
                    continue
                ok = True
                for u in instr_uses(ins):
                    defloc = defmap.get(u)
                    if defloc is None:
                        # argument might be a function parameter or global; treat as outside loop
                        continue
                    dbi, _ = defloc
                    if dbi in loop_set:
                        # only ok if that defining instr is already invariant
                        if (dbi, defloc[1]) not in invariant:
                            ok = False
                            break
                if ok and (bi,ii) not in invariant:
                    invariant.add((bi,ii))
                    changed = True
    return invariant
def topo_sort_invariants(blocks, invariant):
    """Topologically order invariant instructions by def-use deps within the invariant set."""
    inv_list = list(invariant)       # ✅ convert set to list
    inv_set = set(inv_list)
    N = len(inv_list)
    idx_map = { node:i for i,node in enumerate(inv_list) }
    adj = [[] for _ in range(N)]
    indeg = [0]*N

    # Map var -> producing node index if invariant
    prod = {}
    for k,(bi,ii) in enumerate(inv_list):
        d = instr_def(blocks[bi].instrs[ii])
        if d:
            prod[d] = k

    for k,(bi,ii) in enumerate(inv_list):
        ins = blocks[bi].instrs[ii]
        for u in instr_uses(ins):
            if u in prod:
                p = prod[u]
                adj[p].append(k)
                indeg[k]+=1

    dq = deque([i for i in range(N) if indeg[i]==0])
    order = []
    while dq:
        x = dq.popleft()
        order.append(inv_list[x])
        for y in adj[x]:
            indeg[y]-=1
            if indeg[y]==0:
                dq.append(y)

    if len(order)!=N:
        # cycle among invariants shouldn't happen for pure ops, but be robust: fall back to original order
        return inv_list
    return order


# ---------------------------
# Hoist
# ---------------------------

def clone_without_label(ins):
    c = copy.deepcopy(ins)
    if "label" in c:
        del c["label"]
    return c

def insert_before_terminator(block, new_ins):
    # put before a terminator if present; otherwise append
    pos = len(block.instrs)
    for i,ins in enumerate(block.instrs):
        if ins.get("op") in TERMINATORS:
            pos = i
            break
    block.instrs.insert(pos, new_ins)

def hoist_invariants_to_preheader(blocks, loop_set, pre_idx, invariant):
    """
    Move invariant instructions (clone to pre; delete originals).
    We keep the same dest names (SSA); preheader dominates the loop header.
    """
    order = topo_sort_invariants(blocks, invariant)
    pre = blocks[pre_idx]

    # Create a stable set for quick lookup
    inv_set = set(invariant)

    # Insert clones in dependency order
    for (bi,ii) in order:
        ins = blocks[bi].instrs[ii]
        # double-check purity
        if not is_hoistable_pure(ins):
            continue
        new_ins = clone_without_label(ins)
        insert_before_terminator(pre, new_ins)

    # Remove originals in reverse block order to keep indices valid
    by_block = defaultdict(list)
    for (bi,ii) in invariant:
        by_block[bi].append(ii)
    for bi, idxs in by_block.items():
        idxs.sort(reverse=True)
        for ii in idxs:
            # delete original
            del blocks[bi].instrs[ii]

# ---------------------------
# Tiny local DCE (optional)
# ---------------------------

def local_dce(blocks):
    """Very small DCE: remove pure defs whose value is never used."""
    # rebuild uses
    uses = build_uses_map(blocks)
    changed = True
    any_change = False
    while changed:
        changed = False
        for bi,b in enumerate(blocks):
            # skip the label at [0]
            ii = 1
            while ii < len(b.instrs):
                ins = b.instrs[ii]
                d = instr_def(ins)
                if d and is_hoistable_pure(ins):
                    if d not in uses or len(uses[d])==0:
                        del b.instrs[ii]
                        any_change = True
                        changed = True
                        continue  # don't advance ii
                ii += 1
        if changed:
            uses = build_uses_map(blocks)
    return any_change

# ---------------------------
# Flatten blocks back to instr list
# ---------------------------

def flatten_blocks(blocks):
    instrs = []
    for b in blocks:
        instrs.extend(b.instrs)
    return instrs

# ---------------------------
# LICM driver for one function
# ---------------------------

def licm_function(func, do_clean=False):
    # 1) Blocks & CFG
    blocks = split_blocks(func)
    rebuild_all(blocks)

    changed = True
    while changed:
        changed = False

        # 2) Dominators
        dom, dominates = compute_dominators(blocks, entry=0)

        # 3) Backedges & loops
        backs = find_backedges(blocks, dominates)
        if not backs:
            break

        # Process each loop independently (repeat until stable)
        for (u,h) in backs:
            loop = natural_loop(blocks, u, h)
            if not loop:
                continue

            # 4) Preheader
            blocks, pre_idx, h_idx = insert_preheader(blocks, h, loop)
            # Header may have shifted; loop indices must be recomputed by label
            dom, dominates = compute_dominators(blocks, entry=0)

            # Rebuild def map (SSA) for invariant analysis
            defmap = build_def_block_map(blocks)

            # 5) Find invariants (pure & args from outside or invariant)
            invariant = find_loop_invariants(blocks, loop, defmap)
            if not invariant:
                continue

            # 6) Hoist to preheader
            hoist_invariants_to_preheader(blocks, loop, pre_idx, invariant)

            # 7) Cleanup (optional small DCE) and rebuild CFG
            if do_clean:
                local_dce(blocks)

            rebuild_all(blocks)
            changed = True

    # Flatten back
    func["instrs"] = flatten_blocks(blocks)
    return func

# ---------------------------
# Main
# ---------------------------

def main():
    ap = argparse.ArgumentParser(description="LICM for Bril (SSA).")
    ap.add_argument("--in", dest="inp", default=None, help="Input Bril JSON file (default: stdin)")
    ap.add_argument("--out", dest="out", default=None, help="Output Bril JSON file (default: stdout)")
    ap.add_argument("--clean", action="store_true", help="Run tiny local DCE after hoisting")
    args = ap.parse_args()

    prog = read_program(args.inp)
    for f in prog.get("functions", []):
        # skip non-main etc — apply to all
        licm_function(f, do_clean=args.clean)

    write_program(prog, args.out)

if __name__ == "__main__":
    main()
