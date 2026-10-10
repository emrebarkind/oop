"""Re-score every saved spec in eval/runs with the current scorer (keys unchanged), keeping run metadata."""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import score  # noqa: E402

for f in sorted((ROOT / (sys.argv[1] if len(sys.argv) > 1 else "runs")).glob("*.score.json")):
    old = json.load(open(f))
    spec = json.load(open(str(f).replace(".score.json", ".json")))
    res = score.score(spec, ROOT / "packages" / old["pkg"])
    json.dump(dict(res, **{k: old[k] for k in ("mode", "pkg", "seconds", "cost", "calls", "repairs", "error")}),
              open(f, "w"), indent=1, default=str)
print("rescored")
