"""Call state machine that runs INSIDE the phone agent, in the `step` code node, after every customer turn
and after every bank call.

Contract with the workflow (built by builder/compile.py):
  in : V (the Freya `vars` dict). Customer input arrives in u_kind / u_text / u_date / u_amount / u_scope,
       extracted by the talk node that just ran. Bank results arrive as flattened keys
       '<api node>_response.data.<field>' (e.g. 'api_verify_response.data.verified').
  out: V['say'] (spoken verbatim by the next node), V['next'] (node key to route to),
       slot variables baked into the next bank call (q_field, q_answer, p_product, ...).
Everything brief-specific lives in S (the spec), injected by the compiler. The weak agent model never
decides anything here; it only classifies what the customer said.

Sandbox rules: imports limited to json/re/datetime; the source must never contain a double brace pair.
"""
import datetime
import json
import re

try:                                   # local tests import the sibling module; on Freya the compiler
    from builder.runtime import cal    # concatenates cal.py above this file, so these names exist already
    from builder.runtime.cal import *  # noqa: F401,F403
    from builder.runtime.cal import _parse_spoken_date  # noqa: F401
except Exception:
    pass

S = dict()   # injected spec


# ----------------------------------------------------------------------------------------------- helpers
CTX = dict()   # customer names, filled once per step from the call variables


def L(key, **kw):
    """Spec line with {placeholders} filled (customer names always available). Never raises."""
    t = S.get("lines", dict()).get(key, "")
    for k, v in list(kw.items()) + [(k, v) for k, v in CTX.items() if k not in kw]:
        v = str(v)
        if k == "amount" and v.startswith("the ") and " of " in v and "{amount}" in t:
            before = t.split("{amount}")[0][-30:].lower()
            label_words = v[4:].split(" of ")[0].split()
            if re.search(r"minimum|lowest|least|floor", before) or any(w in before for w in label_words):
                v = v.split(" of ", 1)[1]          # the line already names the floor
        t = t.replace("{" + k + "}", v)
    t = re.sub(r"\{[a-z_0-9]+\}", "", t)       # never speak an unfilled placeholder
    return re.sub(r"\s+", " ", t).strip()


def money(x):
    s = speak_amount(x)
    w = S.get("currency_word", "")
    return (s + " " + w).strip()


def floor_label(p):
    """'minimum payment' / 'instalment amount': what the floor is called, so offers say 'the minimum payment of 126 dollars'."""
    lab = by_type(S["products"].get("floor_names"), p["type"])
    if not lab:
        ff = by_type(S["products"].get("floor_field"), p["type"])
        lab = ff.replace("_", " ") if ff else ""
    return lab


def pay(p, amt):
    """Spoken amount for an offer: names the floor when the amount is the floor."""
    if p and abs(float(amt) - p["floor"]) < 0.005 and p["floor"] < p["full"] - 0.005 and floor_label(p):
        return "the " + floor_label(p) + " of " + money(amt)
    return money(amt)


def say(V, *parts):
    pre = V.pop("_prefix", "") if "_prefix" in V else ""
    V["say"] = " ".join(p.strip()[:1].upper() + p.strip()[1:] for p in (pre,) + parts if p and p.strip())


def jget(V, key, default=None):
    v = V.get(key)
    if v is None or v == "":
        return default
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def st(V):
    return jget(V, "_state", None) or dict()


def put(V, state):
    V["_state"] = json.dumps(state)


def truthy(x):
    return str(x).strip().lower() in ("true", "1", "yes")


def resp(V, node, field):
    v = V.get(node + "_response.data." + field)
    if v is None:
        d = V.get(node + "_response.data")
        if isinstance(d, dict):
            v = d.get(field)
    return v


TOOL_OF = dict(api_verify="verify_identity", api_account="get_account", api_promise="create_promise", api_note="add_note",
               api_callback="schedule_callback", api_transfer="transfer_to_agent", api_close="close_call", api_close_nr="close_call",
               api_promise_2="create_promise", api_promise_3="create_promise", api_note_2="add_note", api_note_3="add_note")
PASS_WORDS = ("true", "1", "yes", "ok", "success", "successful", "succeeded", "pass", "passed", "verified", "match", "matched",
              "correct", "valid", "created", "recorded", "scheduled", "booked", "done", "accepted", "transferred", "queued",
              "added", "saved", "confirmed")


def http_failed(V, node):
    """The call itself failed (HTTP error, timeout, no body)."""
    code = str(V.get(node + "_response.status_code") or V.get(node + "_status_code") or "").strip()
    if code and not code.startswith("2"):
        return True
    has_data = any(k.startswith(node + "_response.data") and V[k] not in (None, "") for k in V)
    return not has_data


def succeeded(V, node):
    """Did the bank accept the call? The success flag comes from the spec (read from the tool API file);
    without one, HTTP 2xx with no error field counts as success."""
    if http_failed(V, node):
        return False
    R_ = S.get("responses", dict()).get(TOOL_OF.get(node, ""), dict()) or dict()
    f = R_.get("field", "ok")
    v = resp(V, node, f) if f else None
    if v is None or v == "":
        return not resp(V, node, "error")
    vals = [str(x).strip().lower() for x in (R_.get("values") or [])]
    if vals:
        return str(v).strip().lower() in vals
    return truthy(v)


def passed(v):
    """A verify answer matched: a boolean true, or one of the spec's pass values for a string field."""
    pv = [str(x).strip().lower() for x in S.get("identity", dict()).get("response", dict()).get("pass_values", []) or []]
    sv = str(v).strip().lower()
    if pv:
        return sv in pv or truthy(v)
    return sv in PASS_WORDS


def num(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except Exception:
        return None


def today_of(V):
    return parse_now(V.get("now")).date()


TODAY = [None]    # the call's date, set once per run: dated code changes in the brief read it


def ev(name):
    """Event -> dict(outcome, reason, queue, note), with the brief's dated changes applied (S['event_changes']:
    [{"event": "paid", "from": "YYYY-MM-DD", "set": {"outcome": "..."}}])."""
    e = S.get("events", dict()).get(name, dict())
    for c in S.get("event_changes") or []:
        if c.get("event") == name and TODAY[0] is not None and iso(TODAY[0]) >= str(c.get("from", "9999-12-31")):
            e = dict(e, **(c.get("set") or {}))
    return e


# ----------------------------------------------------------------------------------------------- products
def read_products(V):
    P_ = S.get("products", dict())
    lf, idf, tf = P_.get("list_field", "products"), P_.get("id_field", "product_id"), P_.get("type_field", "type")
    out = read_products_raw(V, lf, idf)
    for p in out:                       # normalise to the engine's own keys
        p["product_id"] = p.get(idf, p.get("product_id"))
        t = p.get(tf, p.get("type"))
        p["type"] = "" if t is None else str(t)
    return out


def read_products_raw(V, lf, idf):
    out, i = [], 0
    while True:
        base = "api_account_response.data." + lf + "." + str(i) + "."
        pid = V.get(base + idf)
        if pid is None or pid == "":
            break
        p = dict()
        for k in list(V.keys()):
            if k.startswith(base):
                p[k[len(base):]] = V[k]
        out.append(p)
        i += 1
    if not out:
        raw = V.get("api_account_response.data." + lf)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = None
        if isinstance(raw, list):
            out = [x for x in raw if isinstance(x, dict)]
        elif isinstance(raw, dict) and raw.get(idf) not in (None, ""):
            out = [raw]
    if not out:                         # one product as an object (data.<list_field>.<field>, or data.<field>)
        for base in ("api_account_response.data." + lf + ".", "api_account_response.data."):
            if V.get(base + idf) not in (None, ""):
                p = dict()
                for k in list(V.keys()):
                    if k.startswith(base) and "." not in k[len(base):]:
                        p[k[len(base):]] = V[k]
                out = [p]
                break
    return out


def band_for(days_late):
    for b in S["bands"]:
        lo, hi = b.get("min", -10 ** 9), b.get("max", 10 ** 9)
        if lo <= days_late <= hi:
            return b
    rule = S.get("bands_outside") or dict()       # what the brief says for days_late in no band, if anything
    if rule.get("band"):
        for b in S["bands"]:
            if b.get("name") == rule["band"]:
                return b
    return None


def round_rule(x, rule):
    """The brief's rounding of a computed amount: up_unit / nearest_unit, or up:<step> / nearest:<step> / down:<step>
    (e.g. up:0.05). No rule: to the cent. Computed on the exact value, once."""
    x = round(x, 6)
    rule = {"up_unit": "up:1", "nearest_unit": "nearest:1"}.get(str(rule or ""), str(rule or ""))
    m = re.fullmatch(r"(up|nearest|down):([0-9.]+)", rule)
    if not m or float(m.group(2)) <= 0:
        return round(x, 2)
    step = float(m.group(2))
    q = x / step
    n = int(q)
    if m.group(1) == "up":
        n = n + (1 if q - n > 1e-6 else 0)
    elif m.group(1) == "nearest":
        n = int(q + 0.5 + 1e-9)
    return round(n * step, 2)


def by_type(m, typ, default=None):
    """Per-product-type spec value: the type's own entry, else '*', else the only entry."""
    m = m or dict()
    if typ in m:
        return m[typ]
    if "*" in m:
        return m["*"]
    if len(m) == 1:
        return list(m.values())[0]
    return default


def only_type(m, typ):
    """A rule stated for some product types applies to those types (or '*'), never to the others."""
    return (m or dict()).get(typ, (m or dict()).get("*"))


def product_facts(V, raw):
    today = today_of(V)
    typ = raw.get("type")
    typ = "" if typ is None else str(typ)
    names = S["products"]["names"]
    name = by_type(names, typ) or (typ.replace("_", " ") if typ else "account")
    for k, v in raw.items():
        name = name.replace("{" + k + "}", str(v))
    name = re.sub(r"\{[a-z_0-9]+\}", "", name).strip()
    scale = float(S["products"].get("amount_scale") or 1)
    full = (num(raw.get(S["products"].get("amount_field", "past_due"))) or 0.0) / scale
    ff = by_type(S["products"].get("floor_field"), typ)
    floor = num(raw.get(ff)) / scale if ff and num(raw.get(ff)) is not None else None
    pct = S["products"].get("floor_pct")
    pct = only_type(pct, typ) if isinstance(pct, dict) else pct          # one percentage, or one per product type
    if floor is None and pct is not None:
        fr = S["products"].get("floor_round")
        floor = round_rule(full * float(pct) / 100.0, only_type(fr, typ) if isinstance(fr, dict) else fr)
    if floor is None:
        floor = full
    fmin = S["products"].get("floor_min")
    fmin = only_type(fmin, typ) if isinstance(fmin, dict) else fmin
    if fmin is not None:
        floor = max(floor, float(fmin))           # "never below X" (and the full amount when that is below X)
    fbelow = S["products"].get("floor_full_below")
    fbelow = only_type(fbelow, typ) if isinstance(fbelow, dict) else fbelow
    if fbelow is not None and full < float(fbelow):
        floor = full                              # "below X the whole amount is due": only when the brief says so
    dl = int(num(raw.get(S["products"].get("days_field", "days_late"))) or 0)
    b = band_for(dl)
    if b is None:                                 # no band and no rule for it: no offer is made for this product
        ev_ = (S.get("bands_outside") or dict()).get("event") or "service_error"
        floor = min(floor, full)
        return dict(id=str(raw.get("product_id")), type=typ, name=name, full=full, floor=floor, days_late=dl,
                    band="", deadline=iso(today), attempts=0, ladder=None, used=0, made=0, too_much=False, floor_offered=False,
                    vague_asked=False, offer=None, agreed=None, status=ev_, reason="no_band", refused_offers=[])
    if b.get("floor") == "full" or typ in (b.get("floor_full_types") or []):
        floor = full                              # the band's own floor: the whole amount owed
    floor = min(floor, full)                      # a floor above the amount owed is the amount owed
    rule = (b.get("deadline_by_type") or dict()).get(typ) or b["deadline"]   # a product type with its own deadline
    deadline = band_deadline(today, rule)
    seen = cal_deadline(today, rule) if rule.get("roll_before_adjust") else deadline   # the brief rolls the raw date
    if (rule.get("roll_if_not_after_today") and seen <= today) or (rule.get("roll_if_before_today") and seen < today):
        nxt = today + datetime.timedelta(days=7 - today.weekday()) if rule.get("kind") in ("end_of_week", "end_of_next_week") \
            else end_of_month(today) + datetime.timedelta(days=1)          # the next period: week or month
        deadline = band_deadline(nxt, rule, today)
    return dict(id=str(raw.get("product_id")), type=typ, name=name, full=full, floor=floor, days_late=dl,
                band=b.get("name", ""), deadline=iso(deadline), attempts=int(b.get("attempts", 1)), ladder=b.get("ladder") or None,
                used=0, made=0, too_much=False, floor_offered=False, vague_asked=False, offer=None,
                agreed=None, status="open", reason="", refused_offers=[])


def cal_deadline(today, rule):
    return deadline(today, rule)


def band_deadline(base, rule, today=None):
    """The rule's date counted from `base`, moved off a non-business day as the brief says (never before today)."""
    today = today or base
    d = cal_deadline(base, rule)
    adj = rule.get("adjust", S.get("rules", dict()).get("deadline_adjust", "previous_business_day"))
    if adj != "none" and not is_business_day(d):
        if adj == "next_business_day":
            d = next_business_day(d)
        else:
            x = d
            while not is_business_day(x) and x > today:
                x = x - datetime.timedelta(days=1)
            d = x if is_business_day(x) else next_business_day(d)
    return d


# ----------------------------------------------------------------------------------------------- dates
def ladder_date(today, which, p):
    if which == "today":
        return today
    if which == "deadline":
        return datetime.date.fromisoformat(p["deadline"])
    if which == "last_business_day_this_week":
        d = last_business_day_of_week(today, 0)
        if d is None or d < today:                   # this week has no business day left
            d = last_business_day_of_week(today, 1)
        elif d == today and S.get("rules", dict()).get("week_end_today") == "next_week":
            d = last_business_day_of_week(today, 1)  # only when the brief says so (brief A: "if today is that day...")
        return d                                     # otherwise literally today: a repeat of the opening ask is skipped
    if which == "last_business_day_next_week":
        return last_business_day_of_week(today, 1)
    if which.startswith("business_days_after:"):
        return add_business_days(today, int(which.split(":")[1]))
    if which == "tomorrow":
        return today + datetime.timedelta(days=1)
    if which == "next_business_day":
        return next_business_day(today, include_self=False)
    if which.startswith("calendar_days_after:"):
        return today + datetime.timedelta(days=int(which.split(":")[1]))
    return datetime.date.fromisoformat(p["deadline"])


def check_date(V, p, d):
    """-> (status, date). status: ok | past | after_deadline | not_business (date = proposal to offer)."""
    today = today_of(V)
    dl = datetime.date.fromisoformat(p["deadline"])
    if d < today:
        return "past", None
    if d > dl:
        return "after_deadline", dl
    if not is_business_day(d) and S.get("rules", dict()).get("deadline_adjust") != "none":
        nb = next_business_day(d)
        if nb > dl:
            return "after_deadline", dl
        return "not_business", nb
    return "ok", d


def check_amount(p, a):
    if a is None:
        return "none", None
    a = round(float(a), 2)
    if a > p["full"] + 0.005:
        return "ok", p["full"]
    if a + 0.005 < p["floor"]:
        return "below_floor", p["floor"]
    return "ok", a


def spoken(d):
    if isinstance(d, str):
        d = datetime.date.fromisoformat(d)
    return speak_date(d)


# ----------------------------------------------------------------------------------------------- input
def customer_turns(V):
    """Customer turns over the whole call, from the platform's own counters (<node>.total_turns of the talk nodes),
    or None when they are not visible to the code node."""
    n, seen = 0, False
    for key, v in list(V.items()):
        if str(key).startswith("talk_") and str(key).endswith(".total_turns"):
            try:
                n += int(float(v or 0)); seen = True
            except (TypeError, ValueError):
                pass
    return n if seen else None


def fresh_answer(V, stage):
    """Without the counters: a talk node's values count as an answer only if something real is in them (the engine
    resets <stage>_kind to "none", _cut to "no" and _text to "" once it has used them)."""
    return (str(V.get(stage + "_kind") or "none").strip().lower() not in ("", "none") or
            bool(str(V.get(stage + "_text") or "").strip()) or str(V.get(stage + "_cut") or "no").strip().lower() == "yes")


def take_input(V, stage):
    """Extracted values of the talk node that just ran (variables are named per stage: <stage>_kind ...)."""
    g = lambda f: V.get(stage + "_" + f)
    k = str(g("kind") or "none").strip().lower()
    t = str(g("text") or "").strip()
    dt = str(g("date") or "").strip()
    a = num(g("amount"))
    sc = "none"
    fr = str(g("for") or "none").strip()
    if (stage + "_for") in V:
        V[stage + "_for"] = "none"
    types = dict((str(t).lower(), t) for t in S.get("products", dict()).get("names", dict()))
    if fr.lower() == "all":
        sc = "all"
    elif fr.lower() not in ("", "none") and fr.lower() in types:
        sc = "type:" + types[fr.lower()]
    V["_cut"] = str(g("cut") or "no").strip().lower()
    if (stage + "_cut") in V:
        V[stage + "_cut"] = "no"
    side = str(g("side") or "none").strip().lower()
    V["_side"] = side
    if (stage + "_side") in V:
        V[stage + "_side"] = "none"
    if side != k and side in ("robot", "scam") and not (stage in ("open", "tp") and k == "unclear"):
        V["_prefix"] = L(side)
    elif side == "question" and k in ("proposes", "too_much", "vague") and stage == "nego":
        V["_prefix"] = L("no_info")
    V["_norm"] = str(g("norm") or "").strip()
    for f in ("text", "date", "norm"):
        if (stage + "_" + f) in V:
            V[stage + "_" + f] = ""
    if (stage + "_amount") in V:
        V[stage + "_amount"] = 0
    if (stage + "_kind") in V:
        V[stage + "_kind"] = "none"
    if a == 0:
        a = None
    return k, t, dt, a, sc


def go(V, node):
    V["next"] = node


TEXT_STAGES = ("confirm",)    # the stage whose answer authorises a write; its _text is the customer message copied


def talk(V, stage, s):
    if stage in TEXT_STAGES:
        V[stage + "_text"] = ""                        # filled again only by a new customer message (freshness)
    if not str(V.get("say", "")).strip():
        V["say"] = L("go_on") or "Sorry, could you say that again?"
    s["await"] = "talk"
    s["stage"] = stage
    put(V, s)
    go(V, "talk_open_again" if stage == "open" else "talk_" + stage)


def call(V, s, api, **slots):
    for k in list(V.keys()):            # stale results of an earlier call to the same node
        if k.startswith(api + "_response") or k == api + "_status_code":
            V[k] = ""
    for k, v in slots.items():
        V[k] = v
    s["await"] = api
    put(V, s)
    go(V, api)


# ----------------------------------------------------------------------------------------------- closing
def close(V, s, event, pre=""):
    e = ev(event)
    s["close_event"] = event
    V["c_outcome"] = e.get("outcome", "")
    V["c_reason"] = e.get("reason", "") or ""
    s["stage"] = "closing"
    say(V, pre, L("goodbye"))
    node = "api_close" if V["c_reason"] else "api_close_nr"
    s["await"] = node
    put(V, s)
    go(V, node)


def finish(V, s, pre=""):
    """End of a verified call: notes (if the brief uses them), then the outcome from the product states."""
    prods = s.get("products", [])
    s["pre"] = (s.get("pre", "") + " " + (pre or "")).strip()     # lines to say before goodbye, kept across note calls
    if S.get("notes", dict()).get("enabled") and (not s.get("transferred") or s.get("no_queue")):
        for p in prods:
            stt = p["status"]
            if s.get("transferred") and stt == "open":
                stt = "transfer_" + s["transferred"]
            if (stt in ("refused", "hardship", "dispute", "paid", "service_error") or stt.startswith("transfer_")) and not p.get("noted"):
                p["noted"] = True
                note_reason = ev(stt).get("note")
                if isinstance(note_reason, dict):      # the brief keeps one code per product type
                    note_reason = only_type(note_reason, p.get("type", ""))
                if not note_reason:            # the brief keeps no note for this outcome
                    continue
                return call(V, s, "api_note", n_product=p["id"], n_reason=note_reason)
    if s.get("transfer_after_notes"):
        kind, line = s.pop("transfer_after_notes")
        s["pre"] = ""
        return transfer(V, s, kind, line)
    pre = s.pop("pre", "")
    if s.get("transferred"):
        return close(V, s, "transfer_" + s["transferred"], pre)
    if any(p["status"] == "recorded" for p in prods):
        return close(V, s, "promise", pre)
    reasons = [p["status"] for p in prods]
    if reasons and all(r == "paid" for r in reasons):
        return close(V, s, "paid", pre)
    order = (S.get("multi") or dict()).get("outcome_priority") or []
    for r in [x for x in order if x in reasons] + reasons:          # the brief's ranking first, then account order
        if r != "paid" and r in S.get("events", dict()):
            return close(V, s, r, pre)
    return close(V, s, "refused", pre)


# ----------------------------------------------------------------------------------------------- stages
def transfer(V, s, kind, line):
    """kind: human | restructuring | support. A transfer happens only when the brief names a queue for it;
    otherwise the call ends with that event (and its note, if the brief keeps notes)."""
    s["transferred"] = kind
    q = ev("transfer_" + kind).get("queue")
    if not q:
        s["no_queue"] = True
        alt = L(kind + "_no_transfer")
        return finish(V, s, alt or ("" if kind == "human" else line))
    s["say_after_transfer"] = line
    return call(V, s, "api_transfer", t_queue=q)


def stage_init(V, s):
    s.update(stage="open", asks=1, questions=0, not_talk=0)
    say(V, L("opening"), L("confirm_ask", **names(V)))
    talk(V, "open", s)


def names(V):
    out = dict(title=V.get("title", ""), first_name=V.get("first_name", ""), last_name=V.get("last_name", ""))
    for k in S.get("call_variables", []) or []:          # any other call variable the brief's lines use
        if k not in out and k != "now":
            out[k] = str(V.get(k, "") or "")
    return out


def common_intents(V, s, k, again, verified):
    """Robot / scam / human / no-talk handled the same in every stage. Returns True if handled."""
    if k == "robot":
        say(V, L("robot"), again)
        talk(V, s["stage"], s)
        return True
    if k == "scam":
        say(V, L("scam"), again)
        talk(V, s["stage"], s)
        return True
    if k == "human":
        if not s.get("human_asked") and S.get("human", dict()).get("ask_first", True):
            s["human_asked"] = True
            s["human_pending"] = True
            say(V, L("human_ask_help"))
            talk(V, s["stage"], s)
            return True
        transfer(V, s, "human", L("transfer"))
        return True
    if k in ("not_talk", "refuse_talk"):
        s["not_talk"] = s.get("not_talk", 0) + 1
        if s["not_talk"] >= S.get("max_not_talk", 2):
            if verified:
                for p in s.get("products", []):
                    if p["status"] in ("open", "agreed"):
                        p["status"] = "refused"
                finish(V, s)
            else:
                close(V, s, "id_declined")
            return True
        say(V, L("not_talk_once"), again)
        talk(V, s["stage"], s)
        return True
    return False


def stage_open(V, s, k, t):
    again = L("confirm_ask", **names(V))
    if k in ("unclear", "none") and V.get("_side") in ("robot", "scam"):
        k = V["_side"]
    if k == "confirmed":
        return start_identity(V, s)
    if k == "third_party":
        if not S.get("callback", dict()).get("days", ["today"]):   # the brief allows no callback: no time is asked
            return close(V, s, "no_callback", L("tp_no_info"))
        s["stage"] = "tp"
        s["tp_tries"] = 0
        say(V, L("tp_ask"))
        return talk(V, "tp", s)
    if k == "wrong_number":
        return close(V, s, "wrong_number", L("wrong_number"))
    if k == "deceased":
        return close(V, s, "deceased", L("condolence"))
    if k in ("robot", "scam", "purpose", "question"):
        s["asks"] = s.get("asks", 1) + 1
        line = dict(robot=L("robot"), scam=L("scam"), purpose=L("purpose_before_verify")).get(k, L("no_info"))
        if s["asks"] > S["confirm"]["max_asks"]:
            return close(V, s, "unclear")
        say(V, line, again)
        return talk(V, "open", s)
    if common_intents(V, s, k, again, False):
        return
    # unclear / hello / anything else
    if s.get("asks", 1) >= S["confirm"]["max_asks"]:
        return close(V, s, "unclear")
    s["asks"] = s.get("asks", 1) + 1
    say(V, again)
    return talk(V, "open", s)


def stage_tp(V, s, k, t, dtxt):
    today = today_of(V)
    if k == "wrong_number":
        return close(V, s, "wrong_number", L("wrong_number"))
    if k == "deceased":
        return close(V, s, "deceased", L("condolence"))
    if k == "customer_now":   # the customer came to the phone
        s["asks"] = 1
        say(V, L("confirm_ask", **names(V)))
        return talk(V, "open", s)
    if k in ("purpose", "offers_pay", "question", "robot", "scam"):
        s["tp_side"] = s.get("tp_side", 0) + 1
        if s["tp_side"] > 4:                            # keeps asking instead of giving a time
            return no_callback(V, s)
        line = L("robot") if k == "robot" else (L("scam") if k == "scam" else L("tp_no_info"))
        say(V, line, L("tp_ask"))
        return talk(V, "tp", s)
    if k in ("gives_time", "unclear", "none") and (dtxt or t):
        d, hm = parse_callback(dtxt + " " + t, today, V)
        if d is None and s.get("cb_day"):
            d = datetime.date.fromisoformat(s["cb_day"])      # day said in an earlier turn
        if hm is None and s.get("cb_time"):
            hm = s["cb_time"]
        if d is None and hm is not None and callback_ok(V, today, hm):
            d = today                                          # a time alone means today when that is allowed
        if d is not None and hm is None:                       # ask for the missing time without using a try
            s["cb_day"] = iso(d)
            if not s.get("cb_asked_time"):
                s["cb_asked_time"] = True
                say(V, L("tp_ask_time"))
                return talk(V, "tp", s)
        if hm is not None and d is None:
            s["cb_time"] = hm
            if not s.get("cb_asked_day"):
                s["cb_asked_day"] = True
                say(V, L("tp_ask"))
                return talk(V, "tp", s)
        if d is None and hm is None and re.search(r"\bnext (week|month)\b|\b(at|on|over) the weekend\b|\bin (a|two|three|a few|few|a couple of) (weeks?|months?)\b", (dtxt + " " + t).lower()):
            s["tp_tries"] = s.get("tp_tries", 0) + 1  # a period outside the callback days: an outside answer
            if s["tp_tries"] >= S["callback"].get("max_asks", 2):
                return no_callback(V, s)
            say(V, L("tp_outside_window"))
            return talk(V, "tp", s)
        if d is None and hm is None:                 # small talk, no day or time: ask again without using a try
            s["tp_empty"] = s.get("tp_empty", 0) + 1
            if s["tp_empty"] >= 5:
                return no_callback(V, s)
            say(V, L("tp_ask"))
            return talk(V, "tp", s)
        if d is not None and hm is not None:
            d = night_date(d, hm, dtxt + " " + t)
        ok = d is not None and hm is not None and callback_ok(V, d, hm)
        if ok:
            s["cb"] = [iso(d), hm]
            s["cb_booked"] = L("tp_booked", date=spoken(d), time=hm)     # said only after the bank confirms the booking
            return call(V, s, "api_callback", cb_date=iso(d), cb_time=hm, cb_datetime=iso(d) + "T" + hm)
        s["tp_tries"] = s.get("tp_tries", 0) + 1
        s.pop("cb_day", None)
        s.pop("cb_time", None)
        if s["tp_tries"] >= S["callback"].get("max_asks", 2):
            return no_callback(V, s)
        say(V, L("tp_outside_window"))
        return talk(V, "tp", s)
    if k in ("no_time", "declines"):
        return no_callback(V, s)
    say(V, L("cb_ask") if s.get("cb_event") else L("tp_ask"))
    return talk(V, "tp", s)


def no_callback(V, s, pre=""):
    if s.get("cb_event") and s.get("products"):     # a verified customer gave no usable time: no promise today
        s.pop("cb_event", None)
        for p in s["products"]:
            if p["status"] in ("open", "agreed"):
                p["status"] = "refused"
        return finish(V, s, pre)
    return close(V, s, "no_callback", pre)


def parse_callback(text, today, V):
    """(date, 'HH:MM') of a callback request; a correction ('ten, sorry, eleven') wins."""
    full = str(text or "").lower()
    corr = after_correction(full).lower()
    hm = spoken_time(corr) if corr != full else None
    if hm is None and corr != full:      # "at ten, sorry, eleven": a bare hour replaces the time it corrects
        m = re.fullmatch(r"\s*(?:at\s+)?(\d{1,2}|" + "|".join(HOUR_WORDS) + r")(\s+.*)?", corr)
        if m and spoken_time(CORRECTION.split(full)[0]):
            hm = spoken_time("at " + corr)
    hm = hm or spoken_time(full)
    d = None
    explicit = r"\b(today|tomorrow|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{1,2}(st|nd|rd|th)?\s+[a-z]{3,}|next|this)\b"
    def strip_times(x):
        x = re.sub(r"\b\d{1,2}[:.]\d{2}\s*(am|pm|a\.m\.|p\.m\.)?", " ", x)
        x = re.sub(r"\b\d{1,2}\s*(am|pm|a\.m\.|p\.m\.|o'clock|oclock)\b", " ", x)
        x = re.sub(r"\b(at|around|about|after|before|by|from|until|till)\s+\d{1,2}\b(?!\s*(st|nd|rd|th))", " ", x)
        return x
    full_d, corr_d = strip_times(full), strip_times(corr)
    for part in ([corr_d] if corr != full and re.search(explicit, corr) else []) + [full_d]:
        d = _parse_spoken_date(part, today, S.get("date_rules"))     # the correction was already applied above
        if d is None and re.search(r"\b(this )?(morning|afternoon|evening|later|tonight)\b", part) and not re.search(r"tomorrow", part):
            d = today
        if d is not None:
            break
    return d, hm


def callback_ok(V, d, hm):
    c = S["callback"]
    now = parse_now(V.get("now"))
    today = now.date()
    allowed = []
    for k in c.get("days", ["today", "next_business_day"]):
        if k == "today":
            allowed.append(today)
        elif k == "next_business_day":
            allowed.append(next_business_day(today, include_self=False))
        elif k == "tomorrow":
            allowed.append(today + datetime.timedelta(days=1))
        elif k.startswith("within_business_days:"):
            n = int(k.split(":")[1])
            x = today
            for _ in range(n):
                x = next_business_day(x, include_self=False)
                allowed.append(x)
        elif k.startswith("within_days:"):
            for i in range(1, int(k.split(":")[1]) + 1):
                allowed.append(today + datetime.timedelta(days=i))
    mins = lambda x: int(x[:2]) * 60 + int(x[3:5])      # validated HH:MM values only (see spec_model)
    t, a_, b_ = mins(hm), mins(c.get("start", "00:00")), mins(c.get("end", "23:59"))
    wins = c.get("windows") or dict()
    if str(d.weekday()) in wins:                          # this weekday has its own window (null = no callbacks)
        w = wins[str(d.weekday())]
        if not w:
            return False
        a_, b_ = mins(w["start"]), mins(w["end"])
    if not (a_ <= t <= b_ if a_ <= b_ else (t >= a_ or t <= b_)):     # start > end: the window crosses midnight
        return False
    # d is the calendar date of the call; the allowed days name the evening a night window starts on
    wday = d - datetime.timedelta(days=1) if a_ > b_ and t <= b_ else d
    if wday not in allowed:
        return False
    if c.get("business_days_only") and not is_business_day(wday):
        return False
    if c.get("after_now", True) and (d, t) <= (today, now.hour * 60 + now.minute):
        return False
    return True


def night_date(d, hm, text):
    """'tonight at one' in a window that crosses midnight is the next calendar date."""
    c = S["callback"]
    a_, b_ = int(c.get("start", "00:00")[:2]) * 60 + int(c.get("start", "00:00")[3:5]), \
        int(c.get("end", "23:59")[:2]) * 60 + int(c.get("end", "23:59")[3:5])
    t = int(hm[:2]) * 60 + int(hm[3:5])
    if a_ > b_ and t <= b_ and re.search(r"\b(tonight|this evening|later tonight)\b", str(text).lower()):
        return d + datetime.timedelta(days=1)
    return d


def start_identity(V, s):
    s.update(stage="id", qi=0, wrong=0, declined=0, partial="", id_explained=False)
    q = S["identity"]["questions"][0]
    say(V, L("id_intro"), q["ask"])
    return talk(V, "id", s)


def stage_id(V, s, k, t):
    qs = S["identity"]["questions"]
    q = qs[s["qi"]]
    if k == "asks_why" and not s.get("why_answered"):
        s["why_answered"] = True          # explaining why the questions are needed is not a refusal
        say(V, L("id_why"), q["ask"])
        return talk(V, "id", s)
    purpose_declines = S["identity"].get("purpose_is_decline", True)
    if k in ("refuses", "asks_why") or (k == "purpose" and purpose_declines) or (k == "question" and S["identity"].get("question_is_decline")):
        s["declined"] = s.get("declined", 0) + 1
        if s["declined"] >= 2:
            return close(V, s, "id_declined", L("id_declined"))
        say(V, L("id_explain"), q["ask"])
        return talk(V, "id", s)
    if k in ("robot", "scam", "human", "not_talk"):
        if common_intents(V, s, k, q["ask"], False):
            return
    if k in ("wrong_person", "third_party"):      # not the customer after all: someone else is on the line
        s["stage"] = "tp"
        s["tp_tries"] = 0
        say(V, L("tp_ask"))
        return talk(V, "tp", s)
    kind = q.get("kind")
    ans = normalize_answer(q, t)                           # 1. the latest turn on its own
    if kind == "upper":
        nf0 = norm_fallback(q, V.get("_norm", ""))
        if nf0 and re.fullmatch(r"[A-Z0-9]{3,}", nf0) and (not q.get("length") or len(nf0) == int(q["length"])):
            ans = nf0
    if kind in ("text", "email"):
        nf0 = norm_fallback(q, V.get("_norm", ""))         # words: the extractor's clean value beats the raw sentence
        if nf0:
            ans = nf0
    if ans in ("", "partial"):
        nf = norm_fallback(q, V.get("_norm", ""))          # 2. the extractor's normalised value
        if nf and kind in ("date", "year") and not has_year(t + " " + s.get("partial", "")):
            nf = ""                                        # the extractor filled in a year nobody said
        if nf:
            ans = nf
    if ans in ("", "partial") and s.get("partial") and kind in ("digits", "date", "year"):
        both = normalize_answer(q, (s["partial"] + " " + t).strip())   # 3. the earlier fragment + this turn
        if both and both != "partial":
            ans = both
    # a date or year that did not parse but carries a piece of one (a day, a month, a year): wait for the rest
    looks_partial = bool(t) and kind in ("date", "year") and bool(re.search(
        r"\d|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|\b(first|second|third|[a-z]+th|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)\b", t.lower()))
    if (ans == "partial" or (not ans and looks_partial)) and len((s.get("partial", "") + " " + t).split()) < 16 and s.get("partial_used", 0) < 2:
        s["partial"] = (s.get("partial", "") + " " + t).strip()
        if ans != "partial":
            s["partial_used"] = s.get("partial_used", 0) + 1     # up to two 'go on's while pieces of a date arrive
        say(V, L("id_continue"))
        return talk(V, "id", s)
    s["partial"] = ""
    s["partial_used"] = 0
    if ans == "partial":
        ans = ""
    if not ans and k in ("unclear", "none") and not t:
        s["id_chat"] = s.get("id_chat", 0) + 1         # small talk, no answer in it: ask again, more patiently
        if s["id_chat"] < 4:
            say(V, q["ask"] if s["id_chat"] < 3 else (L("id_repeat") + " " + q["ask"]).strip())
            return talk(V, "id", s)
    if not ans:
        s["repeats"] = s.get("repeats", 0) + 1
        if s["repeats"] >= 4:
            return close(V, s, "id_failed", L("id_failed"))
        say(V, L("id_repeat"), q["ask"] if s["repeats"] < 2 else (L("id_format_hint") or q["ask"]))
        return talk(V, "id", s)
    s["repeats"] = 0
    s["last_answer"] = ans
    return call(V, s, "api_verify", q_field=q["field"], q_answer=format_answer(q, ans))


def format_answer(q, ans):
    """A date in the format the verify tool wants (identity.questions[].format, default YYYY-MM-DD)."""
    fmt = q.get("format")
    m = re.fullmatch(r"space_before_last:(\d+)", str(fmt or ""))
    if q.get("kind") == "upper" and m:
        c = re.sub(r"\s+", "", ans)
        n = int(m.group(1))
        return c[:-n] + " " + c[-n:] if len(c) > n else c
    if q.get("kind") == "date" and fmt and re.fullmatch(r"\d{4}-\d\d-\d\d", ans):
        try:
            return datetime.date.fromisoformat(ans).strftime(fmt)
        except Exception:
            return ans
    return ans


YEAR_SAID = re.compile(r"\b(19|20)\d\d\b|\bnineteen[\s-]+(ten|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|o|oh|zero|one|two|"
                       r"three|four|five|six|seven|eight|nine|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|"
                       r"eighteen|nineteen)\b|\btwo thousand\b|\btwenty[\s-]+(o|oh|zero|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)\b")


def has_year(t):
    return bool(YEAR_SAID.search(str(t or "").lower()))


def norm_fallback(q, norm):
    """The extractor's own normalisation, used only when our parser found nothing."""
    norm = str(norm or "").strip()
    if not norm:
        return ""
    kind = q.get("kind", "text")
    if kind == "date":
        return norm if re.fullmatch(r"(19|20)\d\d-\d\d-\d\d", norm) else ""
    if kind == "digits":
        d = re.sub(r"\D", "", norm)
        n = int(q.get("length") or 0)
        return d if d and (not n or len(d) == n) else ""
    if kind == "year":
        return norm if re.fullmatch(r"(19|20)\d\d", norm) else ""
    if kind == "upper":
        return re.sub(r"[^A-Za-z0-9]", "", norm).upper()
    if kind == "email":
        return spoken_email(norm)
    return strip_lead_in(norm)


LEAD_IN = r"^(?:(?:yes|yeah|sure|ok|okay|well|so|um+|uh+)[,.\s]+)*(?:it'?s|it is|that'?s|that is|my [a-z' ]{1,40}? is|i think it'?s)\s+"


def strip_lead_in(t):
    t = after_correction(str(t or "")).strip(" .,!")
    return re.sub(LEAD_IN, "", t, flags=re.I).strip(" .,!")


def spoken_email(t):
    t = strip_lead_in(t).lower()
    t = re.sub(r"\s+(at)\s+", "@", t)
    t = re.sub(r"\s+(dot)\s+", ".", t)
    t = re.sub(r"\s+(underscore)\s+", "_", t)
    t = re.sub(r"\s+(dash|hyphen)\s+", "-", t)
    return re.sub(r"\s+", "", t)


def normalize_answer(q, text):
    kind = q.get("kind", "text")
    if not text:
        return ""
    if kind == "date":
        order = S["identity"].get("date_order") or "dmy"
        d = digits_only(text)
        # eight digits only in an order the brief states: the answer format (%d%m%Y, %m%d%Y, %Y%m%d) or an explicit
        # identity.date_order. Without either, no digit order is assumed and the answer is asked for again.
        fm = re.fullmatch(r"%([dmY])%([dmY])%([dmY])", str(q.get("format") or ""))
        seq = "".join(fm.groups()) if fm and sorted(fm.groups()) == ["Y", "d", "m"] else \
            {"dmy": "dmY", "mdy": "mdY"}.get(S["identity"].get("date_order") or "", "")
        t_ = str(text).strip().lower()
        one_by_one = bool(re.fullmatch(r"(\d\s*)+", t_)) or bool(re.fullmatch(
            r"(oh|zero|one|two|three|four|five|six|seven|eight|nine)(\s+(oh|zero|one|two|three|four|five|six|seven|eight|nine))*", t_))
        if len(d) == 8 and one_by_one and seq:       # only when the answer is nothing but digits, never a written date
            pos, i = dict(), 0
            for c in seq:
                n = 4 if c == "Y" else 2
                pos[c], i = d[i:i + n], i + n
            day, mon, yr = pos["d"], pos["m"], pos["Y"]
            try:
                return datetime.date(int(yr), int(mon), int(day)).isoformat()
            except ValueError:
                return ""
        return parse_birth_date(text, order)
    if kind == "digits":
        d = digits_only(text)
        n = int(q.get("length") or 0)
        if n and 0 < len(d) < n:
            return "partial"
        if n and len(d) > n:
            d = d[-n:]
        return d
    if kind == "year":
        m = re.search(r"\b(19\d\d|20\d\d)\b", text)
        if m:
            return m.group(1)
        if year_is_prefix(text):
            return "partial"           # "nineteen..." with the rest still to come
        if re.fullmatch(r"\D*\b\d{2}\b\D*", text) and not re.search(r"nineteen|twenty|thousand", text.lower()):
            return "partial"           # '85': the century is missing
        n = words_to_number(text)
        if n and 1900 <= n <= 2100:
            return str(int(n))
        if not re.search(r"nineteen|twenty|two thousand|\b\d{2}\b", text.lower()):
            return ""
        b = parse_birth_date("1 january " + text)
        return b[:4] if b else ""
    if kind == "upper":
        code = spoken_code(after_correction(text))
        n = int(q.get("length") or 0)
        if n and 0 < len(code) < n:
            return "partial"
        m = re.fullmatch(r"space_before_last:(\d+)", str(q.get("format") or ""))
        if m and 0 < len(code) < int(m.group(1)) + 2:
            return "partial"           # only the first part of a postcode
        return code
    if kind == "email":
        return spoken_email(text)
    return strip_lead_in(text)


def after_verify(V, s):
    node = "api_verify"
    if s.get("qi", 0) + 1 >= len(S["identity"]["questions"]) and read_products(V):
        return after_account(V, s)          # the workflow went straight on to the account lookup
    qs = S["identity"]["questions"]
    R = S["identity"].get("response", dict())
    ok = resp(V, node, R.get("ok", "ok"))
    verified = resp(V, node, R.get("verified", "verified"))
    allv = resp(V, node, R.get("all_verified", "all_verified"))
    left = resp(V, node, R.get("left", "wrong_answers_left"))
    hint = resp(V, node, R.get("hint", "hint"))
    V["api_verify_response.data.hint"] = ""
    q = qs[s["qi"]]
    if http_failed(V, node):                       # HTTP error / timeout: nothing was checked, ask the bank again once
        s["verify_http"] = s.get("verify_http", 0) + 1
        if s["verify_http"] <= 1:
            return call(V, s, "api_verify")
        return close(V, s, "service_error", L("system_error") or "I'm sorry, our system is not responding right now. Please call us back later.")
    s["verify_http"] = 0
    vstr = str(verified).strip().lower()
    locked_words = ("locked", "blocked", "exhausted", "too_many", "too many", "ended")
    if any(w in vstr for w in locked_words):
        return close(V, s, "id_failed", L("id_failed"))
    rejects = [str(x).lower() for x in R.get("reject_values", []) or []] or ["unreadable", "invalid", "malformed", "error"]
    if (ok not in (None, "") and not truthy(ok) and not passed(verified)) or vstr in rejects:
        err = str(resp(V, node, "error") or "").lower()       # rejected without counting (e.g. unreadable answer)
        s["verify_rejects"] = s.get("verify_rejects", 0) + 1
        if any(w in err for w in locked_words) or s["verify_rejects"] >= 3:
            return close(V, s, "id_failed", L("id_failed"))
        say(V, L("id_repeat"), q["ask"])
        return talk(V, "id", s)
    if passed(allv) or (passed(verified) and s["qi"] + 1 >= len(qs)):
        return call(V, s, "api_account")
    if passed(verified):
        s["qi"] += 1
        s["id_chat"] = 0
        say(V, qs[s["qi"]]["ask"])
        return talk(V, "id", s)
    if hint and str(hint) in S["identity"].get("hints", dict()):
        say(V, S["identity"]["hints"][str(hint)])
        return talk(V, "id", s)
    s["wrong"] = s.get("wrong", 0) + 1
    wq = s.setdefault("wrong_q", dict())
    wq[str(s["qi"])] = wq.get(str(s["qi"]), 0) + 1
    lv = num(left)
    per_q = S["identity"].get("max_wrong_per_question")
    if (lv is not None and lv <= 0) or s["wrong"] >= S["identity"].get("max_wrong", 99 if per_q else 3) or \
            (per_q and wq[str(s["qi"])] >= int(per_q)):
        return close(V, s, "id_failed", L("id_failed"))
    say(V, L("id_wrong"), q["ask"])
    return talk(V, "id", s)


def after_account(V, s):
    raws = read_products(V)
    if not raws:
        n = s.get("acct_tries", 0) + 1
        s["acct_tries"] = n
        if n < 2:
            return call(V, s, "api_account")
        return close(V, s, "service_error", L("no_info"))
    prods = order_products([product_facts(V, r) for r in raws], raws)
    s["products"] = prods
    s["ci"] = -1
    outside = [p for p in prods if p.get("reason") == "no_band"]
    s["stage"] = "nego"
    lines = [L("disclose_intro")]
    for p in prods:
        lines.append(L("product_line", name=p["name"], amount=money(p["full"]), days=p["days_late"]))
    for p in outside:                             # say it plainly instead of negotiating outside the policy
        lines.append(L("no_band_line", name=p["name"]) or ("I can't arrange a payment for your " + p["name"] + " on this call."))
    if not any(p["status"] == "open" for p in prods):
        return finish(V, s, " ".join(x for x in lines if x))
    lines.append(ask_today_line(V, s))
    s["phase"] = "today"
    say(V, *lines)
    return talk(V, "nego", s)


def ask_today_line(V, s):
    prods = s.get("products") or []
    if len(prods) > 1:
        return L("ask_today_multi", amount=money(sum(p["full"] for p in prods)))
    if not prods:
        return L("ask_today")
    p = prods[0]
    focus(p)
    d = p["deadline"] if S.get("rules", dict()).get("first_ask") == "deadline" else iso(today_of(V))
    return L("ask_today", name=p["name"], amount=money(p["full"]), date=spoken(d))


def focus(p):
    """Make the product's fields available to every line until the next step."""
    if p:
        cur_amt = p["offer"][0] if p.get("offer") and p["offer"][0] else p["full"]
        CTX.update(name=p["name"], amount=money(cur_amt), days=p["days_late"], deadline=spoken(p["deadline"]),
                   floor=money(p["floor"]), full=money(p["full"]))


def cur(s):
    ci = s.get("ci", -1)
    if 0 <= ci < len(s["products"]):
        return s["products"][ci]
    return None


def next_open(s):
    for i, p in enumerate(s["products"]):
        if p["status"] == "open":
            return i
    return None


def ladder_of(p):
    return (p.get("ladder") if p else None) or S["ladder"]


def start_product(V, s, p, pre=""):
    """The first proposal for product p after the opening ask. With rules.opening_is_attempt the opening ask has
    already used attempt 1 (see the today-phase branch), so the next proposal is step used+1, and a product whose
    attempts are all used ends here."""
    if p.get("made", 0) > 0 and p["made"] >= p["attempts"]:
        p["status"] = "refused"
        p["offer"] = None
        return advance(V, s, pre)
    return make_offer(V, s, p, p.get("used", 0) + 1, pre)


def make_offer(V, s, p, step_idx, pre=""):
    """Persuasion attempt number step_idx (1-based)."""
    focus(p)
    today = today_of(V)
    ladder = ladder_of(p)
    if not ladder:                    # the brief allows no persuasion: no offer, the product ends as refused
        p["status"] = "refused"
        p["offer"] = None
        return advance(V, s, pre)
    if step_idx > len(ladder) and not S.get("rules", dict()).get("ladder_repeat_last"):
        p["used"] = step_idx              # the brief gives no step for this attempt: persuasion ends here
        p["status"] = "refused"
        p["offer"] = None
        return advance(V, s, pre)
    st_ = ladder[min(step_idx, len(ladder)) - 1]
    amt = p["full"]
    if st_.get("amount") == "floor" or (st_.get("amount") == "full_or_floor" and p["too_much"]):
        amt = p["floor"]
    elif str(st_.get("amount", "")).startswith("pct:"):
        amt = max(p["floor"], round_rule(p["full"] * float(st_["amount"][4:]) / 100.0, S["products"].get("step_round")))
    d = ladder_date(today, st_.get("date", "deadline"), p)
    dl = datetime.date.fromisoformat(p["deadline"])
    if not is_business_day(d) and S.get("rules", dict()).get("deadline_adjust") != "none":
        d = next_business_day(d)
    if d > dl:
        d = dl
    offer = [round(amt, 2), iso(d)]
    repeat_ok = step_idx > len(ladder) and S.get("rules", dict()).get("ladder_repeat_last")   # the brief says so
    full_barred = p["too_much"] and S.get("rules", dict()).get("too_much_skips_full") and round(amt, 2) >= round(p["full"], 2)
    if (offer in p["refused_offers"] and not repeat_ok) or full_barred:
        if S.get("rules", dict()).get("skipped_repeat_counts") is not False:
            p["made"] = p.get("made", 0) + 1     # a skipped step uses one of the band's proposals unless the brief says not
        if step_idx < len(ladder) and p.get("made", 0) < p["attempts"]:
            return make_offer(V, s, p, step_idx + 1, pre)
        p["used"] = step_idx                 # rule: never repeat an offer the customer already refused
        p["status"] = "refused"
        p["offer"] = None
        return advance(V, s, pre)
    p["used"] = step_idx                     # position in the ladder
    p["made"] = p.get("made", 0) + 1         # proposals made: what the band's attempts limit
    p["offer"] = offer
    s["unclear_n"] = 0
    floor_first = amt == p["floor"] and amt < p["full"] and not p.get("floor_note_said")
    if floor_first:
        p["floor_note_said"] = True
        p["floor_offered"] = True
    line = L("offer_floor_date" if floor_first else "offer", name=p["name"], amount=pay(p, amt), date=spoken(d))
    say(V, pre, line)
    return talk(V, "nego", s)


def refusal(V, s, p, pre=""):
    """A flat refusal / non-answer on product p: consume one persuasion attempt or give up."""
    if p.get("offer"):
        p["refused_offers"].append(p["offer"])
    if s.get("hardship_offered"):            # no persuasion after a hardship disclosure
        p["status"] = "hardship"
        p["offer"] = None
        return advance(V, s, pre)
    if p.get("made", 0) < p["attempts"]:
        return make_offer(V, s, p, p["used"] + 1, pre)
    p["status"] = "refused"
    p["offer"] = None
    return advance(V, s, pre)


def advance(V, s, pre=""):
    """Move to the next open product, or to the read-back, or finish."""
    i = next_open(s)
    if i is not None:
        s["ci"] = i
        p = s["products"][i]
        if p.get("offer") and p["offer"][1] and p["offer"] not in p["refused_offers"]:
            focus(p)                             # the customer talked about another product: this offer is still open
            say(V, pre, L("repeat_offer", name=p["name"], amount=pay(p, p["offer"][0]), date=spoken(p["offer"][1])))
            return talk(V, "nego", s)
        intro = L("next_product", name=p["name"], amount=money(p["full"])) if len(s["products"]) > 1 else ""
        return start_product(V, s, p, (pre + " " + intro).strip())
    agreed = [p for p in s["products"] if p["status"] == "agreed"]
    if agreed:
        return readback(V, s, pre)
    return finish(V, s, pre)


def readback_parts(s):
    parts = []
    for p in [p for p in s["products"] if p["status"] == "agreed"]:
        focus(p)
        extra = L("clamped_note", date=spoken(p["agreed"][1]), name=p["name"]) if p.get("clamped") else ""
        parts.append((L("readback_item", name=p["name"], amount=money(p["agreed"][0]), date=spoken(p["agreed"][1])) + " " + extra).strip())
    return parts


def readback(V, s, pre=""):
    parts = readback_parts(s)
    s["stage"] = "confirm"
    s["confirm_unclear"] = 0
    say(V, pre, L("readback_intro"), *(parts + [L("confirm_q")]))
    return talk(V, "confirm", s)


def agree(p, amount, date, clamped=False):
    p["asks"] = 0
    p["agreed"] = [round(float(amount), 2), date]
    p["status"] = "agreed"
    p["offer"] = None
    p["clamped"] = clamped


def apply_proposal(V, s, p, dtxt, a, scope_all=False):
    """Customer proposed a date and/or an amount for p. Returns (handled, pre_line)."""
    focus(p)
    today = today_of(V)
    amount = None
    if a is not None:
        a = snap_amount(p, a)
        if a + 0.005 < p["full"]:
            p["too_much"] = True          # offering less than the full amount: the next attempt is the floor
        stt, val = check_amount(p, a)
        if stt == "below_floor":
            if p["floor_offered"]:
                return "refuse", ""
            p["floor_offered"] = True
            p["floor_note_said"] = True
            p["too_much"] = True
            d0 = p["offer"][1] if p.get("offer") else None
            if dtxt:                                           # keep the customer's own date if it is allowed
                dd = parse_spoken_date(dtxt, today, S.get("date_rules"))
                if dd is not None:
                    st2, v2 = check_date(V, p, dd)
                    if st2 in ("ok", "not_business"):
                        d0 = iso(v2)
            if d0 is None:
                d0 = iso(ladder_date(today, ladder_of(p)[0].get("date", "deadline"), p))
            p["offer"] = [p["floor"], d0]
            return "ask", (L("below_floor", amount=pay(p, p["floor"]), name=p["name"]) + " " +
                           L("offer_short_date", date=spoken(d0))).strip()
        amount = val
    date = None
    if dtxt:
        d = parse_spoken_date(dtxt, today, S.get("date_rules"))
        if d is None:
            vague = True
        else:
            vague = False
            stt, val = check_date(V, p, d)
            if stt == "ok":
                date = iso(val)
            elif stt == "past":
                return "ask", L("date_past")
            elif stt == "not_business":
                p["offer"] = [amount if amount is not None else default_amount(p), iso(val)]
                return "ask", L("not_business_day", date=spoken(val))
            elif stt == "after_deadline":
                if scope_all and S.get("multi", dict()).get("clamp_single_date", True) and len(s["products"]) > 1:
                    date = iso(val)
                    p["clamped"] = True
                else:
                    p["after_deadline_offered"] = True
                    p["offer"] = [amount if amount is not None else default_amount(p), iso(val)]
                    return "ask", L("after_deadline", date=spoken(val), amount=pay(p, p["offer"][0]))
        if vague:
            if not p["vague_asked"]:
                p["vague_asked"] = True
                p["vague_pending"] = True
                return "ask", L("vague_ask")
            dl = datetime.date.fromisoformat(p["deadline"])
            p["offer"] = [amount if amount is not None else default_amount(p), iso(dl)]
            return "ask", L("propose_deadline", date=spoken(dl), amount=pay(p, p["offer"][0]))
    if amount is not None and date is not None:
        agree(p, amount, date, p.get("clamped", False))
        return "agreed", ""
    if date is not None:
        agree(p, p["offer"][0] if p.get("offer") and p["offer"][0] else default_amount(p), date, p.get("clamped", False))
        return "agreed", ""
    if amount is not None:
        if p.get("offer") and p["offer"][1]:
            agree(p, amount, p["offer"][1])
            return "agreed", ""
        p["offer"] = [amount, None]
        return "ask", L("ask_date_for_amount", amount=money(amount))
    return "none", ""


def snap_amount(p, a):
    """Spoken amounts lose cents ('six hundred thirty-seven' for 637.50): within 1 unit of the pending offer
    or of the floor/full amount, take that exact figure."""
    try:
        a = float(a)
    except Exception:
        return a
    if abs(a - round(a)) > 1e-9:
        return a                          # the customer said the pence: keep them
    targets = [p["offer"][0]] if p.get("offer") and p["offer"][0] else []
    targets += [p["floor"], p["full"]]
    for x in targets:
        if x and 0 <= float(x) - a < 1.0:
            return float(x)               # 'six hundred thirty-seven' for 637.50
    return a


def default_amount(p):
    return p["floor"] if p["too_much"] and p["floor_offered"] else p["full"]


def special(V, s, k, p, scope="none"):
    """Paid / dispute / hardship / restructure / call-me-later on product p (or the call). Returns True if handled."""
    focus(p)
    if k == "call_later" and ev("callback_requested").get("outcome"):
        s["stage"] = "tp"                  # the verified customer wants a callback: book one in the callback window
        s["cb_event"] = "callback_requested"
        s["tp_tries"] = 0
        say(V, L("cb_ask") or "When would be a good time to call you back?")
        talk(V, "tp", s)
        return True
    multi = len(s["products"]) > 1
    if multi and scope == "all" and k in ("already_paid", "dispute"):
        for q in s["products"]:
            if q["status"] in ("open", "agreed"):
                q["status"] = "paid" if k == "already_paid" else "dispute"
        advance(V, s, L("paid_line") if k == "already_paid" else L("dispute_line"))
        return True
    if k == "already_paid":
        if multi and S.get("multi", dict()).get("continue_after_paid", True):
            p["status"] = "paid"
            return advance(V, s, L("paid_line")) or True
        p["status"] = "paid"
        for q in s["products"]:
            if q["status"] == "open":
                q["status"] = "paid"
        finish(V, s, L("paid_line"))
        return True
    if k == "dispute":
        p["status"] = "dispute"
        if multi and S.get("multi", dict()).get("continue_after_dispute", True):
            advance(V, s, L("dispute_line"))
            return True
        for q in s["products"]:
            if q["status"] == "open":
                q["status"] = "dispute"
        finish(V, s, L("dispute_line"))
        return True
    if k == "hardship" and s.get("hardship_offered"):     # offered once already: no second offer, no persuasion
        if p is not None and p["status"] == "open":
            p["status"] = "hardship"
            p["offer"] = None
        if any(q["status"] == "agreed" for q in s["products"]) and s.get("stage") == "confirm":
            say(V, L("hardship_declined"), L("confirm_q"))
            talk(V, "confirm", s)
            return True
        advance(V, s, L("hardship_declined"))
        return True
    if k == "hardship":
        s["hardship_offered"] = True
        if S.get("rules", dict()).get("hardship_transfer") == "direct" and ev("transfer_support").get("queue"):
            for q in s["products"]:                         # the brief: sympathy, then transfer, no question
                if q["status"] in ("open", "agreed"):
                    q["status"] = "hardship"
            if S.get("rules", dict()).get("notes_before_transfer") and S.get("notes", dict()).get("enabled"):
                s["transfer_after_notes"] = ["support", L("hardship")]   # the brief: note each product, then transfer
                finish(V, s)
                return True
            transfer(V, s, "support", L("hardship"))
            return True
        if not ev("transfer_support").get("queue"):        # the brief offers no support transfer
            for q in s["products"]:
                if q["status"] == "open":
                    q["status"] = "hardship"
            if any(q["status"] == "agreed" for q in s["products"]):
                readback(V, s, L("hardship"))
            else:
                finish(V, s, L("hardship"))
            return True
        s["stage"] = "hardship"
        say(V, L("hardship"))
        talk(V, "hardship", s)
        return True
    if k == "restructure" and s.get("hardship_offered"):     # the hardship rule comes before restructuring
        if p is not None and p["status"] == "open":
            p["status"] = "hardship"
            p["offer"] = None
        advance(V, s, L("hardship_declined"))
        return True
    if k == "restructure":
        for q in s["products"]:
            if q["status"] == "agreed":
                q["status"] = "open"
        transfer(V, s, "restructuring", L("restructure_line"))
        return True
    return False


def product_target(s, scope, text=""):
    """Index of the product a statement names ('type:<product type>'), or None. Several products of the same
    type are told apart by words or digits of their spoken names that the customer said."""
    if scope == "any":                  # no product field in the extraction: only a clear match by name counts
        cands = [i for i, p in enumerate(s["products"]) if p["status"] in ("open", "agreed")]
    elif not scope.startswith("type:"):
        return None
    else:
        cands = [i for i, p in enumerate(s["products"]) if p["type"] == scope[5:] and p["status"] in ("open", "agreed")]
    if len(cands) > 1 and text:
        low = text.lower()
        digits = digits_only(text)
        toks = dict()
        for i in cands:
            toks[i] = set(re.findall(r"[a-z]+|\d+", s["products"][i]["name"].lower()))
        for i in cands:
            mine = toks[i] - set().union(*[toks[j] for j in cands if j != i])
            if any((x.isdigit() and x in digits) or (not x.isdigit() and len(x) > 2 and re.search(r"\b" + x + r"\b", low)) for x in mine):
                return i
    if scope == "any":
        return None
    return cands[0] if cands else None


def refuses_asked_day(V, s, dtxt, target):
    """A 'no' whose date is only the day the agent just asked about (the pending offer's day, or today / the
    deadline in the first question), or only a negated mention ('not Friday'). A vague period ('next week')
    or another day is a proposal (rules 21 and 29) and is kept."""
    dd = parse_spoken_date(dtxt, today_of(V), S.get("date_rules"))
    if dd is None:
        low = " " + str(dtxt).lower() + " "
        rest = re.sub(NEG_RE + r"(?:pay\s+|do\s+it\s+|make\s+it\s+)?(?:on\s+)?(?:this\s+(?:coming\s+)?|next\s+)?(?:" +
                      WEEKDAY_RE + r"s?|today|tomorrow|now|right now|this week)\b", " ", low)
        words = set(re.findall(r"[a-z0-9]+", rest)) - set(["i", "on", "the", "pay", "it", "that", "day", "either", "anyway", "no", "and", "but", "or", "really", "sorry"])
        return rest != low and not words
    if s.get("phase") == "today":
        first = S.get("rules", dict()).get("first_ask") == "deadline"
        asked = [q["deadline"] if first else iso(today_of(V)) for q in s["products"]]
        return iso(dd) in asked
    p = s["products"][target] if target is not None else cur(s)
    if not (p and p.get("offer") and p["offer"][1] == iso(dd)):
        return False
    if target is not None:                 # about one product: only a day the customer actually negated
        t = str(V.get("_t", "") or "").lower()
        return bool(re.search(NEG_RE + r"[^.,;!?]{0,30}" + re.escape(str(dtxt).lower()), t)) or not t
    return True


def order_products(prods, raws):
    """The brief's handling order (multi.order: a list of sort keys, the first decides), else the account's order.
    {"by": "type", "rank": [types in order]} | {"by": "amount"|"days"|"<record field>", "desc": true|false}"""
    keys = (S.get("multi") or dict()).get("order") or []
    if not keys:
        return prods
    raw_of = dict((id(p), r) for p, r in zip(prods, raws))

    def key(p):
        out = []
        for k in keys:
            by = k.get("by")
            if by == "type":
                rank = [str(x) for x in k.get("rank") or []]
                out.append(rank.index(p["type"]) if p["type"] in rank else len(rank))
                continue
            v = p["full"] if by == "amount" else p["days_late"] if by == "days" else num(raw_of[id(p)].get(by))
            v = float(v or 0)
            out.append(-v if k.get("desc") else v)
        return out
    return sorted(prods, key=key)                    # stable: ties keep the account's order


def pay_day(V, p):
    """'Yes, today': today, or on a call made on a non-business day the next business day (never past the deadline),
    as deadline_adjust says for every other promise date."""
    d = today_of(V)
    if not is_business_day(d) and S.get("rules", dict()).get("deadline_adjust") != "none":
        d = min(next_business_day(d), datetime.date.fromisoformat(p["deadline"]))
    return iso(d)


def consume_opening(V, prods):
    """The opening 'full amount today' ask was not accepted (a no, a counter-proposal, 'too much', a dispute on one
    product...). Where the brief makes that ask a proposal, it is now one: a ladder step equal to it is a repeat
    (opening_blocks_repeat / opening_is_attempt) and it used attempt 1 (opening_is_attempt). Runs once per product."""
    first = (lambda q: q["deadline"]) if S.get("rules", dict()).get("first_ask") == "deadline" else (lambda q: iso(today_of(V)))
    R_ = S.get("rules") or dict()
    for q in prods:
        if q["status"] in ("open", "agreed") and q.get("used", 0) == 0 and not q.get("opening_done"):
            q["opening_done"] = True
            if R_.get("opening_is_attempt") or R_.get("opening_blocks_repeat"):
                q["refused_offers"].append([round(q["full"], 2), first(q)])
            if R_.get("opening_is_attempt"):
                q["used"] = 1
                q["made"] = 1


def stage_nego(V, s, k, t, dtxt, a, scope):
    if k == "too_much" and not dtxt and a is None and S.get("rules", dict()).get("too_much_floor") == "refuse":
        k = "no"                                     # the brief: "too much" is a refusal of the proposal on the table
    prods = s["products"]
    multi = len(prods) > 1
    pv = cur(s) if s.get("phase") != "today" else None
    if s.get("all_pending") and (k in ("no", "refuses", "cannot", "too_much") or ((dtxt or a is not None) and k not in ("yes", "agrees", "pay_today"))):
        s.pop("all_pending")              # the adjusted day for all was not accepted: the others start at their own attempt 1
        for q in prods:
            if q is not cur(s) and q["status"] == "open" and not q.get("used"):
                q["offer"] = None
    if k in ("unclear", "none") and not dtxt and a is None and not (pv is not None and pv.get("vague_pending")):
        if (s.get("unclear_n", 0) < S.get("max_unclear", 3) and s.get("unclear_total", 0) < 8) or not counts_as_non_answer(V):
            s["unclear_n"] = s.get("unclear_n", 0) + 1     # small talk / a fragment is not an answer yet: re-ask the open question
            s["unclear_total"] = s.get("unclear_total", 0) + 1
            s["reask"] = True
            if pv is not None and pv.get("offer") and pv["offer"][1]:
                say(V, L("offer_short") or "Would that work for you?")
            else:
                say(V, ask_today_line(V, s))
            return talk(V, "nego", s)
    else:
        s["unclear_n"] = 0
    if multi and scope == "none" and len([x for x in S["products"].get("names", dict()) if x != "*"]) <= 1:
        ti0 = product_target(s, "any", t)
        if ti0 is not None:
            scope = "idx:" + str(ti0)
    target = (int(scope[4:]) if scope.startswith("idx:") else product_target(s, scope, t)) if multi else None
    if target is not None and prods[target]["status"] == "agreed" and k not in ("no", "refuses"):
        tp = prods[target]
        dd = parse_spoken_date(dtxt, today_of(V), S.get("date_rules")) if dtxt else None
        same = (dd is None or iso(dd) == tp["agreed"][1]) and (a is None or abs(snap_amount(tp, a) - tp["agreed"][0]) < 0.005)
        if same:                  # restating what was already agreed for that product: not about the current one
            target, dtxt, a, scope = None, "", None, "none"
    if target is not None:
        s["ci"] = target
        scope = "one"
        if prods[target]["status"] == "agreed" and (dtxt or a is not None or k in ("no", "refuses")):
            prods[target]["status"] = "open"
            prods[target]["offer"] = list(prods[target]["agreed"])
    if k in ("no", "refuses", "cannot") and dtxt and a is None and refuses_asked_day(V, s, dtxt, target):
        dtxt = ""                         # "I cannot pay on Friday" refuses the asked day; it proposes nothing
    if s.get("phase") == "today" and k in ("robot", "scam", "question", "purpose", "human", "not_talk", "asks_amount", "details") \
            and not dtxt and a is None and target is None:
        ask = ask_today_line(V, s)
        if k == "details":
            say(V, " ".join(L("product_line", name=q["name"], amount=money(q["full"]), days=q["days_late"]) for q in prods), ask)
            return talk(V, "nego", s)
        if k in ("question", "purpose"):
            say(V, L("no_info"), ask)
            return talk(V, "nego", s)
        if k == "asks_amount":
            say(V, ask)
            return talk(V, "nego", s)
        if common_intents(V, s, k, ask, True):
            return
    if s.get("phase") == "today":
        s["phase"] = "items"
        if k in ("yes", "agrees", "pay_today") and target is None and not dtxt:
            by_deadline = S.get("rules", dict()).get("first_ask") == "deadline"
            for p in prods:
                if p["status"] == "open":
                    agree(p, p["full"], p["deadline"] if by_deadline else pay_day(V, p))
            return readback(V, s)
        if target is not None:
            pass
        elif k in special_kinds():
            s["ci"] = next_open(s)                    # paid / dispute / hardship / call me later ...: handled below
        elif k in ("proposes", "gives_date", "gives_amount", "too_much", "vague") or dtxt or a is not None:
            s["ci"] = next_open(s)
            if multi and (scope == "all" or (dtxt and a is None and scope != "one")):
                return propose_all(V, s, dtxt, a, k)
        elif k == "asks_amount":
            s["phase"] = "today"
            say(V, ask_today_line(V, s))
            return talk(V, "nego", s)
        elif k in ("no", "refuses", "cannot", "none", "unclear"):
            s["ci"] = next_open(s)
            p = cur(s)
            intro = L("next_product", name=p["name"], amount=money(p["full"])) if multi else ""
            consume_opening(V, prods)
            p = cur(s) or p
            return start_product(V, s, p, intro)
        else:
            s["ci"] = next_open(s)
        consume_opening(V, prods)                     # any other first answer: the opening ask is spent too
    p = cur(s)
    if p is None:
        s["ci"] = next_open(s)
        p = cur(s)
        if p is None:
            return advance(V, s)
    again = L("repeat_offer", name=p["name"], amount=pay(p, p["offer"][0]), date=spoken(p["offer"][1])) if p.get("offer") and p["offer"][1] else ask_today_line(V, s)
    # after a side turn the offer was already spoken: re-prompt without naming the amount again
    short = (L("offer_short") or "Would that work for you?") if p.get("offer") and p["offer"][1] else again
    vague_pending = p.pop("vague_pending", False)
    if vague_pending and k not in special_kinds() and k not in ("yes", "agrees") and \
            (not dtxt or parse_spoken_date(dtxt, today_of(V), S.get("date_rules")) is None) and a is None:
        dl = datetime.date.fromisoformat(p["deadline"])          # rule: no exact day after asking once -> propose the deadline
        p["offer"] = [default_amount(p), iso(dl)]
        say(V, L("propose_deadline", date=spoken(dl), amount=pay(p, p["offer"][0])))
        return talk(V, "nego", s)
    if special(V, s, k, p, scope):
        return
    if common_intents(V, s, k, short, True):
        return
    if k == "details":                # "which account?", "how much was it?": answer from what we know, then re-prompt
        focus(p)
        line = L("product_line", name=p["name"], amount=money(p["full"]), days=p["days_late"])
        say(V, line, short if p.get("offer") and p["offer"][1] else ask_today_line(V, s))
        return talk(V, "nego", s)
    if k == "asks_amount":            # "you tell me" / "how much?": the agent always proposes the amount
        if p.get("offer") and p["offer"][1] and abs(p["offer"][0] - p["floor"]) < 0.005 and p.get("floor_note_said"):
            if p.get("floor_restated"):
                k = "too_much"            # asked again: treat as not accepting the floor
            else:
                p["floor_restated"] = True
                say(V, short)             # the floor was just named: do not name it again
                return talk(V, "nego", s)
        else:
            say(V, again)
            return talk(V, "nego", s)
    if k in ("question", "purpose"):
        say(V, L("no_info"), short)
        return talk(V, "nego", s)
    if multi and scope == "all" and (dtxt or a is not None or k in ("yes", "agrees")):
        return propose_all(V, s, dtxt, a, k)
    if k == "too_much" and a is not None:     # "too much, I could manage three hundred": the amount wins over the label
        p["too_much"] = True
        res, pre = apply_proposal(V, s, p, dtxt, a)
        if res == "agreed":
            return advance(V, s)
        if res == "ask":
            say(V, pre)
            return talk(V, "nego", s)
        return refusal(V, s, p, L("below_floor_again", amount=pay(p, p["floor"])))
    if k == "too_much":
        if p["floor_offered"]:
            return refusal(V, s, p)
        p["too_much"] = True
        if dtxt:                                  # "too much, but I could pay something on Friday"
            p["floor_offered"] = True
            p["floor_note_said"] = True
            p["offer"] = [p["floor"], None]
            res, pre = apply_proposal(V, s, p, dtxt, None)
            if res == "agreed":
                p["agreed"][0] = p["floor"]
                return advance(V, s, L("offer_floor", amount=pay(p, p["floor"])))
            say(V, L("offer_floor", amount=pay(p, p["floor"])), pre)
            return talk(V, "nego", s)
        if p["used"] == 0 and S.get("rules", dict()).get("too_much_floor") == "asked_date":
            asked = p["offer"][1] if p.get("offer") and p["offer"][1] else (
                p["deadline"] if S.get("rules", dict()).get("first_ask") == "deadline" else iso(today_of(V)))
            p["offer"] = [p["floor"], asked]       # the floor for the date just asked, without using an attempt
            p["floor_offered"] = True
            p["floor_note_said"] = True
            say(V, L("offer_floor_date", amount=pay(p, p["floor"]), date=spoken(asked)))
            return talk(V, "nego", s)
        if p["used"] == 0:
            return make_offer(V, s, p, 1)          # attempt 1 = the floor on the attempt-1 date, said once with its note
        if S.get("rules", dict()).get("too_much_skips_full"):
            return refusal(V, s, p)                # the brief's own meaning of "too much": a refusal; later full steps skipped
        d = p["offer"][1] if p.get("offer") and p["offer"][1] else iso(ladder_date(today_of(V), ladder_of(p)[0].get("date", "deadline"), p))
        p["offer"] = [p["floor"], d]
        p["floor_offered"] = True
        p["floor_note_said"] = True
        say(V, L("offer_floor_date", amount=pay(p, p["floor"]), date=spoken(d)))
        return talk(V, "nego", s)
    if k in ("yes", "agrees", "pay_today") and s.pop("all_pending", False) and not dtxt and a is None:
        for q in prods:
            if q["status"] == "open" and q.get("offer") and q["offer"][1]:
                agree(q, q["offer"][0], q["offer"][1])
        return advance(V, s)
    if k in ("yes", "agrees", "pay_today") and p.get("offer") and p["offer"][1] and not dtxt and \
            (a is None or snap_amount(p, a) <= p["offer"][0] + 0.005):
        agree(p, p["offer"][0], p["offer"][1])          # "yes, I can pay six hundred and thirty..." = yes to the offer
        return advance(V, s)
    if dtxt or a is not None or k in ("proposes", "gives_date", "gives_amount", "vague"):
        res, pre = apply_proposal(V, s, p, dtxt if dtxt else ("__vague__" if k == "vague" else ""), a)
        if res == "agreed":
            return advance(V, s)
        if res == "ask":
            p["asks"] = p.get("asks", 0) + 1
            if p["asks"] > 2:              # the same impossible date or period again and again: a refusal (one attempt)
                p["asks"] = 0
                return refusal(V, s, p, L("below_floor_again", amount=pay(p, p["floor"])))
            say(V, pre)
            return talk(V, "nego", s)
        if res == "refuse":
            return refusal(V, s, p, L("below_floor_final", amount=pay(p, p["floor"])) or L("below_floor_again", amount=pay(p, p["floor"])))
    if k in ("yes", "agrees", "pay_today"):
        if p.get("offer") and p["offer"][1]:
            agree(p, p["offer"][0], p["offer"][1])
            return advance(V, s)
        if p.get("offer") and not p["offer"][1]:
            agree(p, p["offer"][0], pay_day(V, p))
            return advance(V, s)
        agree(p, default_amount(p), pay_day(V, p))
        return advance(V, s)
    if k in ("no", "refuses", "cannot", "none", "unclear"):
        if p.get("after_deadline_offered") and S.get("rules", dict()).get("refuse_deadline_is_refusal", True):
            p["after_deadline_offered"] = False
        return refusal(V, s, p)
    say(V, short)                     # nothing new in the turn: re-prompt without restating the amount
    return talk(V, "nego", s)


def propose_all(V, s, dtxt, a, k):
    """One date (and maybe amount) for every open product."""
    pre_lines = []
    for p in s["products"]:
        if p["status"] != "open":
            continue
        if k in ("yes", "agrees") and not dtxt and a is None:
            if p.get("offer") and p["offer"][1]:
                agree(p, p["offer"][0], p["offer"][1])
                continue
            ref = cur(s) if cur(s) is not None and cur(s).get("offer") and cur(s)["offer"][1] else None
            ref = ref or next((q for q in s["products"] if q.get("agreed")), None)
            d0 = (ref.get("offer") or ref.get("agreed"))[1] if ref else iso(today_of(V))
            dq = min(datetime.date.fromisoformat(d0), datetime.date.fromisoformat(p["deadline"]))
            agree(p, default_amount(p), iso(dq), iso(dq) != d0)      # the same date as the others, capped at its deadline
            continue
        res, pre = apply_proposal(V, s, p, dtxt, a if len(s["products"]) == 1 else None, scope_all=True)
        if res != "agreed":
            s["ci"] = s["products"].index(p)
            if p.get("offer") and p["offer"][1]:          # e.g. a weekend date: offer the adjusted day for every open product
                for q in s["products"]:
                    if q is not p and q["status"] == "open" and not q.get("offer"):
                        dq = min(datetime.date.fromisoformat(p["offer"][1]), datetime.date.fromisoformat(q["deadline"]))
                        q["offer"] = [default_amount(q), iso(dq)]
                s["all_pending"] = True
            say(V, pre)
            return talk(V, "nego", s)
    return advance(V, s)


def stage_hardship(V, s, k, t, dtxt, a):
    p = cur(s) or (s["products"][0] if s["products"] else None)
    if k in ("restructure",) and p is not None and not s.get("hardship_offered"):
        return special(V, s, k, p) and None
    if k == "restructure":
        k = "no"                          # plan request after hardship: the support offer stands, no restructuring transfer
    if k in ("human",):
        k = "yes"
    if k in ("robot", "scam") and s.get("hardship_side", 0) < 3:
        s["hardship_side"] = s.get("hardship_side", 0) + 1
        say(V, L(k), L("hardship_short") or "Would you like that?")
        return talk(V, "hardship", s)
    if k in ("unclear", "none", "question") and not dtxt and a is None and s.get("hardship_reasked", 0) < 2:
        s["hardship_reasked"] = s.get("hardship_reasked", 0) + 1   # re-prompt without naming the team again
        say(V, L("no_info") if k == "question" else "", L("hardship_short") or "Would you like that?")
        return talk(V, "hardship", s)
    if k in ("yes", "agrees"):
        return transfer(V, s, "support", L("transfer"))
    if (dtxt or a is not None) and p is not None:
        s["stage"] = "nego"
        res, pre = apply_proposal(V, s, p, dtxt, a)
        if res == "agreed":
            return advance(V, s)
        say(V, pre)
        return talk(V, "nego", s)
    for q in s["products"]:
        if q["status"] in ("open",):
            q["status"] = "hardship"
    if any(q["status"] == "agreed" for q in s["products"]):
        return readback(V, s, L("hardship_declined"))        # promises agreed before the hardship stay
    return finish(V, s, L("hardship_declined"))


def stage_confirm(V, s, k, t, dtxt, a, scope):
    agreed = [p for p in s["products"] if p["status"] == "agreed"]
    if len(s["products"]) > 1 and scope == "none" and len([x for x in S["products"].get("names", dict()) if x != "*"]) <= 1:
        ti0 = product_target(s, "any", t)
        if ti0 is not None:
            scope = "idx:" + str(ti0)
    ti = int(scope[4:]) if scope.startswith("idx:") else product_target(s, scope, t)
    named_change = False
    if ti is not None and s["products"][ti] in agreed and (dtxt or a is not None):
        kp0 = s["products"][ti]
        dd = parse_spoken_date(dtxt, today_of(V), S.get("date_rules")) if dtxt else None
        named_change = (dd is not None and iso(dd) != kp0["agreed"][1]) or (a is not None and abs(snap_amount(kp0, a) - kp0["agreed"][0]) > 0.005)
    if ti is not None and len(agreed) > 1 and named_change and k not in special_kinds():
        kp = s["products"][ti]                    # "make the card Monday": re-check that product only, keep the others
        kp["status"] = "open"
        kp["offer"] = list(kp["agreed"])
        res, pre = apply_proposal(V, s, kp, dtxt, a)
        if res == "agreed":
            return readback(V, s)
        s["stage"] = "nego"
        s["ci"] = ti
        say(V, pre or L("offer_short") or "Would that work for you?")
        return talk(V, "nego", s)
    if ti is not None and len(agreed) > 1 and s["products"][ti] in agreed and k not in special_kinds():
        # "I only confirm the credit card": keep that one, the others go back to negotiation as refused offers
        kp = s["products"][ti]
        for p in agreed:
            if p is not kp:
                p["status"] = "open"
                p["refused_offers"].append(list(p["agreed"]))
                p["agreed"] = None
        if dtxt or a is not None:
            kp["status"] = "open"
            kp["offer"] = list(kp["agreed"])
            res, pre = apply_proposal(V, s, kp, dtxt, a)
            if res != "agreed":
                s["stage"] = "nego"
                s["ci"] = ti
                say(V, pre)
                return talk(V, "nego", s)
        s["stage"] = "nego"
        s["ci"] = next_open(s)
        if cur(s) is None:
            return readback(V, s)
        return refusal(V, s, cur(s), L("next_product", name=cur(s)["name"], amount=money(cur(s)["full"])))
    changed = False
    if (dtxt or a is not None) and len(agreed) == 1:     # "yes, but make it Monday": re-check the new value first
        p0 = agreed[0]
        dd = parse_spoken_date(dtxt, today_of(V), S.get("date_rules")) if dtxt else None
        changed = (dd is not None and iso(dd) != p0["agreed"][1]) or (a is not None and abs(snap_amount(p0, a) - p0["agreed"][0]) > 0.005)
    if k in ("yes", "agrees", "confirm") and not changed:
        s["rec_queue"] = [p["id"] for p in agreed]
        return record_next(V, s)
    if k == "too_much" and agreed:
        p0 = agreed[0]
        for p in agreed:
            p["status"] = "open"
            p["offer"] = list(p["agreed"])
        s["stage"] = "nego"
        s["ci"] = s["products"].index(p0)
        return stage_nego(V, s, "too_much", t, dtxt, a, "none")
    if dtxt or a is not None:
        target = agreed[0] if len(agreed) == 1 else None
        if target is None:
            for p in agreed:
                p["status"] = "open"
            return propose_all(V, s, dtxt, a, "proposes")
        target["status"] = "open"
        target["offer"] = [target["agreed"][0], target["agreed"][1]]
        res, pre = apply_proposal(V, s, target, dtxt, a)
        if res == "agreed":
            return readback(V, s)
        s["stage"] = "nego"
        s["ci"] = s["products"].index(target)
        say(V, pre)
        return talk(V, "nego", s)
    if special(V, s, k, agreed[0] if agreed else s["products"][0], scope):
        return
    if common_intents(V, s, k, L("confirm_q"), True):
        return
    if k in ("unclear", "none", "question") and not dtxt and a is None:
        # No answer yet (small talk, a fragment). Silence is not a refusal: the plan the customer agreed is kept (never
        # reopened for the earlier offers), and every third time the plan itself is read again instead of a bare
        # "do you confirm?".
        n = s.get("confirm_unclear", 0) + 1
        s["confirm_unclear"] = n
        s["reask"] = True
        if k == "question" or n % 3 == 0:
            say(V, *(readback_parts(s) + [L("confirm_q")]))
        else:
            say(V, L("confirm_q") if n == 1 else (L("confirm_again") or "Sorry, I did not catch that. Do you confirm, yes or no?"))
        return talk(V, "confirm", s)
    if k in ("no", "refuses"):
        for p in agreed:
            p["status"] = "open"
            p["refused_offers"].append(list(p["agreed"]))
        s["stage"] = "nego"
        s["ci"] = next_open(s)
        return refusal(V, s, cur(s))
    say(V, L("confirm_q"))
    return talk(V, "confirm", s)


def special_kinds():
    return ("already_paid", "dispute", "hardship", "restructure", "human", "robot", "scam", "not_talk", "call_later")


def record_next(V, s):
    q = s.get("rec_queue", [])
    if not q:
        rec = [p for p in s["products"] if p["status"] == "recorded"]
        failed = [p for p in s["products"] if p["status"] == "service_error"]
        lines = []
        mixed = bool(rec) and bool(failed)
        if rec:
            lines.append(for_products(rec, "recorded", mixed))
            lines.append(L("sms_line"))
        if failed:
            lines.append(for_products(failed, "record_failed", mixed))
        if rec and S.get("after_promise", dict()).get("ask_anything_else", True):
            s["stage"] = "after"                    # give the customer a turn (change requests, transfers after a promise)
            say(V, " ".join(lines), L("anything_else") or "Is there anything else I can help you with?")
            return talk(V, "after", s)
        return finish(V, s, " ".join(lines))
    pid = q[0]
    p = [x for x in s["products"] if x["id"] == pid][0]
    if not promise_representable(p):
        # the API cannot take this amount (whole units, no rounding rule in the brief): no write call with a value we
        # know is invalid, and no silent change of what the customer agreed. The product ends as not recorded.
        s["rec_queue"] = q[1:]
        p["status"] = "service_error"
        p["reason"] = "unrepresentable_amount"
        return record_next(V, s)
    s["rec_try"] = s.get("rec_try", 0)
    return call(V, s, "api_promise", p_product=p["id"], p_amount=promise_amount(p), p_date=p["agreed"][1])


def promise_representable(p):
    a = promise_amount(p)
    return isinstance(a, int) or not (S["products"].get("promise_integer") or
                                      float(S["products"].get("promise_scale") or S["products"].get("amount_scale") or 1) != 1)


def promise_amount(p):
    """The amount sent to create_promise, in the unit the tool API takes (promise_scale, else amount_scale). Minor units
    and integer fields take whole numbers. A value that is not whole after conversion is never cut silently: the brief's
    rounding rule (products.promise_round) applies; without one the fraction is returned unchanged and record_next
    does not send it (promise_representable)."""
    scale = float(S["products"].get("promise_scale") or S["products"].get("amount_scale") or 1)
    a = p["agreed"][0] * scale
    if not (S["products"].get("promise_integer") or scale != 1):
        return round(a, 2)
    if abs(a - round(a)) < 1e-6:                       # whole after conversion (float noise only): 85.5 -> 8550
        return int(round(a))
    rule = S["products"].get("promise_round")
    if rule == "up":
        return int(a) + 1
    if rule == "down":
        return int(a)
    if rule == "nearest":
        return int(a + 0.5)
    return round(a, 6)


def for_products(prods, key, name_them):
    """A line about some products only: names them when the outcome differs between products."""
    names = " and ".join(p["name"] for p in prods)
    t = S.get("lines", dict()).get(key, "")
    if not name_them:
        return L(key, name=names) if "{name}" in t else L(key)
    if "{name}" in t:
        return L(key, name=names)
    line = L(key)
    if line and not re.match(r"I\b|I'", line):
        line = line[:1].lower() + line[1:]
    return ("For your " + names + ": " + line) if line else ""


def after_promise(V, s):
    q = s.get("rec_queue", [])
    pid = q[0]
    p = [x for x in s["products"] if x["id"] == pid][0]
    ok = succeeded(V, "api_promise")
    if ok:
        p["status"] = "recorded"
        q.pop(0)
        s["rec_try"] = 0
        return record_next(V, s)
    s["rec_try"] = s.get("rec_try", 0) + 1
    retries = S.get("rules", dict()).get("promise_retries")
    if s["rec_try"] < 1 + (1 if retries is None else int(retries)):   # the brief's retries (unstated: one, reported)
        return call(V, s, "api_promise", p_product=p["id"], p_amount=promise_amount(p), p_date=p["agreed"][1])
    p["status"] = "service_error"
    q.pop(0)
    s["rec_try"] = 0
    return record_next(V, s)


# ----------------------------------------------------------------------------------------------- chains
# A bank write whose follow-up needs no new decision does not come back to this code node: the engine plans the
# whole sequence as if every call succeeds (e.g. promise, promise, then the 'recorded' turn; note, note, close)
# and the workflow walks it through deterministic transitions (<node> succeeded AND plan_<node> == next node).
# Each code run costs seconds of silence on the platform. If a call fails, the workflow falls back to this node,
# which rebuilds the state from what really happened and continues one call at a time.
CHAIN = dict(api_promise=["api_promise", "api_promise_2", "api_promise_3"], api_note=["api_note", "api_note_2", "api_note_3"],
             api_callback=["api_callback"], api_transfer=["api_transfer"])
SLOTS_OF = dict(api_promise=["p_product", "p_amount", "p_date"], api_note=["n_product", "n_reason"],
                api_callback=["cb_date", "cb_time", "cb_datetime"], api_transfer=["t_queue"])
PHYS = [x for v in CHAIN.values() for x in v]


def logical(node):
    return re.sub(r"_[23]$", "", node)


def suffix(node):
    m = re.search(r"_([23])$", node)
    return "_" + m.group(1) if m else ""


def counts(V, node):
    return (num(V.get(node + "_success_count")) or 0) + (num(V.get(node + "_failure_count")) or 0)


def fake_success(V, node):
    R_ = S.get("responses", dict()).get(TOOL_OF.get(node, ""), dict()) or dict()
    for k in list(V.keys()):
        if k.startswith(node + "_response"):
            V[k] = ""
    f = R_.get("field", "ok") if R_ else "ok"
    vals = R_.get("values") or ["true"]
    V[node + "_response.data." + (f or "done")] = vals[0] if f else "true"
    V[node + "_response.status_code"] = 200


def clear_result(V, node):
    for k in list(V.keys()):
        if k.startswith(node + "_response") or k == node + "_status_code":
            V[k] = ""


def chainable(node):
    r = S.get("responses", dict()).get(TOOL_OF.get(node, ""), dict()) or dict()
    return bool(r.get("field")) if r else True     # without a success flag the platform cannot test success


def plan_chain(V):
    nxt = V.get("next")
    if nxt not in CHAIN or S.get("no_chains") or not chainable(nxt):
        return
    s = st(V)
    if s.get("no_chain"):
        return
    snap = dict(V)
    s0 = V.get("_state", "")
    calls, used = [], dict((k, 0) for k in CHAIN)
    try:
        for _ in range(12):
            node = V.get("next")
            if node not in CHAIN or not chainable(node):
                break
            if used[node] >= len(CHAIN[node]):
                raise ValueError("chain too long")
            phys = CHAIN[node][used[node]]
            used[node] += 1
            calls.append(dict(node=node, phys=phys, slots=dict((k, V.get(k)) for k in SLOTS_OF[node])))
            fake_success(V, node)
            handle_result(V, st(V), node)
        final = V.get("next") or ""
        if not (final.startswith("talk_") or final in ("api_close", "api_close_nr")):
            raise ValueError("chain does not end in a turn or the close")
    except Exception:
        for k in list(V.keys()):                       # undo the dry run
            if k not in snap:
                V[k] = ""
        V.update(snap)
        return
    fs = st(V)
    fs["chain_prefix"] = snap.get("_prefix", "")
    if final.startswith("talk_"):
        fstage = final[5:].replace("open_again", "open")
        for f, v in (("_kind", "none"), ("_cut", "no"), ("_text", "")):
            V[fstage + f] = v                          # platform-valid resets ("" is rejected for enum variables)
        fs["turns_at_plan"] = customer_turns(V)
    for p in PHYS:
        clear_result(V, p)
        V["plan_" + p] = ""
    for i, c in enumerate(calls):
        for k, v in c["slots"].items():
            V[k + suffix(c["phys"])] = v
        V["plan_" + c["phys"]] = calls[i + 1]["phys"] if i + 1 < len(calls) else final
    fs["chain"] = calls
    fs["chain_s0"] = jget(dict(x=s0), "x", dict())
    fs["chain_await"] = fs.get("await", "")
    fs["chain_final"] = final
    fs["chain_say"] = V.get("say", "")
    fs["chain_close"] = [V.get("c_outcome", ""), V.get("c_reason", "")]
    fs["chain_counts"] = dict((c["phys"], counts(V, c["phys"])) for c in calls)
    fs["await"] = "chain"
    put(V, fs)
    V["next"] = calls[0]["phys"]


def chain_step(V, s):
    """The step node runs after a planned chain. Returns True if this run is fully handled."""
    calls = s.get("chain", [])
    c0 = s.get("chain_counts", dict())
    def has_result(n):
        return any(k.startswith(n + "_response") and V[k] not in (None, "") for k in V)
    # A failed call leaves no trace on the platform. But a call that succeeded moves on to the next one, so the
    # chain stopped at the first call that did not succeed: that one ran (and failed), the ones after it did not.
    ran, stopped = [], False
    for c in calls:
        ran.append(not stopped or counts(V, c["phys"]) > c0.get(c["phys"], 0) or has_result(c["phys"]))
        if not succeeded(V, c["phys"]):
            stopped = True
    if all(ran) and all(succeeded(V, c["phys"]) for c in calls):
        final = s.pop("chain_final", "")
        say_ = s.pop("chain_say", "")
        cl = s.pop("chain_close", ["", ""])
        s["await"] = s.pop("chain_await", "talk")
        for k in ("chain", "chain_s0", "chain_counts"):
            s.pop(k, None)
        put(V, s)
        stage = s.get("stage", "open")
        n0, n1 = s.get("turns_at_plan"), customer_turns(V)
        heard = (n1 > n0) if (n0 is not None and n1 is not None) else fresh_answer(V, stage)
        if final.startswith("talk_") and heard:
            return False                               # the planned turn was spoken and answered: carry on
        V["say"] = say_                                # the workflow came back before the planned node: go there now
        V["c_outcome"], V["c_reason"] = cl[0], cl[1]
        go(V, final)
        return True
    # a call failed: rebuild the state from what really happened, then continue one call at a time
    st0 = dict(s.get("chain_s0") or dict())
    st0["no_chain"] = True
    put(V, st0)
    if s.get("chain_prefix"):
        V["_prefix"] = s["chain_prefix"]
    for c, r in zip(calls, ran):
        if not r:
            return True                                # the previous result already routed to this call: make it now
        ok = succeeded(V, c["phys"])
        if c["phys"] != c["node"]:
            clear_result(V, c["node"])
            for k in list(V.keys()):
                if k.startswith(c["phys"] + "_response"):
                    V[c["node"] + k[len(c["phys"]):]] = V[k]
        for k, v in c["slots"].items():
            V[k] = v
        handle_result(V, st(V), c["node"])
        if not ok:
            return True
    return True


def fallback_close(V):
    """Outcome to close with if later engine runs keep failing on the platform (api_close_fb)."""
    s = st(V)
    if s.get("await") == "chain" and s.get("chain_s0"):
        s = s["chain_s0"]                              # what really happened so far, not the planned result
        prods = s.get("products") or []
        e = ev("promise" if any(p.get("status") == "recorded" for p in prods) else ("service_error" if prods else "unclear"))
        V["fb_outcome"], V["fb_reason"] = e.get("outcome", ""), e.get("reason", "") or ""
        V["fb_nr"] = "no" if V["fb_reason"] else "yes"
        V["fb_say"] = (L("system_error") or "I'm sorry, we have a technical problem and need to end the call. Please call us back.") + " " + L("goodbye")
        return
    prods = s.get("products") or []
    if s.get("stage") == "closing" and V.get("c_outcome"):
        o, r = V.get("c_outcome", ""), V.get("c_reason", "")
    else:
        e = ev("promise" if any(p.get("status") == "recorded" for p in prods) else ("service_error" if prods else "unclear"))
        o, r = e.get("outcome", ""), e.get("reason", "") or ""
    V["fb_outcome"], V["fb_reason"] = o, r
    V["fb_nr"] = "no" if r else "yes"
    V["fb_say"] = (L("system_error") or "I'm sorry, we have a technical problem and need to end the call. Please call us back.") + " " + L("goodbye")


# ----------------------------------------------------------------------------------------------- entry
def run(V):
    s = st(V)
    aw = s.get("await", "")
    if not s:   # the initial talk_open node already spoke the opening and the first identity ask
        s = dict(stage="open", asks=1, questions=0, not_talk=0)
    if aw == "chain":
        if chain_step(V, s):
            return
        s = st(V)
        aw = s.get("await", "")
    if aw in RESULT_NODES:
        return handle_result(V, s, aw)
    stage = s.get("stage", "open")
    if s.get("products") and cur(s) is not None:
        focus(cur(s))                                  # product fields for every line said this step
    n = customer_turns(V)
    if n is not None and s.get("turns_seen") is not None and n <= s["turns_seen"]:
        # no new customer message since the last answer was used: nothing extracted earlier is taken as an answer
        # (never as a yes, a no or a confirmation); the last line is said again
        s["reask"] = True
        return talk(V, stage, s)
    if n is not None:
        s["turns_seen"] = n
    elif stage in TEXT_STAGES and not str(V.get(stage + "_text") or "").strip():
        # no counters: a valid value ("yes") does not prove a new answer. The talk node copies every customer message
        # into <stage>_text, which the engine empties when it hands over and after use, so an empty text means no new
        # message: nothing is used, the plan stays, the last line is said again
        s["reask"] = True
        return talk(V, stage, s)
    return run_turn(V, s, stage)


RESULT_NODES = ("api_verify", "api_account", "api_promise", "api_note", "api_callback", "api_transfer", "api_close", "api_close_nr")


def handle_result(V, s, aw):
    """What to do after a bank call returned (or failed)."""
    if aw == "api_verify":
        return after_verify(V, s)
    if aw == "api_account":
        return after_account(V, s)
    if aw == "api_promise":
        return after_promise(V, s)
    if aw in ("api_note", "api_callback", "api_transfer"):
        ok = succeeded(V, aw)
        if not ok and not s.get("retried_" + aw):
            s["retried_" + aw] = True                 # HTTP 500 / failure: try once more with the same values
            return call(V, s, aw)
        s.pop("retried_" + aw, None)
        if aw == "api_note":
            return finish(V, s)
        if aw == "api_callback":
            if ok:
                return close(V, s, s.pop("cb_event", "callback"), s.pop("cb_booked", ""))
            return no_callback(V, s, L("callback_failed") or "I'm sorry, I could not book the callback. Please call us back.")
        if ok:
            return close(V, s, "transfer_" + s.get("transferred", "human"), s.get("say_after_transfer", ""))
        line = L("transfer_failed") or "I'm sorry, I could not connect you right now. Please call us back."
        s.pop("transferred", None)
        if not s.get("products"):
            return close(V, s, "service_error", line)
        for p in s["products"]:
            if p["status"] in ("open", "agreed"):
                p["status"] = "service_error"
        return finish(V, s, line)
    if aw in ("api_close", "api_close_nr"):
        go(V, "end")
        return


NUM_RUN = r"(?:(?:%s|\d+(?:\.\d+)?)[\s,-]*)+" % "|".join(sorted(list(NUM_WORDS) + ["hundred", "thousand"], key=len, reverse=True))
NEG_DATE = re.compile(r"(do(es)?\s*n[o']?t\s+work|won'?t\s+work|no\s+good|not\s+good|can'?t\s+(do|make)|cannot\s+(do|make)|is\s*n'?t\s+(good|possible)|not\s+(on\s+)?(that|this|monday|tuesday|wednesday|thursday|friday|saturday|sunday))", re.I)
def rejects_day(t, dtxt):
    """True when the message says the day it names does not work ("Yes... no, Friday does not work", "not on
    Friday"), whatever the intent label: the negation must be attached to that day, so "Monday doesn't work, but
    Tuesday is fine" (day = Tuesday) is still a proposal."""
    tl, d = str(t or "").lower(), re.escape(str(dtxt or "").lower().strip())
    if not d:
        return False
    neg = r"(do(es)?\s*n[o']?t\s+work|won'?t\s+work|is\s+no\s+good|is\s*n'?t\s+(good|possible)|is\s+not\s+(good|possible)|isn'?t\s+ok)"
    return bool(re.search(d + r"\W{0,3}\s*" + neg, tl) or re.search(r"(not|never|can'?t|cannot|can\s+not)\s+(do\s+|make\s+|pay\s+)?(it\s+)?(on\s+)?" + d, tl))


CORRECTION = re.compile(r"^\W*(no\b|nope|sorry|i mean|actually|wait|not\b|make that|rather)", re.I)


def amount_pieces(V, s, stage, k, t, dtxt, a):
    """An amount said in two messages. The extraction reads only the latest message, so the engine keeps the first
    piece and decides from the shape of the two pieces (the field asked is an amount here; identity digits are
    joined in the identity stage):
      'three hundred and' + 'fifty'  -> one number, joined (350);
      '463...' + '51'                -> cents, more digits or a new amount: ask once what the full amount is;
      '463...' + 'no, 500' / 'sorry, I mean 500' -> a correction: the new amount.
    The last piece never silently replaces the first, and the pieces are never joined blindly.
    Returns the amount to use, or "asked" when a clarifying question was put."""
    frag = s.pop("num_frag", None)
    tl = str(t or "").lower().strip()
    if frag is not None and tl:
        lead = re.match(r"\s*(" + NUM_RUN + ")", tl)
        rest = words_to_number(lead.group(1)) if lead else None
        if rest is not None and not CORRECTION.search(tl) and (a is None or abs(float(a) - rest) < 0.005):
            if frag["kind"] == "hundreds" and rest < 100:
                return frag["v"] + rest
            if rest < 100 and s.get("amount_asked", 0) < 2:
                s["amount_asked"] = s.get("amount_asked", 0) + 1
                s.pop("held", None)                      # neither piece is taken on its own
                s["reask"] = True
                say(V, L("amount_clarify") or "Sorry, I want to be sure I have the amount right. How much can you pay in total?")
                talk(V, stage, s)
                return "asked"
            if rest < 100:
                s.pop("held", None)
                return None                              # still unclear: no amount rather than the wrong one
    cut = V.get("_cut") == "yes" or bool(re.search(r"(\.\.\.|\u2026|-|\u2014)\s*$", tl))
    if cut and tl:
        tail = re.search(r"(" + NUM_RUN + r")(?:\s+and)?\W*$", tl)
        v = words_to_number(tail.group(1)) if tail else None
        if v:
            kind = "hundreds" if re.search(r"(hundred|thousand)(\s+and)?\W*$", tl) else "number"
            s["num_frag"] = dict(v=v, kind=kind)
            if a is None and k in ("proposes", "vague", "gives_amount", "unclear", "none") and not dtxt \
                    and s.get("go_on_total", 0) < GO_ON_MAX:
                s["go_on_total"] = s.get("go_on_total", 0) + 1
                s["reask"] = True
                say(V, L("go_on") or "Sorry, please go on.")
                talk(V, stage, s)
                return "asked"
    return a


GO_ON_MAX = 4          # content-free "please go on" lines in one call; after that a cut-off message gets the question again
STALL_MAX = 12         # customer turns in a row with nothing in them (negotiation / read-back) before the call is ended


def counts_as_non_answer(V):
    """A complete message that answers nothing counts against the customer only when the brief says a non-answer
    does (rules.non_answer_counts). A cut-off fragment is an unfinished answer, never a non-answer."""
    return bool((S.get("rules") or dict()).get("non_answer_counts")) and V.get("_cut") != "yes"


def stalled_out(V, s):
    """STALL_MAX empty turns in a row. With the brief's non-answer rule the remaining attempts are spent: products
    without a confirmed promise end as refused (the brief's close for used-up attempts), nothing is recorded."""
    for p in s.get("products", []):
        if p["status"] in ("open", "agreed"):
            p["status"] = "refused"
            p["offer"] = None
    return finish(V, s, L("stall_close") or "I am sorry, I cannot hear an answer, so I will end the call here.")


def run_turn(V, s, stage):
    """A customer turn arrived from talk_<stage>."""
    k, t, dtxt, a, scope = take_input(V, stage)
    V["_t"] = t
    if s.pop("human_pending", False) and k in ("no", "refuses", "not_talk", "human", "unclear", "none", "asks_why"):
        k = "human"                                    # still wants a person after being asked once: transfer
    if V.get("_cut") != "yes" and t and k in ("too_much", "no", "cannot", "unclear", "none") and not dtxt and a is None and \
            not re.search(r"[.?!]\s*$", t) and re.search(r"\b(to|like|want|mean|i|the|a|an|on|and|but|because|would|can|pay|just|so|my)\s*$", t.lower()):
        V["_cut"] = "yes"                              # text backup: the message ends on an unfinished phrase
    if stage in ("nego", "confirm"):                  # turns that bring nothing (fragments, small talk), over the call
        empty = k in ("unclear", "none") and not dtxt and a is None
        asked = k in ("question", "asks_amount", "robot", "scam", "asks_why") and not dtxt and a is None
        s["stall"] = s.get("stall", 0) + 1 if (empty or asked) else 0   # a question back is answered but brings no progress
        if empty and s["stall"] >= STALL_MAX and (S.get("rules") or dict()).get("non_answer_counts"):
            return stalled_out(V, s)
    if s.pop("day_asked", False) and k in ("yes", "agrees") and not dtxt and a is None:
        k = "unclear"                                  # "yes" to "which day?" names no day: nothing is accepted
    if stage in ("nego", "confirm") and dtxt and a is None and k not in ("yes", "agrees") and rejects_day(t, dtxt):
        # "Yes... no, Friday does not work": the day named is rejected, not proposed (never read back as the plan)
        pv = cur(s) if stage == "nego" and s.get("products") else None
        dd = parse_spoken_date(dtxt, today_of(V), S.get("date_rules"))
        dtxt = ""
        if stage == "nego" and not (pv and pv.get("offer") and dd and pv["offer"][1] == iso(dd)):
            s["day_asked"] = True                      # no other day given: ask for one (no attempt is used)
            s["reask"] = True
            say(V, L("ask_other_day") or "Which day would work for you instead?")
            return talk(V, stage, s)
    if stage in ("nego", "confirm", "hardship"):
        r = amount_pieces(V, s, stage, k, t, dtxt, a)
        if r == "asked":
            return
        a = r
    if V.get("_cut") == "yes" and stage in ("open", "tp", "id", "nego", "confirm", "hardship") and not dtxt and a is None \
            and k in ("unclear", "none", "too_much", "cannot") \
            and s.get("cut_waits", 0) < 2 and s.get("go_on_total", 0) < GO_ON_MAX \
            and k not in ("wrong_number", "deceased", "dispute", "already_paid", "hardship", "restructure"):
        s["cut_waits"] = s.get("cut_waits", 0) + 1     # the customer stopped mid-sentence: let them finish
        s["go_on_total"] = s.get("go_on_total", 0) + 1   # bounded over the whole call: then the question itself is asked again
        s["reask"] = True
        say(V, L("go_on") or "Sorry, please go on.")
        return talk(V, stage, s)
    if V.get("_cut") == "yes" and stage in ("open", "tp") and k in ("wrong_number", "deceased") and not s.get("end_cut_waited"):
        s["end_cut_waited"] = True                     # 'No, you're not...' cut off: hear the rest before ending the call
        s["go_on_total"] = s.get("go_on_total", 0) + 1
        s["reask"] = True
        say(V, L("go_on") or "Sorry, please go on.")
        return talk(V, stage, s)
    if V.get("_cut") == "yes" and (dtxt or a is not None) and stage in ("nego", "confirm", "hardship") \
            and k not in ("yes", "agrees", "confirm") and not s.get("cut_value_waited"):
        s["cut_value_waited"] = True                   # "I can pay six hundred and..." : let them finish the number
        s["held"] = [dtxt, a, scope, k]                # keep what was said in case the rest does not repeat it
        s["go_on_total"] = s.get("go_on_total", 0) + 1
        s["reask"] = True
        say(V, L("go_on") or "Sorry, please go on.")
        return talk(V, stage, s)
    held = s.pop("held", None)
    if held and not dtxt and a is None and k in ("proposes", "unclear", "none", "yes", "agrees", "too_much"):
        dtxt, a = held[0], held[1]                     # the finished sentence did not repeat the date/amount
        scope = scope if scope != "none" else held[2]
        if k in ("unclear", "none"):
            k = held[3] if held[3] not in ("unclear", "none") else "proposes"
    if V.get("_cut") != "yes":
        s["cut_value_waited"] = False
    s["cut_waits"] = 0
    if s.get("same", 0) >= 2 and stage == "nego" and not dtxt and a is None \
            and (k in ("proposes", "details") or (k in ("unclear", "none") and counts_as_non_answer(V))):
        # a question asked again is answered again: it is never turned into a refusal (no attempt is used)
        k, scope = "no", "none"     # the same line three times and still nothing new: stop looping, count a non-answer
    prev = V.get("say", "")
    dispatch(V, s, stage, k, t, dtxt, a, scope)
    s2 = st(V)
    if s2:
        if s2.pop("reask", False):
            pass                                   # a content-free re-ask is not an engine loop
        else:
            s2["same"] = (s2.get("same", 0) + 1) if V.get("say", "") == prev and s2.get("stage") in ("nego", "confirm") else 0
        put(V, s2)


def stage_after(V, s, k, t):
    """After a recorded promise: 'can I change it?', a transfer request, or goodbye."""
    offer = s.pop("after_offer", "")
    if offer == "support" and k in ("yes", "agrees", "human"):
        return transfer(V, s, "support", L("transfer"))
    if k in ("robot", "scam"):
        say(V, L(k), L("anything_else") or "Is there anything else I can help you with?")
        return talk(V, "after", s)
    if k == "change":
        return finish(V, s, L("promise_change"))
    if k == "restructure" and not s.get("hardship_offered"):
        return transfer(V, s, "restructuring", L("restructure_line"))
    if k == "human":
        if not s.get("human_asked") and S.get("human", dict()).get("ask_first", True):
            s["human_asked"] = True       # ask once whether we can help, as everywhere else
            s["human_pending"] = True
            say(V, L("human_ask_help"))
            return talk(V, "after", s)
        return transfer(V, s, "human", L("transfer"))
    if k == "hardship":
        if not ev("transfer_support").get("queue") or s.get("hardship_offered"):
            return finish(V, s, L("hardship") if not s.get("hardship_offered") else "")
        s["hardship_offered"] = True
        s["after_offer"] = "support"
        say(V, L("hardship"))
        return talk(V, "after", s)
    if k == "question" and not s.get("after_q"):
        s["after_q"] = True
        say(V, L("no_info"), L("anything_else") or "Is there anything else I can help you with?")
        return talk(V, "after", s)
    return finish(V, s)


def dispatch(V, s, stage, k, t, dtxt, a, scope):
    if stage == "after":
        return stage_after(V, s, k, t)
    if stage == "open":
        return stage_open(V, s, k, t)
    if stage == "tp":
        return stage_tp(V, s, k, t, dtxt)
    if stage == "id":
        return stage_id(V, s, k, t)
    if stage == "nego":
        return stage_nego(V, s, k, t, dtxt, a, scope)
    if stage == "hardship":
        return stage_hardship(V, s, k, t, dtxt, a)
    if stage == "confirm":
        return stage_confirm(V, s, k, t, dtxt, a, scope)
    go(V, "end")


def set_in(d, path, value):
    keys = str(path).split(".")
    for i, k in enumerate(keys[:-1]):
        if isinstance(d, list):
            d = d[int(k)]
            continue
        if not isinstance(d.get(k), (dict, list)):
            d[k] = [] if keys[i + 1].isdigit() else dict()
        d = d[k]
    if isinstance(d, list):
        d[int(keys[-1])] = value
    else:
        d[keys[-1]] = value


def effective(spec, day):
    """The spec in force on `day`: the brief's dated changes (spec["changes"]: [{"from": "YYYY-MM-DD", "until"?:
    "YYYY-MM-DD", "set": {"<dotted path>": value}}]) applied in date order on a copy. The base is never changed."""
    ch = [c for c in spec.get("changes") or [] if isinstance(c, dict) and str(c.get("from", "9999-12-31")) <= iso(day)
          and (not c.get("until") or iso(day) <= str(c["until"]))]
    if not ch:
        return spec
    s = json.loads(json.dumps(spec))
    for c in sorted(ch, key=lambda c: str(c.get("from"))):
        for path, value in (c.get("set") or dict()).items():
            set_in(s, path, value)
    return s


def main(V):
    global S
    base, G = S, is_business_day.__globals__          # G: where the calendar (P) lives, here and in the code node
    base_p = G.get("P")
    TODAY[0] = today_of(V)
    if base.get("changes"):
        S = effective(base, TODAY[0])
        G["P"] = dict(workdays=S["calendar"]["workdays"], holidays=S["calendar"]["holidays"])
    try:
        main_turn(V)
    finally:
        S = base
        G["P"] = base_p


def main_turn(V):
    CTX.clear()
    CTX.update(names(V))
    V["next"] = ""
    for p in PHYS:
        V["plan_" + p] = ""
    try:
        run(V)
        plan_chain(V)
        fallback_close(V)
    except Exception as e:  # never let the call die: apologise and close safely
        V["engine_error"] = (type(e).__name__ + ": " + str(e))[:300]
        s = st(V)
        if s.get("stage") == "closing":
            go(V, "end")
            return
        if s.get("products"):
            try:
                for q in s["products"]:                 # a crash is a service failure, not the customer's refusal
                    if q.get("status") in ("open", "agreed"):
                        q["status"] = "service_error"
                return finish(V, s, L("no_info"))
            except Exception:
                pass
        e_ = ev("service_error") or ev("refused")
        V["c_outcome"] = e_.get("outcome", "")          # a technical failure is a service error, not a refusal
        V["c_reason"] = e_.get("reason", "") or ""
        say(V, L("goodbye"))
        V["_state"] = json.dumps(dict(stage="closing"))
        go(V, "api_close" if V["c_reason"] else "api_close_nr")
