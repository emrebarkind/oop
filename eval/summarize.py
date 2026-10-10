"""Summary of eval/runs/*.score.json: per mode and package, accuracy, missed rules, seconds, cost; and the checks
every run of both modes failed (candidates for an engine limit or a key problem, not an extraction difference)."""
import collections
import json
import pathlib
import statistics as st

import sys
RUNS = pathlib.Path(__file__).resolve().parent / (sys.argv[1] if len(sys.argv) > 1 else "runs")
rows = [json.load(open(f)) for f in sorted(RUNS.glob("*.score.json"))]
by = collections.defaultdict(list)
for r in rows:
    by[(r["mode"], r["pkg"])].append(r)


def line(label, rs):
    acc = [r["accuracy"] for r in rs]
    return (f"{label:10} n={len(rs)}  accuracy mean {st.mean(acc):.3f} (min {min(acc):.3f})  "
            f"missed rules mean {st.mean(len(r['missed_rules']) for r in rs):.2f}  "
            f"seconds mean {st.mean(r['seconds'] for r in rs):.0f}  cost mean ${st.mean(r['cost'] for r in rs):.2f}  "
            f"repairs mean {st.mean(r['repairs'] for r in rs):.1f}" + ("  ERRORS" if any(r["error"] for r in rs) else ""))


for mode in sorted({m for m, _ in by}):
    print(f"== mode {mode}")
    for pkg in sorted({p for _, p in by}):
        if by.get((mode, pkg)):
            print(line(pkg, by[(mode, pkg)]))
    print(line("all", [r for (m, _), rs in by.items() if m == mode for r in rs]))

fails = collections.defaultdict(lambda: collections.Counter())
runs = collections.Counter()
for r in rows:
    runs[(r["pkg"], r["mode"])] += 1
    for f in r["failed"]:
        fails[(r["pkg"], f.get("id"))][r["mode"]] += 1
MODES = sorted({m for _, m in runs})
print("\n== failed checks (pkg check: failures per mode / runs)")
for (pkg, cid), c in sorted(fails.items()):
    tot = {m: runs[(pkg, m)] for m in MODES}
    every = all(c[m] == tot[m] for m in tot if tot[m])
    ex = next(f for r in rows if r["pkg"] == pkg for f in r["failed"] if f.get("id") == cid)
    print(f"{pkg} {cid:4} {ex.get('type', ''):18} " + "  ".join(f"{m} {c[m]}/{tot[m]}" for m in MODES) + ("  ALL RUNS" if every else "") +
          f"  expect={json.dumps(ex.get('expect'))[:80]} got={json.dumps(ex.get('got'), default=str)[:80]}")
