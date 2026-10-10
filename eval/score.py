"""Scores a spec against a frozen key (eval/packages/<pkg>/key.json, format in eval/packages/CHECKS.md).

The key states behaviour, never spec fields. Each check is answered by running the engine's own functions on the
spec, after the same finalisation the build applies (lossless normalisation + what the tool API decides), so a
check passes only if the deployed agent would behave as the key says.

    python3 eval/score.py <spec.json> <package dir>      -> JSON report on stdout
"""
import copy
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from builder import compile as comp, spec_model  # noqa: E402
from builder.runtime import cal, engine  # noqa: E402


def setup(spec, openapi):
    spec = copy.deepcopy(spec)
    spec_model.normalise(spec)
    spec = comp.apply_defaults(spec, openapi)
    engine.S = spec
    cal.P = dict(workdays=spec["calendar"]["workdays"], holidays=spec["calendar"]["holidays"])
    return spec


def facts(spec, now, rec):
    P_ = spec.get("products", {})
    lf, idf = P_.get("list_field", "products"), P_.get("id_field", "product_id")
    V = {"now": now}
    for k, v in rec.items():
        V[f"api_account_response.data.{lf}.0.{k}"] = v
    if idf not in rec:
        V[f"api_account_response.data.{lf}.0.{idf}"] = "x1"
    raws = engine.read_products(V)
    return V, engine.product_facts(V, raws[0])


def offers(spec, now, rec):
    """Proposals in order after the opening 'full amount today' ask, when the customer flatly refuses each."""
    V, p = facts(spec, now, rec)
    if p["status"] != "open":
        return []
    seq = []
    s = {"products": [p], "ci": 0, "stage": "nego", "phase": "items"}
    saved = engine.say, engine.talk, engine.advance
    engine.say = lambda V_, *a: None
    engine.talk = lambda V_, stage, s_: None
    engine.advance = lambda V_, s_, pre="": None
    try:
        p["used"] = 0
        engine.make_offer(V, s, p, 1)
        for _ in range(12):
            if not p.get("offer"):
                break
            seq.append([round(p["offer"][0], 2), p["offer"][1]])
            engine.refusal(V, s, p)
    finally:
        engine.say, engine.talk, engine.advance = saved
    return seq


def question(spec, field):
    for q in spec["identity"]["questions"]:
        if q.get("field") == field:
            return q
    return None


def blank(x):
    return None if x in (None, "") else x


def answer(spec, c):
    t, i = c["type"], c.get("input") or {}
    events = spec.get("events") or {}

    def ev(sit):
        # the engine offers the brief's hardship transfer through the transfer_support event; when that event has a
        # queue, a hardship call ends there (transfer, its close codes, no other note)
        if sit == "hardship" and (events.get("transfer_support") or {}).get("queue"):
            return events["transfer_support"]
        return events.get(sit) or {}
    if t == "deadline":
        _, p = facts(spec, i["now"], i["product"])
        return p["deadline"] if p["status"] == "open" else None
    if t == "floor":
        return round(facts(spec, i["now"], i["product"])[1]["floor"], 2)
    if t == "attempts":
        return len(offers(spec, i["now"], i["product"]))
    if t == "offers":
        return offers(spec, i["now"], i["product"])
    if t == "business_day":
        return cal.is_business_day(cal.datetime.date.fromisoformat(i["date"]))
    if t == "callback_ok":
        return bool(engine.callback_ok({"now": i["now"]}, cal.datetime.date.fromisoformat(i["date"]), i["time"]))
    if t == "close":
        e = ev(i["situation"])
        return {"outcome": e.get("outcome"), "reason": e.get("reason") or ""}
    if t == "note":
        return blank(ev(i["situation"]).get("note"))
    if t == "transfer":
        return blank(ev(i["situation"]).get("queue"))
    if t == "identity_fields":
        return [q.get("field") for q in spec["identity"]["questions"]]
    if t == "identity_max_wrong":
        idn = spec["identity"]
        return idn.get("max_wrong") or (None if idn.get("max_wrong_per_question") else 3)
    if t == "id_answer":
        q = question(spec, i["field"])
        if not q:
            return None
        norm = engine.normalize_answer(q, str(i["value"]))          # what the engine makes of the answer it heard
        return engine.format_answer(q, norm if norm not in ("", "partial") else str(i["value"]))
    if t == "promise_amount":
        return engine.promise_amount({"agreed": [float(i["amount"]), ""]})
    raise ValueError("unknown check type " + t)


def same(got, exp):
    if isinstance(exp, (int, float)) and not isinstance(exp, bool) and isinstance(got, (int, float)) and not isinstance(got, bool):
        return abs(float(got) - float(exp)) < 0.006
    if isinstance(exp, list) and isinstance(got, list):
        return len(exp) == len(got) and all(same(g, e) for g, e in zip(got, exp))
    if isinstance(exp, dict) and isinstance(got, dict):
        return all(same(got.get(k) if k != "reason" else (got.get(k) or ""), v if k != "reason" else (v or "")) for k, v in exp.items())
    return got == exp


def score(spec, pkg):
    pkg = pathlib.Path(pkg)
    key = json.load(open(pkg / "key.json"))
    openapi = json.load(open(pkg / "openapi.json"))
    try:
        spec = setup(spec, openapi)
    except Exception as e:
        return dict(ok=0, total=len(key["checks"]), accuracy=0.0, missed_rules=sorted(key["rules"]),
                    failed=[dict(id="setup", error=f"{type(e).__name__}: {e}")])
    failed, ok = [], 0
    for c in key["checks"]:
        try:
            got = answer(spec, c)
        except Exception as e:
            got = f"ERROR {type(e).__name__}: {e}"
        if same(got, c["expect"]):
            ok += 1
        else:
            failed.append(dict(id=c["id"], rule=c["rule"], type=c["type"], expect=c["expect"], got=got))
    missed = sorted({f["rule"] for f in failed})
    return dict(ok=ok, total=len(key["checks"]), accuracy=round(ok / len(key["checks"]), 4),
                rules=len(key["rules"]), missed_rules=missed, failed=failed)


if __name__ == "__main__":
    print(json.dumps(score(json.load(open(sys.argv[1])), sys.argv[2]), indent=1, default=str))
