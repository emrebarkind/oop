"""GEPA experiment: search for extra reading guidance (appended to the extractor's system prompt) that makes the
spec extraction more accurate.

Search set: eval/dev/d1-d3 only (their own blind keys). The frozen test packages eval/packages/p1-p3 are never
shown to the search; the chosen guidance is measured on them afterwards, with the normal pipeline
(BUILDER_EXTRACT_GUIDANCE=<file>), against the same pipeline without it.

Each evaluation is one reading at effort "high" (the cheap core of the pipeline), scored by eval/score.py. The
feedback GEPA reflects on is concrete: which rule, what the brief says (quoted), what the spec made the agent do.

    python3 eval/gepa_extract.py [max_metric_calls]
"""
import concurrent.futures as cf
import json
import pathlib
import sys
import time

from gepa import optimize
from gepa.core.adapter import EvaluationBatch

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))
import score  # noqa: E402
from builder import claude, extract  # noqa: E402

DEV = ROOT / "eval" / "dev"
OUT = ROOT / "eval" / "out" / "gepa"
BASE_SYSTEM = extract.SYSTEM
SEED = ("Before writing the spec, list for yourself every rule the materials state, then make sure each one is carried "
        "by a spec field.")


class Quiet:
    def __init__(self):
        self.cost, self.calls = 0.0, 0

    def model_call(self, model, purpose, seconds, cost):
        self.calls += 1
        self.cost += cost or 0.0

    def note(self, text):
        pass


SPEND = Quiet()


def load(name):
    d = DEV / name
    return dict(name=name, docs=[("brief.md", (d / "brief.md").read_text())], openapi=json.load(open(d / "openapi.json")),
                key=json.load(open(d / "key.json")))


def run_one(inst, guidance):
    system = BASE_SYSTEM + ("\n\nAdditional guidance:\n" + guidance if guidance.strip() else "")
    out = claude.ask(extract.prompt_for(inst["docs"], inst["openapi"]), system, "gepa: one reading", SPEND)
    try:
        spec = claude.json_block(out, keys=extract.SPEC_KEYS)
    except Exception as e:
        return 0.0, f"the answer was not a spec JSON ({e})", None
    res = score.score(spec, DEV / inst["name"])
    checks = {c["id"]: c for c in inst["key"]["checks"]}
    lines = []
    for f in res["failed"]:
        c = checks.get(f["id"], {})
        lines.append(f"- rule {f.get('rule')} ({inst['key']['rules'].get(f.get('rule'), '')}): check {f['type']} "
                     f"{json.dumps(c.get('input'))[:200]} expected {json.dumps(f['expect'])} but the spec made the agent "
                     f"give {json.dumps(f['got'], default=str)[:200]}. The brief says: \"{c.get('source', '')[:300]}\"")
    fb = (f"{res['ok']}/{res['total']} checks right." + ("\n" + "\n".join(lines) if lines else " All rules carried."))
    return res["accuracy"], fb, spec


class Adapter:
    propose_new_texts = None

    def evaluate(self, batch, candidate, capture_traces=False):
        g = candidate["guidance"]
        with cf.ThreadPoolExecutor(len(batch)) as ex:
            res = list(ex.map(lambda i: run_one(i, g), batch))
        log = dict(t=time.strftime("%H:%M:%S"), guidance=g, scores={i["name"]: r[0] for i, r in zip(batch, res)},
                   cost_so_far=round(SPEND.cost, 2))
        with open(OUT / "evals.jsonl", "a") as f:
            f.write(json.dumps(log) + "\n")
        print(json.dumps(log)[:300], flush=True)
        traj = [dict(name=i["name"], feedback=r[1]) for i, r in zip(batch, res)] if capture_traces else None
        return EvaluationBatch(outputs=[r[2] for r in res], scores=[r[0] for r in res], trajectories=traj)

    def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
        recs = [{"Inputs": {"package": t["name"]}, "Generated Outputs": "spec (scored by behaviour checks)",
                 "Feedback": t["feedback"]} for t in eval_batch.trajectories]
        return {c: recs for c in components_to_update}


def reflection_lm(prompt):
    if isinstance(prompt, list):
        prompt = "\n\n".join(m.get("content", "") for m in prompt)
    rule = ("\n\nThe guidance you write is appended to the system prompt of an analyst who turns ANY bank's collections "
            "brief + tool API into a spec. It must be general: never name a bank, product, code, date or value from the "
            "feedback; state reading habits and pitfalls instead. Keep it under 250 words.")
    return claude.ask(prompt + rule, "You improve instructions for a document-reading task.", "gepa: reflection", SPEND)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    budget = int(sys.argv[1]) if len(sys.argv) > 1 else 18
    data = [load(n) for n in ("d1", "d2", "d3")]
    t0 = time.time()
    res = optimize(seed_candidate={"guidance": SEED}, trainset=data, valset=data, adapter=Adapter(),
                   reflection_lm=reflection_lm, max_metric_calls=budget, reflection_minibatch_size=3,
                   run_dir=str(OUT / "run"), display_progress_bar=False, seed=0)
    best = res.best_candidate["guidance"]
    (OUT / "best_guidance.txt").write_text(best)
    summary = dict(seconds=round(time.time() - t0), cost=round(SPEND.cost, 2), calls=SPEND.calls,
                   val_scores=[round(x, 3) for x in res.val_aggregate_scores], best_index=res.best_idx, best=best)
    json.dump(summary, open(OUT / "summary.json", "w"), indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
