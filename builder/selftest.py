"""Self-tests a builder run can afford inside its time budget:
  * run_all: the in-agent engine against synthetic customers derived from the spec, with a generic fake bank.
  * chat_smoke: a few turns over Freya's chat route to prove the published workflow starts and speaks.
"""
import copy
import datetime
import json
import random
import re

from builder.runtime import cal, engine

KIT_OPENAPI = __import__("pathlib").Path(__file__).resolve().parent.parent / "kit" / "build-the-builder" / "tools" / "openapi.json"

API_SLOTS = {
    "api_verify": ("verify_identity", ["q_field", "q_answer"]),
    "api_account": ("get_account", []),
    "api_promise": ("create_promise", ["p_product", "p_amount", "p_date"]),
    "api_note": ("add_note", ["n_product", "n_reason"]),
    "api_callback": ("schedule_callback", ["cb_date", "cb_time", "cb_datetime"]),
    "api_transfer": ("transfer_to_agent", ["t_queue"]),
    "api_close": ("close_call", ["c_outcome", "c_reason"]),
    "api_close_nr": ("close_call", ["c_outcome"]),
}


class GenericBank:
    def __init__(self, spec, identity, products, faults=None):
        self.spec, self.identity, self.products = spec, identity, products
        self.faults = dict(faults or {})
        self.R = spec["identity"].get("response", {})
        self.led = dict(promises=[], notes=[], callbacks=[], transfers=[], outcome=None, reason=None, calls=[])
        self.ok_fields, self.wrong = set(), 0

    def ok(self, tool):
        """The success flag in the shape the spec expects for this tool (e.g. ok: true, status: 'created')."""
        r = self.spec.get("responses", {}).get(tool) or {"field": "ok", "values": ["true"]}
        if not r.get("field"):
            return {}
        v = (r.get("values") or ["true"])[0]
        return {r["field"]: True if str(v).lower() == "true" else v}

    def verdict(self, good):
        pv = self.R.get("pass_values")
        if pv:
            return pv[0] if good else "fail"
        return bool(good)

    def call(self, tool, a):
        self.led["calls"].append(tool)
        if self.faults.get(tool, 0) > 0:
            self.faults[tool] -= 1
            return None
        R = self.R
        if tool == "verify_identity":
            if str(a["q_answer"]) == str(self.identity.get(a["q_field"])):
                self.ok_fields.add(a["q_field"])
                return {R.get("ok", "ok"): True, R.get("verified", "verified"): self.verdict(True),
                        R.get("all_verified", "all_verified"): self.verdict(len(self.ok_fields) == len(self.identity)),
                        R.get("left", "wrong_answers_left"): self.spec["identity"].get("max_wrong", 3) - self.wrong}
            self.wrong += 1
            return {R.get("ok", "ok"): True, R.get("verified", "verified"): self.verdict(False), R.get("all_verified", "all_verified"): self.verdict(False),
                    R.get("left", "wrong_answers_left"): self.spec["identity"].get("max_wrong", 3) - self.wrong}
        if tool == "get_account":
            return dict(self.ok(tool), **{self.spec["products"].get("list_field", "products"): copy.deepcopy(self.products)})
        if tool == "create_promise":
            self.led["promises"].append((a["p_product"], round(float(a["p_amount"]), 2), a["p_date"]))
        elif tool == "add_note":
            self.led["notes"].append((a["n_product"], a["n_reason"]))
        elif tool == "schedule_callback":
            self.led["callbacks"].append((a.get("cb_date") or str(a.get("cb_datetime", ""))[:10],
                                          a.get("cb_time") or str(a.get("cb_datetime", ""))[11:16]))
        elif tool == "transfer_to_agent":
            self.led["transfers"].append(a["t_queue"])
        elif tool == "close_call":
            self.led["outcome"], self.led["reason"] = a["c_outcome"], a.get("c_reason") or ""
        return dict(self.ok(tool), done=True)


def flatten(prefix, obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            flatten(prefix + "." + k, v, out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            flatten(prefix + "." + str(i), v, out)
    else:
        out[prefix] = "true" if obj is True else "false" if obj is False else obj


# ------------------------------------------------------------------ the compiled workflow, walked like the platform
def cond_true(c, V):
    if c.get("operator") == "always":
        return True
    if "conditions" in c:
        vals = [cond_true(x, V) for x in c["conditions"]]
        return all(vals) if c.get("logic") == "and" else any(vals)
    v, op, x = V.get(c.get("variable")), c.get("operator"), c.get("operand")
    if op == "equals":
        return v is not None and str(v) == str(x)
    if op == "not_equals":
        return v is None or str(v) != str(x)
    if op == "greater_than":
        try:
            return float(v) > float(x)
        except (TypeError, ValueError):
            return False
    return False


def route(node, V):
    for t in node.get("transitions", []):
        if cond_true(t["condition"], V):
            return t["target_node"]
    return node.get("passthrough_default_transition")


def resolve(tmpl, V):
    if isinstance(tmpl, str):
        m = re.fullmatch(r"\{\{([a-zA-Z_0-9.]+)\}\}", tmpl)
        if m:
            return V.get(m.group(1)), m.group(1)
        return re.sub(r"\{\{([a-zA-Z_0-9.]+)\}\}", lambda g: str(V.get(g.group(1), "")), tmpl), None
    return tmpl, None


def walk(wf, spec, V, bank_call, turns, feed, max_steps=400):
    """Run a call through the compiled workflow: the step node runs the engine, api nodes call the bank with the
    compiled request body, transitions are evaluated as the platform does. Returns the transcript."""
    engine.S = spec
    cal.P = dict(workdays=spec["calendar"]["workdays"], holidays=spec["calendar"]["holidays"])
    turns = list(turns)
    tr = [("agent", "<opening>")]
    u = turns.pop(0)
    tr.append(("customer", json.dumps(u)))
    feed("open", u)
    node = "step"
    for _ in range(max_steps):
        if node in (None, "end"):
            break
        n = wf["nodes"][node]
        if node == "step":
            engine.main(V)
            if V.get("engine_error"):
                raise RuntimeError("engine error: " + V["engine_error"])
            node = route(n, V)
            continue
        if n["node_type"] == "api_request":
            if n.get("on_enter", {}).get("type") == "tts":
                tr.append(("agent", resolve(n["on_enter"]["text"], V)[0]))
            fn = n["pre_actions"][0]["function"]
            tool = fn["name"]
            body, slots = {}, {}
            for f, d in fn["parameters"]["properties"]["body"]["properties"].items():
                val, var = resolve(d.get("default"), V)
                if var is not None and val in (None, ""):
                    continue                       # an unset single placeholder: the platform drops the field
                body[f] = float(val) if d.get("type") == "number" and val not in (None, "") else val
                if var:
                    slots[re.sub(r"_[23]$", "", var)] = body[f]
            for k in [k for k in V if k.startswith(node + "_response")]:
                del V[k]
            r = bank_call(tool, body, slots)
            if r is None:
                pass                               # as on the platform: a failed call leaves no variables, no counter
            else:
                V[node + "_success_count"] = (V.get(node + "_success_count") or 0) + 1
                flatten(node + "_response.data", r, V)
                V[node + "_response.status_code"] = 200
            node = route(n, V)
            continue
        if n["node_type"] == "conversation":
            tr.append(("agent", V.get("say", "")))
            if not turns and node == "talk_after":
                turns = [dict(k="done", t="no, that's all, thank you")]
            if not turns:
                tr.append(("customer", "<silence>"))
                break
            u = turns.pop(0)
            tr.append(("customer", json.dumps(u)))
            feed(node[5:].replace("open_again", "open"), u)
            node = route(n, V)
            continue
        break
    return tr


def simulate(spec, variables, bank, turns, max_steps=400, openapi=None):
    """turns: list of dicts k/t/d/a/f/side/cut (what extraction would produce). Returns (ledger, transcript)."""
    from builder import compile as comp
    if openapi is None:
        openapi = json.load(open(KIT_OPENAPI))
    spec = comp.apply_defaults(json.loads(json.dumps(spec)), openapi)
    wf = comp.compile_workflow(spec, openapi)
    bank.spec = spec
    V = dict(variables)

    def feed(stage, u):
        V.update({stage + "_kind": u.get("k", "none"), stage + "_text": u.get("t", ""), stage + "_date": u.get("d", ""),
                  stage + "_amount": u.get("a", ""), stage + "_side": u.get("side", "none"),
                  stage + "_for": u.get("f", "none"), stage + "_cut": u.get("cut", "no")})
        if stage == "id":
            V["id_norm"] = u.get("norm", "")

    tr = walk(wf, spec, V, lambda tool, body, slots: bank.call(tool, slots), turns, feed, max_steps)
    return bank.led, tr


# ------------------------------------------------------------------------------------------ synthetic data
def pick_now(spec):
    d = datetime.date(2026, 10, 14)
    cal.P = dict(workdays=spec["calendar"]["workdays"], holidays=spec["calendar"]["holidays"])
    while not cal.is_business_day(d):
        d += datetime.timedelta(days=1)
    return d


DIGIT_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]


def code_value(n):
    """A letters-then-digits code of length n (e.g. 8 -> AB123456), like a postcode or a customer reference."""
    n = int(n or 6)
    letters = "AB"[:min(2, n)]
    return letters + "".join(str((i % 9) + 1) for i in range(n - len(letters)))


def spoken_form(q, value):
    k = q.get("kind")
    if k == "date":
        return "the twenty-first of June nineteen eighty-five"
    if k == "year":
        return "nineteen eighty-five"
    if k == "email":
        return "alex dot morgan at example dot com"
    if k == "text":
        return "it's " + value
    if k in ("upper", "digits"):
        return "it's " + " ".join(DIGIT_WORDS[int(c)] if c.isdigit() else c for c in value)
    return value


def identity_values(spec):
    vals, wrong = {}, {}
    for q in spec["identity"]["questions"]:
        k = q.get("kind")
        if k == "date":
            fmt = q.get("format") or "%Y-%m-%d"
            vals[q["field"]], wrong[q["field"]] = datetime.date(1985, 6, 21).strftime(fmt), datetime.date(1979, 2, 3).strftime(fmt)
        elif k == "email":
            vals[q["field"]], wrong[q["field"]] = "alex.morgan@example.com", "x@y.com"
        elif k == "digits":
            n = int(q.get("length") or 4)
            vals[q["field"]], wrong[q["field"]] = "7" * n, "3" * n
        elif k == "upper":
            vals[q["field"]] = code_value(q.get("length") or 6)
            wrong[q["field"]] = "Z" + vals[q["field"]][1:]
            engine.S = spec                   # the bank stores codes in the tool's own format (e.g. 'AB1 2CD')
            vals[q["field"]] = engine.format_answer(q, vals[q["field"]])
            wrong[q["field"]] = engine.format_answer(q, wrong[q["field"]])
        elif k == "year":
            vals[q["field"]], wrong[q["field"]] = "1985", "1971"
        else:
            vals[q["field"]], wrong[q["field"]] = "Smith", "Jones"
    return vals, wrong


def make_product(spec, i, typ, days_late):
    P_ = spec["products"]
    p = {P_.get("id_field", "product_id"): "p%d" % (i + 1), P_.get("type_field", "type"): typ, P_.get("days_field", "days_late"): days_late,
         "card_last4": "4821", "account_last4": "1234", "last4": "1234"}
    full = 300.0 + 100 * i
    p[spec["products"].get("amount_field", "past_due")] = full
    ff = spec["products"].get("floor_field", {}).get(typ)
    if ff:
        p[ff] = round(full * 0.3, 2)
    return p


def expected(spec, event, promises=(), notes=(), callbacks=(), transfers=()):
    e = spec["events"][event]
    return dict(outcome=e.get("outcome"), reason=e.get("reason") or "", promises=sorted(promises), notes=sorted(notes),
                callbacks=sorted(callbacks), transfers=sorted(transfers))


def run_all(spec, openapi):
    out = []
    try:
        today = pick_now(spec)
        V0 = dict(customer_id="c_test", first_name="Alex", last_name="Morgan", title="Mr", now=today.isoformat() + "T11:00")
        ident, wrong = identity_values(spec)
        types = list(spec["products"]["names"].keys()) or ["product"]
        bands = spec["bands"]
        dl0 = int(bands[0].get("min", 1))
        single = [make_product(spec, 0, types[0], dl0)]
        C = dict(k="confirmed", t="yes that's me")
        answers = [dict(k="answer", t=ident[q["field"]]) for q in spec["identity"]["questions"]]
        notes_on = spec.get("notes", {}).get("enabled")
        T = spec["events"]["transfer_support"].get("queue")
        R = spec["events"]["transfer_restructuring"].get("queue")
        ev_ = spec["events"]

        def notes_for(event, pids=("p1",)):
            n = ev_.get(event, {}).get("note")
            return [(p, n) for p in pids] if notes_on and n else []
        spoken = [dict(k="answer", t=spoken_form(q, ident[q["field"]])) for q in spec["identity"]["questions"]]
        P_ = spec["products"]
        sent = lambda raw: round(raw / float(P_.get("amount_scale") or 1) * float(P_.get("promise_scale") or P_.get("amount_scale") or 1), 2)
        full = sent(single[0][P_.get("amount_field", "past_due")])
        first_date = today.isoformat()
        if spec.get("rules", {}).get("first_ask") == "deadline":
            engine.S = spec
            first_date = engine.product_facts(dict(now=V0["now"]), dict(single[0], type=types[0], product_id="p1"))["deadline"]
        cb = spec["callback"]
        nb = cal.next_business_day(today, include_self=False)
        start_h = int(cb.get("start", "10:00")[:2])
        cb_time = "%02d:00" % min(start_h + 1, int(cb.get("end", "19:00")[:2]))
        scenarios = [
            ("happy path, pays in full today", [C] + answers + [dict(k="yes"), dict(k="yes")], single,
             expected(spec, "promise", promises=[("p1", full, first_date)])),
            ("someone else answers, books a callback", [dict(k="third_party"), dict(k="gives_time", t="next business day at %s" % cb_time, d=nb.isoformat())], single,
             expected(spec, "callback", callbacks=[(nb.isoformat(), cb_time)])),
            ("wrong number", [dict(k="wrong_number")], single, expected(spec, "wrong_number")),
            ("identity fails", [C] + [dict(k="answer", t=wrong[spec["identity"]["questions"][0]["field"]])] * int(spec["identity"].get("max_wrong", 3)), single,
             expected(spec, "id_failed")),
            ("identity answered the way people speak", [C] + spoken + [dict(k="yes"), dict(k="yes")], single,
             expected(spec, "promise", promises=[("p1", full, first_date)])),
            ("already paid", [C] + answers + [dict(k="already_paid")], single, expected(spec, "paid", notes=notes_for("paid"))),
            ("dispute", [C] + answers + [dict(k="dispute")], single, expected(spec, "dispute", notes=notes_for("dispute"))),
            ("hardship, accepts support" if T else "hardship (no transfer in this brief)", [C] + answers + [dict(k="hardship"), dict(k="yes")], single,
             expected(spec, "transfer_support", transfers=[T]) if T else expected(spec, "hardship", notes=notes_for("hardship"))),
            ("restructuring request", [C] + answers + [dict(k="restructure")], single,
             expected(spec, "transfer_restructuring", transfers=[R] if R else [], notes=[] if R else notes_for("transfer_restructuring"))),
            ("flat refusals", [C] + answers + [dict(k="no")] * 6, single, expected(spec, "refused", notes=notes_for("refused"))),
            ("the bank fails to record the promise twice", [C] + answers + [dict(k="yes"), dict(k="yes")], single,
             expected(spec, "service_error", notes=notes_for("service_error")), dict(create_promise=2)),
            ("the callback booking fails once, then works", [dict(k="third_party"), dict(k="gives_time", t="next business day at %s" % cb_time, d=nb.isoformat())], single,
             expected(spec, "callback", callbacks=[(nb.isoformat(), cb_time)]), dict(schedule_callback=1)),
        ]
        if len(types) > 1:
            multi = [make_product(spec, i, t, dl0) for i, t in enumerate(types[:3])]
            fri = cal.ladder_date(today, "last_business_day_this_week", None) if False else None
            idf = spec["products"].get("id_field", "product_id")
            engine.S = spec

            def first_for(p):
                if spec.get("rules", {}).get("first_ask") != "deadline":
                    return today.isoformat()
                return engine.product_facts(dict(now=V0["now"]), dict(p, type=p.get(spec["products"].get("type_field", "type")), product_id=p[idf]))["deadline"]
            prom = [(p[idf], sent(p[spec["products"].get("amount_field", "past_due")]), first_for(p)) for p in multi]
            scenarios.append(("several products, all paid today", [C] + answers + [dict(k="yes"), dict(k="yes")], multi,
                              expected(spec, "promise", promises=prom)))
        for sc in scenarios:
            name, turns, prods, exp = sc[:4]
            try:
                bank = GenericBank(spec, ident, copy.deepcopy(prods), sc[4] if len(sc) > 4 else None)
                led, tr = simulate(spec, V0, bank, turns, openapi=openapi)
                got = dict(outcome=led["outcome"], reason=led["reason"] or "", promises=sorted(led["promises"]), notes=sorted(led["notes"]),
                           callbacks=sorted(led["callbacks"]), transfers=sorted(led["transfers"]))
                spoken = " ".join(t for w, t in tr if w == "agent")
                leaked = [x for x in re.findall(r"\{[a-z_]+\}", spoken)]
                ok = got == exp and led["calls"] and led["calls"][-1] == "close_call" and not leaked
                detail = "" if ok else (f"unfilled placeholders {leaked}; " if leaked else "") + f"got {got} expected {exp}; transcript: " + " | ".join(f"{w}: {t}" for w, t in tr)[:1500]
                out.append((name, bool(ok), detail))
            except Exception as e:
                out.append((name, False, f"{type(e).__name__}: {e}"))
    except Exception as e:
        out.append(("setup", False, f"{type(e).__name__}: {e}"))
    return out


def chat_smoke(freya, workflow_id, spec):
    """The published workflow runs the engine on the platform: after the customer confirms, the agent must ask
    the brief's first security question (that line comes from the code node), with nothing unfilled."""
    out = []
    v = dict(customer_id="c_smoke_test", first_name="Alex", last_name="Morgan", title="Mr", now="2026-10-14T11:00")
    ask = spec["identity"]["questions"][0]["ask"]
    for attempt in range(2):
        try:
            cid, replies = None, []
            for m in ["Hello?", "Yes, this is Alex Morgan speaking."]:
                body = {"workflowId": workflow_id, "message": m, "customerVariables": v}
                if cid:
                    body["callId"] = cid
                r = freya.api("POST", "/api/v2/chat/message", body)
                cid = r.get("callId", cid)
                replies.append(r.get("response", "") or "")
            try:
                freya.api("POST", "/api/v2/chat/end", {"callId": cid})
            except Exception:
                pass
            key = " ".join(re.findall(r"[a-z]+", ask.lower())[-4:])
            ran = key in " ".join(re.findall(r"[a-z]+", replies[-1].lower()))
            out.append(("the engine runs on the platform (asks the first security question)", ran, " / ".join(replies)[:600]))
            out.append(("no unfilled placeholder spoken", not any("{" in x for x in replies), ""))
            bad = [w for w in ("collection", "debt", "overdue", "past due") if any(w in x.lower() for x in replies)]
            out.append(("nothing about the debt before verification", not bad, ",".join(bad)))
            return out
        except Exception as e:
            err = str(e)[:300]
    out.append(("chat", False, err))
    return out
