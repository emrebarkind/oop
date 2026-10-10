"""Brief(s) + tool API file -> spec (builder/spec_doc.md), with validation, a second independent reading,
and an adjudication pass on disagreements."""
import copy
import json
import os
import pathlib
import re

from . import claude
from . import compile as comp
from . import spec_model

DOC = (pathlib.Path(__file__).resolve().parent / "spec_doc.md").read_text()

SYSTEM = """You are a meticulous bank-operations analyst. You turn a bank's collections brief (which may be a clean
specification, or raw material: emails, policy extracts, memos that change earlier rules, code lists) and the
bank's tool API file into a precise machine-readable spec for an automated phone agent.

Rules:
- Read every line. Most failures come from one skipped rule.
- When documents conflict, the later or more specific instruction wins (a memo that changes a rule beats the
  policy it changes; a dated change beats an undated one). Record every such resolution in `assumptions`.
- Take every value (dates, numbers, codes, field names, enum values, wording) from the documents given. Never
  reuse values from examples or from your own knowledge.
- Field names and allowed values for tool calls come from the tool API file.
- Answer with the JSON object only."""


def prompt_for(briefs, openapi):
    docs = "\n\n".join(f"<document name=\"{name}\">\n{text}\n</document>" for name, text in briefs)
    return (f"{DOC}\n\n# Tool API file\n<openapi>\n{json.dumps(openapi, indent=1)}\n</openapi>\n\n"
            f"# Bank materials\n{docs}\n\nProduce the spec JSON now.")


# ------------------------------------------------------------------------------------------- validation
LINE_KEYS = [k.strip() for k in DOC.split("Required keys:")[1].split("Meaning")[0].replace("\n", " ").split(",") if k.strip()]
EVENT_KEYS = ["promise", "paid", "transfer_restructuring", "transfer_human", "transfer_support", "refused", "hardship",
              "dispute", "service_error", "id_failed", "id_declined", "callback", "no_callback", "wrong_number",
              "deceased", "unclear"]
SPEC_KEYS = ["identity", "events", "lines"]
SLOTS = {"verify_identity": {"q_field", "q_answer"}, "get_account": set(),
         "create_promise": {"p_product", "p_amount", "p_date"}, "add_note": {"n_product", "n_reason"},
         "schedule_callback": {"cb_date", "cb_time"}, "transfer_to_agent": {"t_queue"},
         "close_call": {"c_outcome", "c_reason"}}


ALL_SLOTS = set().union(*SLOTS.values()) | {"cb_datetime"}
PLACEHOLDERS = {"title", "first_name", "last_name", "name", "amount", "date", "days", "time", "deadline", "floor", "full"}
STATEMENT_LINES = {"robot", "scam", "tp_no_info", "not_talk_once", "id_wrong", "id_why", "id_explain", "no_info",
                   "below_floor_again", "paid_line", "dispute_line", "recorded", "sms_line", "record_failed"}


def tool_required(openapi, tool):
    try:
        return openapi["paths"]["/" + tool]["post"]["requestBody"]["content"]["application/json"]["schema"].get("required", [])
    except KeyError:
        return []


def account_item(openapi, list_field):
    """Properties of one product record in the get_account response (list items, or the object itself)."""
    try:
        props = openapi["paths"]["/get_account"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]["properties"]
    except (KeyError, TypeError):
        return {}
    v = props.get(list_field) or {}
    if v.get("type") == "array":
        return v.get("items", {}).get("properties", {}) or {}
    return v.get("properties", {}) or {}


def tool_props(openapi, tool):
    try:
        return openapi["paths"]["/" + tool]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]
    except KeyError:
        return {}


def enum_of(openapi, tool, param):
    return tool_props(openapi, tool).get(param, {}).get("enum")


def validate(spec, openapi):
    errs = list(spec_model.check(spec))       # types, ranges, formats, bands, window (Pydantic)
    for k in ["calendar", "identity", "products", "bands", "ladder", "events", "tool_bodies", "lines", "callback", "confirm"]:
        if k not in spec:
            errs.append(f"missing key {k}")
    if errs:
        return errs
    for k in LINE_KEYS:
        if k not in spec["lines"]:
            errs.append(f"lines.{k} missing")
    for k in EVENT_KEYS:
        if k not in spec["events"]:
            errs.append(f"events.{k} missing")
    tb = spec["tool_bodies"]
    for tool, slots in SLOTS.items():
        if tool not in tb:
            errs.append(f"tool_bodies.{tool} missing")
            continue
        props = tool_props(openapi, tool)
        got = set(tb[tool].values())
        need = set(slots)
        if tool == "schedule_callback" and "cb_datetime" in got:
            need = {"cb_datetime"}
        if not need <= got and tool != "close_call":
            errs.append(f"tool_bodies.{tool} must map slots {sorted(slots)}; got {tb[tool]}")
        for param, slot in tb[tool].items():
            if param not in props:
                errs.append(f"tool_bodies.{tool}: '{param}' is not a field of {tool} in the tool API (fields: {sorted(props)})")
            if not (str(slot) in ALL_SLOTS or str(slot).startswith("const:")):
                errs.append(f"tool_bodies.{tool}.{param}: '{slot}' is not a slot (slots: {sorted(ALL_SLOTS)}, or const:<value>)")
        for req in tool_required(openapi, tool):
            if req != "customer_id" and req in props and req not in tb[tool]:
                errs.append(f"tool_bodies.{tool}: required field '{req}' of the tool API is not mapped (map it to a slot or const:<value>)")
    inv = {tool: {v: k for k, v in m.items()} for tool, m in tb.items()}

    def check_enum(tool, slot, value, where):
        param = inv.get(tool, {}).get(slot)
        en = enum_of(openapi, tool, param) if param else None
        if en and value not in ("", None) and value not in en:
            errs.append(f"{where}: '{value}' is not an allowed {tool}.{param} value {en}")

    for name, e in spec["events"].items():
        check_enum("close_call", "c_outcome", e.get("outcome"), f"events.{name}.outcome")
        check_enum("close_call", "c_reason", e.get("reason"), f"events.{name}.reason")
        if e.get("queue"):
            check_enum("transfer_to_agent", "t_queue", e["queue"], f"events.{name}.queue")
        if e.get("note"):
            check_enum("add_note", "n_reason", e["note"], f"events.{name}.note")
    for q in spec["identity"].get("questions", []):
        check_enum("verify_identity", "q_field", q.get("field"), "identity.questions.field")
        if q.get("kind") not in ("date", "digits", "year", "upper", "text", "email"):
            errs.append(f"identity question kind {q.get('kind')} invalid")
    if not spec["identity"].get("questions"):
        errs.append("identity.questions is empty")
    kinds = {"business_days_after", "calendar_days_after", "end_of_month", "end_of_next_month", "last_business_day_of_month", "fixed_date",
             "end_of_week", "end_of_next_week", "day_of_next_month", "business_days_after_or_end_of_month", "later_of", "earlier_of"}

    def rules_of(b):                          # every deadline rule of a band, nested ones included
        out, todo = [], [b.get("deadline") or {}] + list((b.get("deadline_by_type") or {}).values())
        while todo:
            r = todo.pop()
            out.append(r if isinstance(r, dict) else {})
            todo += [x for x in (r.get("of") or [])] if isinstance(r, dict) else []
        return out
    for b in spec["bands"]:
        for r in rules_of(b):
            if r.get("kind") not in kinds:
                errs.append(f"band {b.get('name')}: deadline kind {r.get('kind')!r} invalid")
    for s in spec["ladder"]:
        if s.get("amount") not in ("full", "floor", "full_or_floor") and not str(s.get("amount", "")).startswith("pct:"):
            errs.append(f"ladder amount {s.get('amount')} invalid")
    P_ = dict(comp.account_shape(openapi, spec), **{k: v for k, v in spec["products"].items() if k in ("list_field", "id_field", "type_field")})
    item = account_item(openapi, P_["list_field"])
    if item:
        for key, engine_default in (("amount_field", "past_due"), ("days_field", "days_late")):
            f = spec["products"].get(key) or engine_default      # the field the engine will actually read
            if f not in item:
                errs.append(f"products.{key} '{f}'" + ("" if spec["products"].get(key) else " (the engine default)") +
                            f" is not a field of the get_account product record (fields: {sorted(item)})")
        for t, f in (spec["products"].get("floor_field") or {}).items():
            if f and f not in item:
                errs.append(f"products.floor_field.{t} '{f}' is not a field of the get_account product record (fields: {sorted(item)})")
    ptypes = (item.get(P_["type_field"]) or {}).get("enum") if item else None
    for t in ptypes or []:
        if t not in spec["products"].get("names", {}) and "*" not in spec["products"].get("names", {}):
            errs.append(f"products.names has no spoken name for product type {t}")
        if t not in (spec["products"].get("floor_field") or {}) and "*" not in (spec["products"].get("floor_field") or {}) \
                and spec["products"].get("floor_pct") is None:
            errs.append(f"products.floor_field missing for product type {t}")
    LADDER_DATES = {"today", "deadline", "last_business_day_this_week", "last_business_day_next_week", "tomorrow", "next_business_day"}
    for st_ in list(spec["ladder"]) + [x for b in spec["bands"] for x in (b.get("ladder") or [])]:
        d = str(st_.get("date", "deadline"))
        if d not in LADDER_DATES and not re.fullmatch(r"(business_days_after|calendar_days_after):\d+", d):
            errs.append(f"ladder date '{d}' invalid; use one of {sorted(LADDER_DATES)} or business_days_after:N / calendar_days_after:N")
    for b in spec["bands"]:
        for dk in rules_of(b):
            if dk.get("kind") in ("business_days_after", "calendar_days_after", "day_of_next_month", "business_days_after_or_end_of_month") \
                    and not isinstance(dk.get("n"), int):
                errs.append(f"band {b.get('name')}: deadline kind {dk.get('kind')} needs an integer n")
            if dk.get("kind") == "fixed_date" and not re.fullmatch(r"\d{4}-\d\d-\d\d", str(dk.get("value", ""))):
                errs.append(f"band {b.get('name')}: fixed_date needs value YYYY-MM-DD")
    for d in spec["callback"].get("days", []):
        if d not in ("today", "next_business_day", "tomorrow") and not re.fullmatch(r"within_(business_)?days:\d+", str(d)):
            errs.append(f"callback.days item '{d}' invalid; use today, next_business_day, tomorrow, within_business_days:N or within_days:N")
    for path, allowed in (("rules.deadline_adjust", ("previous_business_day", "next_business_day", "none")),
                          ("date_rules.next_weekday", ("following_week", "upcoming")),
                          ("identity.date_order", ("dmy", "mdy")),
                          ("rules.first_ask", ("today", "deadline")),
                          ("rules.too_much_floor", ("attempt_1", "asked_date")),
                          ("products.floor_round", ("up_unit", "nearest_unit"))):
        v = get(spec, path)
        if v is not None and v not in allowed:
            errs.append(f"{path} '{v}' invalid; allowed {list(allowed)}")
    if (spec.get("rules") or {}).get("ladder_repeat_last") not in (None, True, False):
        errs.append("rules.ladder_repeat_last must be true or false")
    for q in spec["identity"].get("questions", []):
        if q.get("format") and q.get("kind") == "date" and not all(x in q["format"] for x in ("%Y", "%m", "%d")):
            errs.append(f"identity question {q.get('field')}: format must be a strftime pattern with %Y %m %d")
        if q.get("format") and q.get("kind") == "upper" and not re.fullmatch(r"space_before_last:\d+", str(q["format"])):
            errs.append(f"identity question {q.get('field')}: an upper code format must be space_before_last:N")
    vprops = comp.response_props(openapi, "verify_identity")
    if vprops:
        for key, f in (spec["identity"].get("response") or {}).items():
            if key in ("ok", "verified", "all_verified", "left", "hint") and f and f not in vprops:
                errs.append(f"identity.response.{key} '{f}' is not a field of the verify_identity response (fields: {sorted(vprops)})")
    for tool, r in (spec.get("responses") or {}).items():
        rp = comp.response_props(openapi, tool)
        if rp and r.get("field") and r["field"] not in rp:
            errs.append(f"responses.{tool}.field '{r['field']}' is not a field of the {tool} response (fields: {sorted(rp)})")
        en = (rp.get(r.get("field") or "") or {}).get("enum")
        for v in r.get("values") or []:
            if en and v not in en:
                errs.append(f"responses.{tool}.values: '{v}' is not one of {en}")
    if not (spec["events"].get("transfer_human") or {}).get("queue") and not spec["lines"].get("human_no_transfer"):
        errs.append("events.transfer_human has no queue: add lines.human_no_transfer (what to tell a customer who wants a person)")
    for name, e in spec["events"].items():
        if name.startswith("transfer_") and e.get("queue") is None and "queue" not in e:
            errs.append(f"events.{name}.queue missing (use \"\" if the brief does not transfer in this situation)")
    for k, v in spec["lines"].items():
        if k not in ("recorded", "sms_line", "record_failed") and re.search(r"\brecorded\b", str(v), re.I):
            errs.append(f"lines.{k} uses the word 'recorded'; a judge can hear it as a false claim that a promise was recorded. "
                        f"Rephrase in the active voice ('we record this call') or use 'noted'")
        if "{{" in str(v) or "}}" in str(v):
            errs.append(f"lines.{k} must use single braces")
        for ph in re.findall(r"\{([a-zA-Z_0-9]+)\}", str(v)):
            if ph not in PLACEHOLDERS and ph not in (spec.get("call_variables") or []):
                errs.append(f"lines.{k} uses {{{ph}}}, which the engine cannot fill (allowed: {sorted(PLACEHOLDERS)} "
                            f"and call_variables)")
        if k in STATEMENT_LINES and str(v).strip().endswith("?"):
            errs.append(f"lines.{k} must be a statement (another sentence follows it in the same turn), not a question")
    return errs


# ------------------------------------------------------------------------------------------- comparison
CORE = ["calendar", "date_rules", "confirm", "callback", "identity.questions", "identity.max_wrong", "identity.hints",
        "products.floor_field", "products.amount_field", "products.floor_pct", "bands", "ladder", "notes", "events",
        "tool_bodies", "multi", "human", "max_not_talk", "rules"]


def get(d, path):
    for p in path.split("."):
        d = d.get(p) if isinstance(d, dict) else None
    return d


def norm(v):
    if isinstance(v, list) and v and isinstance(v[0], dict):
        return [{k: x for k, x in i.items() if k not in ("ask", "name")} for i in v]
    if isinstance(v, list):
        return sorted(map(str, v)) if all(not isinstance(x, (dict, list)) for x in v) else v
    return v


def diff(a, b):
    out = []
    for path in CORE:
        if json.dumps(norm(get(a, path)), sort_keys=True) != json.dumps(norm(get(b, path)), sort_keys=True):
            out.append(path)
    return out


ADJ = """Two analysts produced specs from the same materials and disagree on these fields: {fields}.
Re-read the materials, decide each disputed field, quoting the sentence that decides it, then output the final
complete spec JSON (all keys), starting from spec A and replacing only what is wrong.

<spec_A>
{a}
</spec_A>
<spec_B>
{b}
</spec_B>

First write one line per disputed field: FIELD: decision -- "quote". Then the JSON in a ```json block."""

BEHAVIOUR = """The phone agent built from your spec was run against synthetic customers, and these scenarios ended
differently from what your spec's own events say they should:
{problems}
Some failures can come from the synthetic test itself (it uses generic data). Re-read the materials. If a failure shows a
real mistake in the spec (a wrong code, field, window, limit, missing line or rule), fix it. Never remove or loosen a
value the materials state (a length, a format, a limit, a code, a window) just to make a test pass: if the materials
decide it, keep it. Output the complete spec JSON in a ```json block."""

REPAIR = """Your spec has these problems:
{errs}
Fix them and output the complete corrected spec JSON in a ```json block."""


AUDIT = """You are auditing a spec that one analyst produced from the materials above. Check these high-risk
fields one by one against the documents: {fields}.
For each field: find the sentence(s) in the bank materials or the tool API file that decide it (a later memo or
dated change beats the rule it changes), quote them, and say whether the spec value carries out exactly what they
say. Then list every rule in the materials that the spec does not carry at all (a holiday, a band, a code, a
limit, a format, an exception).

<spec>
{spec}
</spec>

Write one line per field: FIELD: OK | WRONG -- "quote" -- what is wrong. Then a ```json block holding ONLY the
changes, as {{"path.to.field": new value, ...}} with dotted paths (list items by index, e.g. "bands.1.max", or a
whole value such as "calendar.holidays"). Use {{}} when nothing must change. Never change a value the materials
do not decide."""

AUDIT_FIELDS = CORE + ["identity", "products"]


def set_path(d, path, value):
    keys = path.split(".")
    for i, k in enumerate(keys[:-1]):
        if isinstance(d, list):
            d = d[int(k)]
            continue
        if not isinstance(d.get(k), (dict, list)):
            d[k] = [] if keys[i + 1].isdigit() else {}
        d = d[k]
    last = keys[-1]
    if isinstance(d, list):
        if int(last) < len(d):
            d[int(last)] = value
        elif int(last) == len(d):
            d.append(value)
        else:
            raise IndexError(path)
    else:
        d[last] = value


def read_and_audit(base, trace):
    """Mode B: one reading at the highest effort, then a targeted audit of the high-risk fields against the
    source documents. The audit answers with changes only, so nothing it did not look at can be lost."""
    effort = os.environ.get("BUILDER_B_EFFORT", "max")
    out = claude.ask(base, SYSTEM, f"read the brief and tool API into a spec (single reading, effort {effort})", trace, effort=effort)
    spec = claude.json_block(out, keys=SPEC_KEYS)
    audit = claude.ask(base + "\n\n" + AUDIT.format(fields=", ".join(AUDIT_FIELDS), spec=json.dumps(spec, indent=1)),
                       SYSTEM, "audit the high-risk fields of the reading against the source documents", trace)
    trace.note("audit: " + audit.split("```")[0].strip()[:3000])
    try:
        changes = claude.json_block(audit)
    except Exception as e:
        trace.note(f"audit answer did not parse ({e}); keeping the reading")
        return spec
    fixed = copy.deepcopy(spec)
    for path, value in changes.items():
        try:
            set_path(fixed, path, value)
        except Exception as e:
            trace.note(f"audit change {path} not applied ({type(e).__name__})")
    trace.note(f"audit changed: {sorted(changes) or 'nothing'}")
    return fixed


def extract(briefs, openapi, trace, budget=None, mode=None):
    """mode A (default): two independent readings + adjudication. mode B: one strong reading + targeted audit
    (BUILDER_EXTRACT_MODE=B). Both then share the same validation loop and repair budget."""
    base = prompt_for(briefs, openapi)
    mode = mode or os.environ.get("BUILDER_EXTRACT_MODE", "A")
    trace.note(f"extraction mode {mode}")
    if mode == "B":
        return validate_loop(read_and_audit(base, trace), base, openapi, trace, budget)
    texts = claude.ask_many([(base, SYSTEM, "read the brief and tool API into a spec (reading 1)"),
                             (base, SYSTEM, "read the brief and tool API into a spec (reading 2, independent)")], trace)
    specs = []
    for t in texts:
        try:
            specs.append(claude.json_block(t, keys=SPEC_KEYS))
        except Exception as e:
            trace.note(f"a reading did not parse as JSON: {e}")
    if not specs:
        raise RuntimeError("no reading produced a spec")
    spec = specs[0]
    if len(specs) == 2:
        d = diff(specs[0], specs[1])
        trace.note(f"readings disagree on: {d or 'nothing'}")
        if d:
            out = claude.ask(base + "\n\n" + ADJ.format(fields=", ".join(d), a=json.dumps(specs[0], indent=1), b=json.dumps(specs[1], indent=1)),
                             SYSTEM, "adjudicate disagreements between the two readings: " + ", ".join(d), trace)
            trace.note("adjudication: " + out.split("```")[0].strip()[:3000])
            try:
                spec = claude.json_block(out, keys=SPEC_KEYS)
            except Exception as e:
                trace.note(f"adjudication answer did not parse ({e}); keeping reading 1")
    return validate_loop(spec, base, openapi, trace, budget)


def validate_loop(spec, base, openapi, trace, budget):
    for attempt in range(10):                 # bounded by the shared repair budget
        spec_model.normalise(spec)
        errs = validate(spec, openapi)
        trace.note(f"validation round {attempt + 1}: {len(errs)} problem(s)" + ("".join("\n  - " + e for e in errs)))
        if not errs or (budget is not None and not budget.take(trace, f"repair {len(errs)} validation problem(s)")):
            break
        if budget is None and attempt >= 2:
            break
        out = claude.ask(base + "\n\n<your_spec>\n" + json.dumps(spec, indent=1) + "\n</your_spec>\n\n" + REPAIR.format(errs="\n".join("- " + e for e in errs)),
                         SYSTEM, f"repair {len(errs)} validation problem(s) in the spec", trace)
        try:
            fixed = claude.json_block(out, keys=SPEC_KEYS)
        except Exception as e:
            trace.note(f"repair answer did not parse ({e}); keeping the previous spec")
            continue
        if len(validate(fixed, openapi)) <= len(errs):
            spec = fixed
    return spec


def repair_behaviour(spec, problems, briefs, openapi, trace):
    """One repair round driven by the local self-test (the engine run against synthetic customers)."""
    base = prompt_for(briefs, openapi)
    out = claude.ask(base + "\n\n<your_spec>\n" + json.dumps(spec, indent=1) + "\n</your_spec>\n\n" + BEHAVIOUR.format(problems=problems),
                     SYSTEM, "repair the spec after failed local self-test scenarios", trace)
    return claude.json_block(out, keys=SPEC_KEYS)
