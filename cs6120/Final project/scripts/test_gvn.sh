#!/usr/bin/env bash
# Run semantic equivalence checks between original and GVN-transformed Bril programs.
# Usage: ./scripts/test_gvn.sh benchmark1.bril benchmark2.bril ...
# If no args given, uses a default representative list.

set -euo pipefail

BENCHMARKS=("benchmark/ackermann.bril" "benchmark/loopfact.bril" "benchmark/fib_recursive.bril" "benchmark/gcd.bril" "benchmark/sqrt_bin_search.bril")
if [ "$#" -gt 0 ]; then
  BENCHMARKS=("$@")
fi

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

fail_count=0
# default empty ARGS array to avoid unbound variable when -u set
ARGS=()

for f in "${BENCHMARKS[@]}"; do
  echo "\n=== $f ==="
  # extract ARGS line (supports '# ARGS:' and '#ARGS:')
  ARGS_LINE=$(grep -E "^#\s*ARGS:|^#ARGS:" "$f" || true)
  if [ -z "$ARGS_LINE" ]; then
    echo "No ARGS line found, running with no inputs"
    ARGS=()
  else
    # drop prefix and split
    ARGS_TEXT=$(echo "$ARGS_LINE" | sed -E 's/^#\s*ARGS:\s*//; s/^#ARGS:\s*//')
    read -r -a ARGS <<< "$ARGS_TEXT"
  fi

  echo "ARGS: ${ARGS[*]:-}"

  # original execution
  ORIG_JSON="$TMPDIR/orig.json"
  bril2json < "$f" > "$ORIG_JSON"

  if command -v brili >/dev/null 2>&1; then
    ORIG_OUT="$TMPDIR/orig.out"
    if [ ${#ARGS[@]} -gt 0 ]; then
      echo "${ARGS[@]}" | brili < "$ORIG_JSON" > "$ORIG_OUT" 2>&1 || true
    else
      brili < "$ORIG_JSON" > "$ORIG_OUT" 2>&1 || true
    fi
  else
    echo "Warning: 'brili' not found in PATH; skipping execution test for original program"
    ORIG_OUT="$TMPDIR/orig.out.missing"
    echo "MISSING" > "$ORIG_OUT"
  fi

  # transformed execution
  GVN_JSON="$TMPDIR/gvn.json"
  python3 GVN.py < "$ORIG_JSON" > "$GVN_JSON"
  if command -v brili >/dev/null 2>&1; then
    GVN_OUT="$TMPDIR/gvn.out"
    if [ ${#ARGS[@]} -gt 0 ]; then
      echo "${ARGS[@]}" | brili < "$GVN_JSON" > "$GVN_OUT" 2>&1 || true
    else
      brili < "$GVN_JSON" > "$GVN_OUT" 2>&1 || true
    fi
  else
    echo "Warning: 'brili' not found in PATH; skipping execution test for transformed program"
    GVN_OUT="$TMPDIR/gvn.out.missing"
    echo "MISSING" > "$GVN_OUT"
  fi

  # compare outputs (if brili available)
  if [ -f "$ORIG_OUT" ] && [ -f "$GVN_OUT" ] && ! grep -q "MISSING" "$ORIG_OUT"; then
    if diff -u "$ORIG_OUT" "$GVN_OUT" >/dev/null; then
      echo "OK: outputs identical"
    else
      echo "FAIL: outputs differ"
      echo "--- original output ---"
      sed -n '1,200p' "$ORIG_OUT"
      echo "--- gvn output ---"
      sed -n '1,200p' "$GVN_OUT"
      fail_count=$((fail_count+1))
    fi
  else
    echo "Skipped runtime comparison for $f (brili missing or output not produced)."
  fi

done

if [ "$fail_count" -eq 0 ]; then
  echo "\nAll checked benchmarks matched (or were skipped due to missing runtime)."
else
  echo "\n$fail_count benchmarks failed semantic comparison."
fi

exit $fail_count
