#!/usr/bin/env python3
import sys, json
from collections import defaultdict

TERMINATORS = {"br", "jmp", "ret"}

COMMUTATIVE = {"add", "mul", "and", "or", "eq", "feq", "fadd", "fmul"}  # eq/feq/fadd/fmul are safe to commute
PURE_BINOPS = {
    "add","sub","mul","div","rem",
    "and","or","xor","shl","shr",
    "eq","lt","gt","le","ge","ne",
    "fadd","fsub","fmul","fdiv",
    "feq","flt","fgt","fle","fge","fne"
}
PURE_UNARY = {"not","id","fneg"}
PURE = PURE_BINOPS | PURE_UNARY

EFFECTFUL = {"print","nop","alloc","free","store","load","call","ret"}

# Track floating point types separately for proper handling
FLOAT_OPS = {"fadd", "fsub", "fmul", "fdiv", "feq", "flt", "fgt", "fle", "fge", "fne"}

def is_label(i): return "label" in i
def block_name_of(lab): return lab["label"]

def partition_blocks(func):
    blocks, order = {}, []
    cur_name, cur_instrs = None, []
    for ins in func["instrs"]:
        if is_label(ins):
            # If we see a label and we have seen instructions before it
            # (a function prologue), save that prologue as an entry block.
            if cur_name is not None:
                blocks[cur_name] = cur_instrs
            else:
                if cur_instrs:
                    # create a synthetic entry block for prologue
                    entry_name = "_entry"
                    blocks[entry_name] = cur_instrs
                    order.append(entry_name)
            cur_name = block_name_of(ins)
            order.append(cur_name)
            cur_instrs = []
        else:
            cur_instrs.append(ins)
    # If no label was ever seen, create a single entry block
    if cur_name is None and order == []:
        cur_name = "_entry"
        order = [cur_name]
    blocks[cur_name] = cur_instrs
    return blocks, order

def successors(instrs, fallthrough):
    if not instrs:
        return [fallthrough] if fallthrough else []
    last = instrs[-1]
    op = last.get("op")
    if op == "br":   return last.get("labels", [])
    if op == "jmp":  return [last.get("labels", [])[0]]
    if op == "ret":  return []
    return [fallthrough] if fallthrough else []

def build_cfg(blocks, order):
    succs = {b: [] for b in order}
    preds = {b: [] for b in order}
    for i, b in enumerate(order):
        fall = order[i+1] if i+1 < len(order) else None
        s = successors(blocks[b], fall)
        succs[b] = s
        for t in s:
            preds.setdefault(t, []).append(b)
    return succs, preds, order[0]

def compute_dominators(order, preds, entry):
    dom = {b: set(order) for b in order}
    dom[entry] = {entry}
    changed = True
    while changed:
        changed = False
        for b in order:
            if b == entry: continue
            new = set(order)
            for p in preds.get(b, []):
                new &= dom[p]
            new.add(b)
            if new != dom[b]:
                dom[b] = new
                changed = True
    # Immediate dominators (Cooper et al. style extraction)
    idom = {entry: None}
    for b in order:
        if b == entry: continue
        candidates = dom[b] - {b}
        # pick d in candidates such that for all e!=d in candidates: d in dom[e] implies e==d
        best = None
        for d in candidates:
            ok = True
            for e in candidates:
                if e == d: continue
                if d in dom[e]:  # d dominates e -> e is deeper; then d cannot be idom if another dominates e
                    ok = False
                    break
            if ok:
                best = d
                break
        if best is None:
            # fallback: choose the candidate with the largest dom-set (closest to b)
            best = max(candidates, key=lambda x: len(dom[x])) if candidates else None
        idom[b] = best
    children = {b: [] for b in order}
    for b in order:
        p = idom[b]
        if p is not None:
            children[p].append(b)
    return dom, idom, children

def domtree_preorder(children, root):
    out = []
    def dfs(b):
        out.append(b)
        for c in children[b]:
            dfs(c)
    dfs(root)
    return out

def normalize_commutative(op, args_vn):
    # args_vn may contain heterogeneous items (ints for real VNs and tuples
    # for opaque/unknown placeholders). Use a stable key that orders ints
    # before other kinds and falls back to string repr for non-ints.
    if op in COMMUTATIVE:
        def _key(x):
            if isinstance(x, int):
                return (0, x)
            return (1, str(x))
        return tuple(sorted(args_vn, key=_key))
    return tuple(args_vn)

class VNEnv:
    def __init__(self, parent=None):
        if parent is None:
            self.var2vn = {}
            self.exp2vn = {}
            self.vn2repr = {}
            self.const_of_vn = {}
            self.next_vn = 1
        else:
            self.var2vn = dict(parent.var2vn)
            self.exp2vn = dict(parent.exp2vn)
            self.vn2repr = dict(parent.vn2repr)
            self.const_of_vn = dict(parent.const_of_vn)
            self.next_vn = parent.next_vn

    def new_vn(self):
        v = self.next_vn
        self.next_vn += 1
        return v

    def vn_for_const(self, typ, value):
        # Special handling for floating point values to avoid precision issues
        if typ in {"float"}:
            try:
                value = float(value)  # Normalize float representation
            except (TypeError, ValueError):
                pass
        key = ("const", typ, json.dumps(value, sort_keys=True))
        if key in self.exp2vn:
            vn = self.exp2vn[key]
        else:
            vn = self.new_vn()
            self.exp2vn[key] = vn
            self.const_of_vn[vn] = (typ, value)
        return vn

    def vn_for_expr(self, key):
        if key in self.exp2vn:
            return self.exp2vn[key]
        vn = self.new_vn()
        self.exp2vn[key] = vn
        return vn

    def set_var(self, var, vn):
        # Only update var->vn mapping here. Do NOT auto-assign a canonical
        # representative (vn2repr) because some defs (phis/placeholders)
        # are created before we know a proper leader. Assigning vn2repr
        # should be done explicitly at the point we decide a variable is
        # the canonical representative for that vn.
        self.var2vn[var] = vn

    def get_var_vn(self, var):
        return self.var2vn.get(var)

    def canonical_var(self, vn):
        return self.vn2repr.get(vn)

def can_gvn(instr):
    op = instr.get("op")
    return (op in PURE) and (op not in EFFECTFUL)

def is_const(instr): return instr.get("op") == "const" and "dest" in instr
def is_phi(instr):   return instr.get("op") == "phi"

# ----- semantics helpers (fixes) -----

def trunc_div_toward_zero(a, b):
    if b == 0: return None  # don't fold; leave for runtime error/semantics
    return int(a / b)  # Python int() truncs toward zero

def eval_bin(op, a, b):
    try:
        if op == "add": return a + b
        if op == "sub": return a - b
        if op == "mul": return a * b
        if op == "div": return trunc_div_toward_zero(a, b)
        if op == "rem": return a % b
        if op == "and": return bool(a) and bool(b)
        if op == "or":  return bool(a) or bool(b)
        if op == "xor": return a ^ b
        if op == "shl": return a << b
        if op == "shr": return a >> b
        if op in {"eq","ne","lt","gt","le","ge"}:
            ops = {
                "eq": a == b, "ne": a != b, "lt": a < b,
                "gt": a > b, "le": a <= b, "ge": a >= b
            }
            return bool(ops[op])
        if op == "fadd": return a + b
        if op == "fsub": return a - b
        if op == "fmul": return a * b
        if op == "fdiv": return a / b
        if op in {"feq","fne","flt","fgt","fle","fge"}:
            ops = {
                "feq": a == b, "fne": a != b, "flt": a < b,
                "fgt": a > b, "fle": a <= b, "fge": a >= b
            }
            return bool(ops[op])
    except Exception:
        return None
    return None

def eval_unary(op, a):
    try:
        if op == "not":  return not bool(a)
        if op == "fneg": return -a
        if op == "id":   return a
    except Exception:
        return None
    return None

def all_const_vns(vns, env): return all(v in env.const_of_vn for v in vns)

# ----- core transform -----

def process_block(name, blocks, env):
    instrs = blocks[name]
    out = []

    # First pass: handle constants and phi nodes
    tmp = []
    for ins in instrs:
        if is_const(ins) and "dest" in ins:
            # Handle constants first to ensure proper value numbering
            dest = ins["dest"]
            typ = ins.get("type")
            vn = env.vn_for_const(typ, ins["value"])
            env.set_var(dest, vn)
            tmp.append(ins)
            continue

        if is_phi(ins) and "dest" in ins:
            dest, typ = ins["dest"], ins.get("type")
            arg_vns = []
            all_known = True
            unique_vns = set()
            
            # Collect all known value numbers from phi arguments
            for a in ins.get("args", []):
                vn = env.get_var_vn(a)
                if vn is None:
                    all_known = False
                    break
                arg_vns.append(vn)
                unique_vns.add(vn)
            
            if all_known and len(unique_vns) == 1:
                # All arguments have the same value number - can fold.
                # Pick a leader that is visible in this scope. Prefer the
                # canonical representative if present and visible; otherwise
                # try any incoming argument that is visible here. If none are
                # visible, we cannot safely fold to an out-of-scope name and
                # instead keep the phi and assign the VN to dest.
                vn = next(iter(unique_vns))
                leader = None
                rep = env.canonical_var(vn)
                if rep is not None and env.get_var_vn(rep) == vn:
                    leader = rep
                else:
                    for a in ins.get("args", []):
                        if env.get_var_vn(a) == vn:
                            leader = a
                            break
                if leader is not None:
                    tmp.append({"op":"id","args":[leader],"dest":dest,"type":typ})
                    env.set_var(dest, vn)
                else:
                    # No visible leader; keep original phi but assign VN
                    env.set_var(dest, vn)
                    tmp.append(ins)
            else:
                # Cannot fold - assign new value number
                vn = env.new_vn()
                env.set_var(dest, vn)
                tmp.append(ins)
        else:
            tmp.append(ins)
    instrs = tmp

    for ins in instrs:
        if "dest" not in ins:
            out.append(ins)
            continue

        dest = ins["dest"]
        typ  = ins.get("type")
        op   = ins.get("op")

        if is_const(ins):
            vn = env.vn_for_const(typ, ins["value"])
            # set var mapping and make this const the canonical representative
            env.set_var(dest, vn)
            if vn not in env.vn2repr:
                env.vn2repr[vn] = dest
            out.append(ins)
            continue

        if op == "id" and ins.get("args"):
            src = ins["args"][0]
            src_vn = env.get_var_vn(src)
            if src_vn is None:
                # unknown source; treat as unique value not yet seen
                src_vn = env.new_vn()
            env.set_var(dest, src_vn)
            rep = env.canonical_var(src_vn) or src
            if rep != src:
                out.append({"op":"id","args":[rep],"dest":dest,"type":typ})
            else:
                out.append(ins)
            continue

        if can_gvn(ins):
            args = ins.get("args", [])
            # Build VN args; DO NOT assign VN to unknowns—use opaque placeholders.
            args_vn = []
            for a in args:
                vn = env.get_var_vn(a)
                if vn is None:
                    # unique opaque key for hashing that won't pollute env
                    vn = ("unknown", a)
                args_vn.append(vn)

            # Constant fold when both operands are known constants
            # (or unary known constant)
            if len(args_vn) == 1 and isinstance(args_vn[0], int) and args_vn[0] in env.const_of_vn:
                aval = env.const_of_vn[args_vn[0]][1]
                folded = eval_unary(op, aval)
                if folded is not None:
                    out_typ = "bool" if op == "not" else typ
                    vn = env.vn_for_const(out_typ, folded)
                    env.set_var(dest, vn)
                    out.append({"op":"const","dest":dest,"type":out_typ,"value": folded})
                    continue

            if len(args_vn) == 2 and all(isinstance(v, int) and (v in env.const_of_vn) for v in args_vn):
                a = env.const_of_vn[args_vn[0]][1]
                b = env.const_of_vn[args_vn[1]][1]
                folded = eval_bin(op, a, b)
                if folded is not None:
                    out_typ = "bool" if op in {
                        "eq","ne","lt","gt","le","ge","feq","fne","flt","fgt","fle","fge","and","or"
                    } else typ
                    vn = env.vn_for_const(out_typ, folded)
                    env.set_var(dest, vn)
                    out.append({"op":"const","dest":dest,"type":out_typ,"value": folded})
                    continue

            # Hash expression
            norm = normalize_commutative(op, tuple(args_vn))
            key = ("op", op, norm, typ)
            vn = env.vn_for_expr(key)

            rep = env.canonical_var(vn)
            if rep is not None:
                env.set_var(dest, vn)
                out.append({"op":"id","args":[rep],"dest":dest,"type":typ})
            else:
                env.set_var(dest, vn)
                # record this dest as the leader for the expression vn
                if vn not in env.vn2repr:
                    env.vn2repr[vn] = dest
                out.append(ins)
            continue

        # Effectful/unknown op: keep, assign fresh VN
        env.set_var(dest, env.new_vn())
        out.append(ins)

    return out, env

def run_gvn_on_func(func):
    blocks, order = partition_blocks(func)
    succs, preds, entry = build_cfg(blocks, order)
    _, _, children = compute_dominators(order, preds, entry)
    dom_pre = domtree_preorder(children, entry)

    block_env_out = {}
    transformed = {}

    parent = {entry: None}
    for p, kids in children.items():
        for k in kids:
            parent[k] = p

    for b in dom_pre:
        pin = parent.get(b)
        env = VNEnv(block_env_out[pin]) if pin is not None else VNEnv()
        new_instrs, out_env = process_block(b, blocks, env)
        transformed[b] = new_instrs
        block_env_out[b] = out_env

    # Some blocks may not appear in the dominator preorder (unreachable or
    # separate components). Ensure every block in 'order' is processed so the
    # later assembly loop doesn't KeyError.
    for b in order:
        if b not in transformed:
            env = VNEnv()
            new_instrs, out_env = process_block(b, blocks, env)
            transformed[b] = new_instrs
            block_env_out[b] = out_env

    out_instrs = []
    for b in order:
        if not b.startswith("_entry"):
            out_instrs.append({"label": b})
        out_instrs.extend(transformed[b])

    newf = dict(func)
    newf["instrs"] = out_instrs
    return newf

def run_gvn(prog):
    return {"functions": [run_gvn_on_func(f) for f in prog.get("functions",[])]}

if __name__ == "__main__":
    prog = json.load(sys.stdin)
    json.dump(run_gvn(prog), sys.stdout, indent=2)
