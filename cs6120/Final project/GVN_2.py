#!/usr/bin/env python3
"""
Global Value Numbering (GVN) for SSA Bril.

- Works on SSA Bril functions.
- Replaces redundant pure computations with `id` from an existing leader.
- Conservative around side effects, calls, stores, and memory (basic heap-versioning).
- Safe on control flow (dominance-order traversal + value tables with scoping).

Usage:
  bril2json < prog.bril | python3 gvn.py | bril2txt
  # or
  bril2json < prog.bril | python3 gvn.py | brili -p

Notes:
- Run an SSA conversion pass before this (SSA is required).
- Run a DCE pass after GVN to remove now-dead original computations.
"""

import json
import sys
from collections import defaultdict, deque

# ---------- Utilities ----------

COMMUTATIVE_OPS = {
    "add", "mul", "eq", "and", "or", "xor"
}

# Bril ops considered effectful (don’t value-number)
# We also “bump” memory version on these to be conservative.
EFFECTFUL_OPS = {
    "print", "jmp", "br", "ret", "call", "store"
}

# Ops that *read* memory and therefore depend on the heap version.
MEMORY_READ_OPS = {
    "load"
}

# Pure ops we can GVN safely (no side effects, no I/O, deterministic)
PURE_OPS = {
    # core
    "add", "sub", "mul", "div", "eq", "lt", "gt", "le", "ge",
    "not", "and", "or", "xor",
    "id",  # allowed: id x => just copy
    # float ext (if present)
    "fadd", "fsub", "fmul", "fdiv", "feq", "flt", "fgt", "fle", "fge",
}

def is_label(instr):
    return "label" in instr

def term_successors(instr):
    """
    Return list of successor labels for a terminator (br/jmp), else [].
    """
    if "op" not in instr:
        return []
    op = instr["op"]
    if op == "br":
        # {"op":"br","args":["cond"],"labels":["L1","L2"]}
        return instr.get("labels", [])
    if op == "jmp":
        # {"op":"jmp","labels":["L1"]}
        return instr.get("labels", [])
    return []

def instr_is_effectful(instr):
    if "op" not in instr:
        return False
    op = instr["op"]
    if op in EFFECTFUL_OPS:
        return True
    # Any instruction explicitly marked 'effect' true.
    return instr.get("effect", False)

def instr_reads_memory(instr):
    if "op" not in instr:
        return False
    return instr["op"] in MEMORY_READ_OPS

def instr_is_pure_value(instr):
    if "dest" not in instr or "op" not in instr:
        return False
    if instr["op"] in PURE_OPS and not instr.get("effect", False):
        # id is pure too (it just copies)
        return True
    return False

def normalize_expr_key(op, args, consts_key=None, memver=None):
    """
    Make a canonical hashable key for an expression.
    - args are *value numbers* here, not raw var names
    - commutative ops are sorted by arg value-number
    - include constants bundle and memory version when relevant
    """
    if op in COMMUTATIVE_OPS and len(args) == 2:
        a = tuple(sorted(args))
    else:
        a = tuple(args)

    key = ("op", op, a)
    if consts_key is not None:
        key = key + ("k", tuple(consts_key))
    if memver is not None:
        key = key + ("m", memver)
    return key

def normalize_phi_key(pred_labels, arg_vns):
    """
    SSA φ congruence key: ordered by incoming edges.
    We key by (phi, [(pred_label, vn), ...]) to distinguish different control paths.
    """
    return ("phi", tuple(zip(pred_labels, arg_vns)))

# ---------- CFG & Dominators ----------

class CFG:
    def __init__(self, func):
        self.func = func
        self.blocks = []            # list of blocks, each block: {"label": str, "instrs": [..]}
        self.label2idx = {}
        self.succ = defaultdict(list)   # idx -> [idx,...]
        self.pred = defaultdict(list)   # idx -> [idx,...]
        self.entry = None

        self._split_blocks()
        self._wire_cfg()

    def _split_blocks(self):
        instrs = self.func.get("instrs", [])
        # Determine leaders: first instr, any label, and any target of jump/branch
        leaders = set()
        label_positions = {}
        for i, ins in enumerate(instrs):
            if is_label(ins):
                label_positions[ins["label"]] = i

        # first instruction is leader
        if instrs:
            leaders.add(0)

        for i, ins in enumerate(instrs):
            if is_label(ins):
                leaders.add(i)
            # next instruction after terminator is a leader
            if "op" in ins and ins["op"] in ("br", "jmp"):
                if i + 1 < len(instrs):
                    leaders.add(i + 1)

        # create blocks
        leader_list = sorted(leaders)
        for bi, start in enumerate(leader_list):
            end = (leader_list[bi + 1] if bi + 1 < len(leader_list) else len(instrs))
            block_instrs = instrs[start:end]
            # find the label (if any), else synth
            label = None
            if block_instrs and is_label(block_instrs[0]):
                label = block_instrs[0]["label"]
                body = block_instrs[1:]
            else:
                label = f"__blk_{bi}"
                body = block_instrs
            self.blocks.append({"label": label, "instrs": body})
            self.label2idx[label] = bi

        self.entry = 0

    def _wire_cfg(self):
        # successsors from terminators
        for i, blk in enumerate(self.blocks):
            succs = []
            if blk["instrs"]:
                last = blk["instrs"][-1]
                labels = term_successors(last)
                for L in labels:
                    if L in self.label2idx:
                        succs.append(self.label2idx[L])
            # fall-through if no terminator and not last block
            if not succs and i + 1 < len(self.blocks):
                succs.append(i + 1)
            self.succ[i] = succs
            for j in succs:
                self.pred[j].append(i)

    def dominance(self):
        """
        Simple iterative dominator computation.
        Returns: dom_sets (list of sets of block indices), and idom (immediate dominator map)
        """
        n = len(self.blocks)
        dom = [set(range(n)) for _ in range(n)]
        dom[self.entry] = {self.entry}

        changed = True
        while changed:
            changed = False
            for b in range(n):
                if b == self.entry:
                    continue
                preds = self.pred[b]
                if not preds:
                    new = {b}
                else:
                    new = set(range(n))
                    for p in preds:
                        new &= dom[p]
                    new.add(b)
                if new != dom[b]:
                    dom[b] = new
                    changed = True

        # immediate dominators
        idom = [None] * n
        for b in range(n):
            if b == self.entry:
                idom[b] = None
                continue
            # idom is the unique d in dom[b]-{b} that does not strictly dominate any other in that set
            candidates = dom[b] - {b}
            # pick d \in candidates s.t. for all c in candidates, if c != d then c not dominates b more strictly
            # A simpler heuristic: pick the one with maximum |dom[d]| (closest to b)
            if candidates:
                idom[b] = max(candidates, key=lambda d: len(dom[d]))
            else:
                idom[b] = None

        # dominance tree preorder
        tree = defaultdict(list)
        for b in range(n):
            if idom[b] is not None:
                tree[idom[b]].append(b)

        order = []
        def dfs(u):
            order.append(u)
            for v in tree[u]:
                dfs(v)
        dfs(self.entry)
        return dom, idom, order

# ---------- GVN Core ----------

class GVN:
    def __init__(self):
        # mapping var -> value number
        self.var2vn = {}
        # expression table: expr_key -> (vn, leader_var)
        self.expr2leader = {}
        # constants table (to fold consts into keys neatly)
        # We keep a separate dict for variable->constant if helpful later; not required.
        self.next_vn = 1

        # memory version for basic heap-versioning
        self.memver = 0

        # Scoped stacks to support dominance traversal (push/pop)
        self.scopes = []

    def push_scope(self):
        self.scopes.append((
            dict(self.var2vn),
            dict(self.expr2leader),
            self.next_vn,
            self.memver
        ))

    def pop_scope(self):
        (self.var2vn,
         self.expr2leader,
         self.next_vn,
         self.memver) = self.scopes.pop()

    def new_vn(self):
        v = self.next_vn
        self.next_vn += 1
        return v

    def vn_of_var(self, vname):
        return self.var2vn.get(vname, None)

    def set_var_vn(self, vname, vn):
        self.var2vn[vname] = vn

    def bump_memory(self):
        self.memver += 1

    def value_number_of_args(self, args):
        vns = []
        for a in args:
            vn = self.vn_of_var(a)
            if vn is None:
                # If an operand hasn't been defined yet in dominance order,
                # assign a fresh value number to avoid collapsing incorrectly.
                vn = self.new_vn()
                self.set_var_vn(a, vn)
            vns.append(vn)
        return vns

    def process_phi(self, blk_label, instr, pred_labels):
        """
        Assign a value number to phi. If all incoming VNs are identical, reuse that VN (copy-prop).
        Otherwise create/find congruence class keyed by (phi, [(pred,vn)...]).
        """
        dest = instr["dest"]
        args = instr.get("args", [])
        arg_vns = self.value_number_of_args(args)
        key = normalize_phi_key(pred_labels, arg_vns)

        # If all arg VNs are equal, φ(x,x,...) == x
        if len(arg_vns) > 0 and all(v == arg_vns[0] for v in arg_vns):
            vn = arg_vns[0]
            self.set_var_vn(dest, vn)
            # No need to record an expr leader; it is literally same value.
            return None  # no rewrite
        else:
            # Look up or create VN for this phi congruence
            if key in self.expr2leader:
                vn, leader = self.expr2leader[key]
            else:
                vn = self.new_vn()
                leader = dest
                self.expr2leader[key] = (vn, leader)
            self.set_var_vn(dest, vn)
            return None  # don't rewrite φ itself here

    def process_pure(self, instr):
        """
        Handle pure value-producing ops. If an equivalent expr exists, rewrite to `id leader`.
        Otherwise assign a new VN and record leader.
        """
        op = instr["op"]
        dest = instr["dest"]
        args = instr.get("args", [])
        consts_key = None

        # Some ops may have 'value' (constants) or other attrs
        # but for standard Bril, constants are separate 'const' op.
        if op == "const":
            # const folded by literal value + type
            lit = instr.get("value")
            ty = instr.get("type")
            key = ("const", ty, lit)
            if key in self.expr2leader:
                vn, leader = self.expr2leader[key]
                self.set_var_vn(dest, vn)
                # rewrite to id leader
                return {
                    "op": "id",
                    "dest": dest,
                    "type": ty,
                    "args": [leader],
                }
            else:
                vn = self.new_vn()
                self.expr2leader[key] = (vn, dest)
                self.set_var_vn(dest, vn)
                return None

        # value numbers of operands
        arg_vns = self.value_number_of_args(args)

        # Memory-sensitive?
        memver = None
        if instr_reads_memory(instr):
            memver = self.memver

        key = normalize_expr_key(op, arg_vns, consts_key=consts_key, memver=memver)

        if key in self.expr2leader:
            vn, leader = self.expr2leader[key]
            self.set_var_vn(dest, vn)
            # rewrite to id leader with same type
            ty = instr.get("type")
            # If op produces no type (shouldn't happen for pure), keep as-is.
            if ty is not None:
                return {
                    "op": "id",
                    "dest": dest,
                    "type": ty,
                    "args": [leader],
                }
            else:
                # fallback: keep as-is (rare)
                return None
        else:
            vn = self.new_vn()
            self.expr2leader[key] = (vn, dest)
            self.set_var_vn(dest, vn)
            return None

    def process_effectful(self, instr):
        """
        For effectful ops, we’re conservative and bump memory version where appropriate.
        Also assign fresh VN to dest (if any), but don’t try to dedup.
        """
        op = instr.get("op")
        if op in ("store", "call"):
            self.bump_memory()
        if instr.get("effect", False):
            self.bump_memory()

        if "dest" in instr and "op" in instr and instr["op"] != "br":
            # Give a fresh VN because effectful result can't be replaced safely.
            vn = self.new_vn()
            self.set_var_vn(instr["dest"], vn)
        return None

    def rewrite_block(self, cfg, bidx, pred_labels_map):
        blk = cfg.blocks[bidx]
        out_instrs = []
        # First handle φ at block top (SSA convention: φs first)
        # We need predecessor labels in fixed order.
        pred_idxs = cfg.pred[bidx]
        pred_labels = [cfg.blocks[p]["label"] for p in pred_idxs]

        # φs are recognized in Bril as op "phi" (if present); if not using φ extension, skip.
        i = 0
        while i < len(blk["instrs"]) and blk["instrs"][i].get("op") == "phi":
            phi = blk["instrs"][i]
            # Bril φ form: { "op":"phi", "dest":..., "type":..., "labels":[...], "args":[...] }
            # Ensure labels align with preds; if not provided, use instr labels.
            phi_labels = phi.get("labels", pred_labels)
            self.process_phi(blk["label"], phi, phi_labels)
            out_instrs.append(phi)  # We do not rewrite φ itself here (copy-prop may later remove)
            i += 1

        # Other instructions
        while i < len(blk["instrs"]):
            ins = blk["instrs"][i]

            if "op" not in ins:
                out_instrs.append(ins)
                i += 1
                continue

            op = ins["op"]

            if op == "phi":
                # If φ appears after non-φ, just handle like a pure def (but better to keep φs first).
                self.process_phi(blk["label"], ins, ins.get("labels", pred_labels))
                out_instrs.append(ins)
                i += 1
                continue

            if instr_is_effectful(ins):
                new = self.process_effectful(ins)
                out_instrs.append(ins if new is None else new)
            elif instr_is_pure_value(ins):
                new = self.process_pure(ins)
                out_instrs.append(ins if new is None else new)
            else:
                # Non-effectful but not recognized as pure → treat conservatively
                if "dest" in ins:
                    vn = self.new_vn()
                    self.set_var_vn(ins["dest"], vn)
                out_instrs.append(ins)

            # bump memory version on terminators? Only control-flow, no
            i += 1

        return out_instrs

def run_gvn_on_func(func):
    # Build CFG + dominance order
    cfg = CFG(func)
    _, _, dom_order = cfg.dominance()

    gvn = GVN()
    # dominance-ordered traversal with scoped tables
    new_blocks = [None] * len(cfg.blocks)

    # Build a dom tree for scoping (reuse dominance()’s order with parent info)
    # We’ll emulate recursion by using a stack that tracks entry/exit scopes.
    # Simpler: do a DFS from entry with explicit recursion.

    # Re-derive idom-based tree:
    def dominance_tree():
        _, idom, _ = cfg.dominance()
        tree = defaultdict(list)
        for b in range(len(cfg.blocks)):
            if idom[b] is not None:
                tree[idom[b]].append(b)
        return tree

    tree = dominance_tree()

    sys.setrecursionlimit(10000)

    def dfs(bidx):
        gvn.push_scope()
        # Rewrite block in this scope
        out_instrs = gvn.rewrite_block(cfg, bidx, None)
        new_blocks[bidx] = {"label": cfg.blocks[bidx]["label"], "instrs": out_instrs}
        # Children
        for c in tree[bidx]:
            dfs(c)
        gvn.pop_scope()

    dfs(cfg.entry)

    # Stitch blocks back to a single instr list
    new_instrs = []
    for b in new_blocks:
        # Put label at top (as Bril label or explicit)
        if not b["label"].startswith("__blk_"):
            new_instrs.append({"label": b["label"]})
        else:
            # synthetic labels can be omitted (Bril doesn’t require labels for every block)
            pass
        new_instrs.extend(b["instrs"])

    new_func = dict(func)
    new_func["instrs"] = new_instrs
    return new_func

def run_gvn(prog):
    out = {"functions": []}
    for f in prog.get("functions", []):
        # Skip non-SSA if needed; here we assume SSA.
        out["functions"].append(run_gvn_on_func(f))
    return out

def main():
    prog = json.load(sys.stdin)
    out = run_gvn(prog)
    json.dump(out, sys.stdout)

if __name__ == "__main__":
    main()
