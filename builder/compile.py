"""Spec -> Freya workflow JSON. Deterministic: same spec in, same workflow out.

Graph:  step(code, initial) --next--> talk_<stage> (tts {{say}} + extraction) --always--> step
                            --next--> api_<tool>  (engine-run bank call)        --always--> step
                            --next--> api_close   (tts goodbye + close_call)    -----------> end
"""
import ast
import io
import json
import os
import pathlib
import re
import tokenize

RUNTIME = pathlib.Path(__file__).resolve().parent / "runtime"

# --------------------------------------------------------------------------------------------- code node
class _StripDocs(ast.NodeTransformer):
    """Drops every bare string statement (docstrings, the module notes)."""
    def generic_visit(self, node):
        super().generic_visit(node)
        for field in ("body", "orelse", "finalbody"):
            b = getattr(node, field, None)
            if isinstance(b, list) and b and all(isinstance(x, ast.stmt) for x in b):
                kept = [x for x in b if not (isinstance(x, ast.Expr) and isinstance(x.value, ast.Constant) and isinstance(x.value.value, str))]
                setattr(node, field, kept or ([ast.Pass()] if field == "body" else []))
        return node


def compact(src):
    """Tokens joined without optional spaces, one space per indentation level."""
    out, line, indent, prev = [], [], 0, ""
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        tt, t = tok.type, tok.string
        if tt == tokenize.INDENT:
            indent += 1
            continue
        if tt == tokenize.DEDENT:
            indent -= 1
            continue
        if tt in (tokenize.NEWLINE, tokenize.NL):
            if line:
                out.append(" " * indent + "".join(line))
            line, prev = [], ""
            continue
        if tt in (tokenize.COMMENT, tokenize.ENDMARKER, tokenize.ENCODING):
            continue
        if prev and (prev[-1].isalnum() or prev[-1] == "_") and (t[:1].isalnum() or t[:1] == "_"):
            line.append(" ")
        line.append(t)
        prev = t
    return "\n".join(out) + "\n"


_MINI = dict()


def minify(src):
    if src not in _MINI:
        _MINI[src] = _minify(src)
    return _MINI[src]


def _minify(src):
    """Python source without comments/docstrings, optional spaces or deep indentation: the code node's code and
    data must stay under the platform's 128 KB limit for a sandboxed process. The result must parse to the same
    program (same AST), else the plainer form is used."""
    tree = _StripDocs().visit(ast.parse(src))
    plain = ast.unparse(tree)
    try:
        small = compact(plain)
        if ast.dump(ast.parse(small)) == ast.dump(ast.parse(plain)):
            return small
    except Exception:
        pass
    return plain


def embed_json(obj):
    """JSON text that holds no double braces (allowed whitespace between structural braces), else hex."""
    t = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    t = re.sub(r'"(?:[^"\\]|\\.)*"', lambda m: m.group(0).replace("{", "\\u007b").replace("}", "\\u007d")
               if ("{{" in m.group(0) or "}}" in m.group(0)) else m.group(0), t)
    t2 = t.replace("}}", "} }").replace("{{", "{ {")
    while "}}" in t2 or "{{" in t2:
        t2 = t2.replace("}}", "} }").replace("{{", "{ {")
    if json.loads(t2) == obj:
        return "json.loads(" + repr(t2) + ")"
    return "json.loads(bytes.fromhex('" + t.encode().hex() + "').decode())"


ENGINE_UNUSED = ("assumptions", "tool_bodies", "intents", "system_prompt", "test_variables")
_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
LZW_DECODER = """def _lzw(a, s):
    B = dict((c, i) for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"))
    d = list(a)
    k = [(B[s[i]] << 12) | (B[s[i + 1]] << 6) | B[s[i + 2]] for i in range(0, len(s), 3)]
    w = d[k[0]]
    o = [w]
    for x in k[1:]:
        e = d[x] if x < len(d) else w + w[0]
        o.append(e)
        d.append(w + e[0])
        w = e
    return "".join(o)
"""


def lzw_pack(src):
    """LZW over characters, each code as 3 base64 characters (codes < 2**18). Returns (alphabet, packed)."""
    alpha = "".join(sorted(set(src)))
    d = dict((ch, i) for i, ch in enumerate(alpha))
    w, out = "", []
    for ch in src:
        wc = w + ch
        if wc in d:
            w = wc
            continue
        out.append(d[w])
        if len(d) < 2 ** 18:
            d[wc] = len(d)
        w = ch
    if w:
        out.append(d[w])
    packed = "".join(_B64[(x >> 12) & 63] + _B64[(x >> 6) & 63] + _B64[x & 63] for x in out)
    ns = dict()
    exec(LZW_DECODER, ns)
    assert ns["_lzw"](alpha, packed) == src
    return alpha, packed


CODE_LIMIT = 100_000      # platform limit 131072 bytes for code + call variables; leave room for the variables


def one_line_literal(t):
    """A one-line Python string literal for t, quoted with whichever quote needs fewer escapes."""
    q = '"' if t.count('"') < t.count("'") else "'"
    esc = t.replace("\\", "\\\\").replace(q, "\\" + q).replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    # control characters and Unicode line separators (a PDF form feed, a pasted U+2028) would end the line or the
    # source when the platform splits and indents the code: write them as escapes
    esc = re.sub("[\x00-\x1f\x7f\x85\u2028\u2029]", lambda m: "\\u%04x" % ord(m.group(0)), esc)
    lit = q + esc + q
    assert eval(lit) == t
    return lit


def engine_source(spec):
    src = pathlib.Path(os.environ.get("ENGINE_SRC_DIR") or RUNTIME)    # mutation-testing trial: embed the pristine source
    cal = (src / "cal.py").read_text()
    eng = (src / "engine.py").read_text()
    eng = re.sub(r"try:\s+# local tests.*?except Exception:\n    pass\n", "", eng, flags=re.S)
    eng = eng.replace("S = dict()   # injected spec", "")
    cal = cal.replace("P = dict(workdays=[0, 1, 2, 3, 4], holidays=[])", "")
    params = dict(workdays=spec["calendar"]["workdays"], holidays=spec["calendar"]["holidays"])
    body = minify(cal + "\n" + eng)
    used = {k: v for k, v in spec.items() if k not in ENGINE_UNUSED}
    head = "import json\nP = " + embed_json(params) + "\nS = " + embed_json(used) + "\n"
    assert "{{" not in head + body and "}}" not in head + body
    # The engine is LZW-compressed (no zlib in the sandbox) and unpacked by a few lines of plain Python.
    # Freya may exec the snippet with separate globals/locals: everything runs in one namespace (_G).
    # One physical line per statement: the platform wraps the code in a function and indents every line.
    alpha, packed = lzw_pack(body)
    src = ("_G = dict()\n"
           "exec(" + one_line_literal(LZW_DECODER) + ", _G)\n"
           "_SRC = " + one_line_literal(head) + " + _G['_lzw'](" + one_line_literal(alpha) + ", " + one_line_literal(packed) + ")\n"
           "exec(_SRC, _G)\n"
           "_G['main'](vars)\n")
    assert "{{" not in src and "}}" not in src
    if len(src.encode()) > CODE_LIMIT:
        raise ValueError(f"engine code is {len(src.encode())} bytes, over the {CODE_LIMIT}-byte budget for the code node")
    return src


def code_step(spec, routes):
    src = engine_source(spec)
    err = lambda extra: {"logic": "or", "conditions": [{"logic": "and", "conditions": [
        {"variable": "step_response.status", "operator": "equals", "operand": "error"}] + extra}]}
    # A failed or timed-out code run writes no variables, so `next` would still name the previous target and replay
    # it. Retry the run instead; after repeated failures close the call safely with the engine's fallback outcome.
    trans = [{"target_node": "api_close_fb_nr", "description": "the engine keeps failing (fallback outcome known, no reason)",
              "condition": err([{"variable": "step_failure_count", "operator": "greater_than", "operand": 9},
                                {"variable": "fb_nr", "operator": "equals", "operand": "yes"}])},
             {"target_node": "api_close_fb", "description": "the engine keeps failing (fallback outcome known)",
              "condition": err([{"variable": "step_failure_count", "operator": "greater_than", "operand": 9},
                                {"variable": "fb_outcome", "operator": "is_set"}])},
             {"target_node": "api_close_fb0", "description": "the engine keeps failing before it ever ran",
              "condition": err([{"variable": "step_failure_count", "operator": "greater_than", "operand": 9}])},
             {"target_node": "step", "description": "retry a failed engine run", "condition": err([])}]
    trans += [{"target_node": r, "description": "engine routes to " + r,
               "condition": {"logic": "or", "conditions": [{"variable": "next", "operator": "equals", "operand": r}]}}
              for r in routes]
    trans.append({"target_node": "api_close", "description": "fallback", "condition": {"operator": "always"}})
    return {"name": "step", "node_type": "code", "passthrough": True, "passthrough_default_transition": "api_close",
            "on_enter": {"type": "silent"}, "disable_inactivity_monitor": True,
            "pre_actions": [{"type": "function", "function": {"name": "code_exec", "handler": "__function__:code_exec",
                             "parameters": {"type": "object", "properties": {
                                 "code": {"type": "string", "default": src},
                                 "timeout_ms": {"type": "number", "default": 30000}}}}}],
            "transitions": trans}


# --------------------------------------------------------------------------------------------- talk nodes
KINDS = {
    "open": {
        "confirmed": "the person confirms they are the customer: yes, that's me, speaking, or says the customer's own name",
        "third_party": "someone else answered (a relative, partner, colleague, child) or says the customer is not available",
        "wrong_number": "says this number does not belong to the customer or they do not know the customer at all (a 'no' because someone else answered, e.g. a relative, is third_party)",
        "deceased": "says the customer has died / passed away",
        "robot": "asks whether you are a robot, a machine or an AI",
        "scam": "asks whether this call is a scam or fraud, or doubts that you are the bank",
        "purpose": "asks what the call is about or who is calling, without confirming",
        "human": "asks to speak to a real person / a human agent",
        "not_talk": "says they do not want to talk / are not interested / asks you to stop",
        "unclear": "anything else: hello, go ahead, who is this, unclear or no answer",
    },
    "tp": {
        "gives_time": "gives a day and/or time when the customer can be reached",
        "no_time": "does not know when the customer can be reached, or will not say",
        "customer_now": "says the customer is coming to the phone or hands the phone over",
        "wrong_number": "says the number does not belong to the customer / does not know them",
        "deceased": "says the customer has died / passed away",
        "purpose": "asks what the call is about",
        "offers_pay": "offers to pay or to answer questions for the customer",
        "robot": "asks whether you are a robot or an AI",
        "scam": "asks whether this is a scam",
        "unclear": "anything else",
    },
    "id": {
        "answer": "gives an answer to the security question (any date, number, digits or words), even partial",
        "refuses": "refuses or does not want to answer the security question",
        "asks_why": "asks why you need this information or why you ask, without answering",
        "purpose": "asks what the call is about or which account it concerns, without answering",
        "robot": "asks whether you are a robot or an AI",
        "scam": "asks whether this is a scam",
        "human": "asks to speak to a human",
        "not_talk": "says they do not want to talk",
        "wrong_person": "says they are not the customer after all",
        "unclear": "anything else",
    },
    "nego": {
        "yes": "agrees / accepts what was just offered or asked (yes, ok, fine, that works, I confirm)",
        "no": "says no / cannot / will not, without giving a date or an amount",
        "proposes": "names a day, a date or an amount they can pay (the date of an unrelated event, e.g. a wedding or a trip, is not a proposal: use unclear)",
        "too_much": "says the amount is too much / too high / cannot afford it, even if they also say 'you tell me'",
        "vague": "gives a vague time like next week, end of the month, when my salary comes, without an exact day",
        "already_paid": "says they already paid this amount",
        "dispute": "says the debt is not theirs, they never had this product, or it was closed or paid off in the past",
        "hardship": "mentions illness, a death in the family, or job loss",
        "restructure": "asks for a new payment plan, lower instalments, or restructuring",
        "human": "asks to speak to a human",
        "robot": "asks whether you are a robot or an AI",
        "scam": "asks whether this is a scam",
        "not_talk": "says they do not want to talk about this",
        "asks_amount": "asks how much they should or can pay, what the options are, or says 'you tell me', without refusing in the same message",
        "details": "asks which account or product this is, how much they owe, how late it is, or asks you to repeat the amount",
        "question": "asks another question",
        "unclear": "anything else",
    },
    "hardship": {
        "yes": "accepts being connected / transferred",
        "no": "declines being connected / transferred",
        "proposes": "names a date or an amount they can pay themselves",
        "robot": "asks whether you are a robot or an AI",
        "scam": "asks whether this is a scam",
        "question": "asks a question",
        "unclear": "anything else",
    },
    "confirm": {
        "yes": "confirms the read-back (yes, correct, I confirm)",
        "no": "does not confirm / refuses",
        "proposes": "changes the date or the amount",
        "too_much": "now says the amount is too much",
        "already_paid": "says they already paid",
        "dispute": "says the debt is not theirs",
        "hardship": "mentions illness, a death in the family, or job loss",
        "restructure": "asks for a new payment plan or lower instalments",
        "human": "asks to speak to a human",
        "robot": "asks whether you are a robot or an AI",
        "scam": "asks whether this is a scam",
        "question": "asks another question",
        "unclear": "anything else",
    },
    "after": {
        "done": "nothing else / no thanks / goodbye / thanks",
        "change": "wants to change or cancel the promise just recorded (date or amount)",
        "restructure": "asks for a new payment plan, lower instalments or restructuring",
        "human": "asks to speak to a human",
        "hardship": "mentions illness, a death in the family, or job loss",
        "robot": "asks whether you are a robot or an AI",
        "scam": "asks whether this is a scam",
        "yes": "says yes (accepts what was just offered)",
        "question": "asks a question",
        "unclear": "anything else",
    },
}


OVERRIDABLE = ("hardship", "dispute", "already_paid", "restructure", "vague", "too_much", "human")


def kinds_for(stage, spec):
    """The stage's intents; the brief's own definitions (spec.intents) replace the defaults where given."""
    kinds = dict(KINDS[stage])
    if stage in ("nego", "confirm") and (spec or {}).get("events", {}).get("callback_requested", {}).get("outcome"):
        kinds = dict(list(kinds.items())[:-2] + [("call_later", "asks to be called back another time instead of deciding now")]
                     + list(kinds.items())[-2:])
    for k, v in ((spec or {}).get("intents") or {}).items():
        if k in OVERRIDABLE and k in kinds and isinstance(v, str) and v.strip() and stage in ("nego", "confirm", "after"):
            kinds[k] = v.strip().replace("{", "(").replace("}", ")")
    if "hardship" in kinds:              # chatty customers talk about their day: that is not a hardship disclosure
        kinds["hardship"] += " (small talk about being busy, tired, stressed or having a lot of work is NOT this: use unclear)"
    return kinds


def extraction(stage, spec):
    kinds = kinds_for(stage, spec)
    desc = ("Classify ONLY the customer's latest message. Values: " +
            "; ".join(f"{k} = {v}" for k, v in kinds.items()) + "; none = nothing new was said.")
    schema = [{"name": stage + "_kind", "type": "string", "enum_values": list(kinds) + ["none"], "description": desc, "min_confidence": 0.0},
              {"name": stage + "_cut", "type": "string", "enum_values": ["yes", "no"], "min_confidence": 0.0,
               "description": "yes = the customer's latest message stops in the middle of a sentence, ending on an unfinished phrase "
                              "(e.g. '... I would like', '... I want to', '... sorry, I mean', '... on the'), so they have not finished; "
                              "no = it is a complete thought."},
              {"name": stage + "_side", "type": "string", "enum_values": ["robot", "scam", "question", "none"], "min_confidence": 0.0,
               "description": "Does the customer's latest message ALSO contain a side question, besides its main point? "
                              "robot = asks if you are a robot, a machine or an AI; scam = asks if this is a scam or doubts you are the bank; "
                              "question = asks another question you should answer; none = no side question."}]
    if stage == "id":
        schema.append({"name": stage + "_text", "type": "string", "min_confidence": 0.0,
                       "description": "The customer's answer to the security question in their latest message, copied exactly as said "
                                      "(keep every digit and word, e.g. 'the fourteenth of March 1988' or 'four four seven one'). Empty if none."})
    if stage == "id":
        schema.append({"name": "id_norm", "type": "string", "min_confidence": 0.0,
                       "description": "The same answer normalised: a date as YYYY-MM-DD (day before month when only numbers are said), "
                                      "a number or code as its digits or characters only, without spaces. Empty if no answer."})
    if stage in ("tp",):
        schema.append({"name": stage + "_text", "type": "string", "min_confidence": 0.0,
                       "description": "The day and time the customer can be reached, copied exactly as said in the latest message, e.g. 'tomorrow around eleven'. Empty if none."})
    if stage in ("nego", "hardship", "confirm"):
        schema.append({"name": stage + "_text", "type": "string", "min_confidence": 0.0,
                       "description": "The customer's latest message, copied exactly as written, including how it ends."})
        schema.append({"name": stage + "_date", "type": "string", "min_confidence": 0.0,
                       "description": "The day or date the customer OFFERS TO PAY on in the latest message (not dates of other events), copied exactly as said "
                                      "(e.g. 'this Friday', 'next Tuesday', 'the 23rd', '30 October', 'next week'). If they correct themselves, "
                                      "only the corrected value. Empty if no day is mentioned."})
        schema.append({"name": stage + "_amount", "type": "float", "min_confidence": 0.0,
                       "description": "An amount of money the customer offers or agrees to pay in the latest message, as a number "
                                      "(e.g. 'six hundred thirty-seven fifty' = 637.5). If they correct themselves, the corrected value. Empty if none."})
        names = {t: n for t, n in (spec.get("products", {}).get("names", {}) if spec else {}).items() if t != "*"}
        if len(names) > 1:
            spoken = "; ".join(f"{t} = the {re.sub(r'[{][^}]*[}]', '', n).replace('ending', '').strip()}" for t, n in names.items())
            schema.append({"name": stage + "_for", "type": "string", "enum_values": list(names) + ["all", "none"], "min_confidence": 0.0,
                           "description": "Which product the customer's date, amount or yes/no in the latest message is about. "
                                          f"{spoken}; all = all of their products together (both, all three, everything); "
                                          "none = not stated. If they accept one product and refuse the others, give the product they accept."})
    return schema


def talk_node(stage, spec):
    return {"name": "talk_" + stage, "node_type": "conversation",
            "on_enter": {"type": "tts", "text": "{{say}}"},
            "task_message": "Repeat exactly this sentence and nothing else: {{say}}\nDo not call a tool in this turn.",
            "extraction_schema": extraction(stage, spec),
            "max_node_turns": 30,
            "transitions": [{"target_node": "step", "description": "always", "condition": {"operator": "always"}}]}


# --------------------------------------------------------------------------------------------- api nodes
def api_node(node, tool, url, body, types, nxt="step", tts=None, extra_transitions=None):
    """An engine-run bank call. No pre/post hooks: every hook is an extra sandbox run (seconds under load).
    Body fields take their JSON type from the tool API file, so numbers are sent as numbers."""
    props = {k: {"type": types.get(k, "string"), "default": const_value(v, types.get(k, "string"))} for k, v in body.items()}
    trans = list(extra_transitions or []) + [{"target_node": nxt, "description": "always", "condition": {"operator": "always"}}]
    return {"name": node, "node_type": "api_request", "passthrough": True, "passthrough_default_transition": nxt,
            "on_enter": {"type": "tts", "text": tts} if tts else {"type": "silent"}, "disable_inactivity_monitor": True,
            "pre_actions": [{"type": "function", "config": {"timeoutMs": 12000, "maxRetries": 0},
                             "function": {"name": tool, "handler": "__function__:api_call", "description": tool,
                                          "parameters": {"type": "object", "properties": {
                                              "url": {"type": "string", "default": url},
                                              "method": {"type": "string", "default": "POST"},
                                              "headers": {"type": "object", "properties": {"Content-Type": {"type": "string", "default": "application/json"}}},
                                              "body": {"type": "object", "properties": props}}}}}],
            "transitions": trans}


def const_value(v, typ):
    """'{{slot}}' stays a placeholder; a 'const:<value>' mapping becomes the literal, typed as the tool API says."""
    if not (isinstance(v, str) and v.startswith("const:")):
        return v
    lit = v[6:]
    if typ == "number":
        try:
            return float(lit) if "." in lit else int(lit)
        except ValueError:
            return lit
    if typ == "boolean":
        return lit.strip().lower() == "true"
    return lit


API_NODES = {"api_verify": "verify_identity", "api_account": "get_account", "api_promise": "create_promise",
             "api_note": "add_note", "api_callback": "schedule_callback", "api_transfer": "transfer_to_agent",
             "api_close": "close_call", "api_close_nr": "close_call",
             # extra copies for planned chains (several promises / notes in a row without a code run in between)
             "api_promise_2": "create_promise", "api_promise_3": "create_promise", "api_note_2": "add_note", "api_note_3": "add_note"}
CHAIN_NODES = ["api_promise", "api_promise_2", "api_promise_3", "api_note", "api_note_2", "api_note_3", "api_callback", "api_transfer"]


def chain_transitions(node, targets, success):
    """node -> T when the call succeeded and the engine planned T next (plan_<node> == T)."""
    field, values = success
    out = []
    for t in targets:
        if t == node:
            continue
        groups = []
        if field:
            for v in values:
                for form in sorted({str(v), str(v).lower(), str(v).capitalize()}):
                    groups.append({"logic": "and", "conditions": [
                        {"variable": f"{node}_response.data.{field}", "operator": "equals", "operand": form},
                        {"variable": f"plan_{node}", "operator": "equals", "operand": t}]})
        else:
            groups.append({"logic": "and", "conditions": [
                {"variable": f"{node}_response.status_code", "operator": "equals", "operand": "200"},
                {"variable": f"plan_{node}", "operator": "equals", "operand": t}]})
        out.append({"target_node": t, "description": f"planned next: {t}", "condition": {"logic": "or", "conditions": groups}})
    return out


def account_shape(openapi, spec):
    """Field names of the get_account response, read from the tool API file (not assumed):
    the array of product objects, the product id field and the product type field."""
    out = dict(list_field="products", id_field="product_id", type_field="type")
    try:
        props = openapi["paths"]["/get_account"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]["properties"]
    except (KeyError, TypeError):
        return out
    arrays = [(k, v) for k, v in props.items() if v.get("type") == "array" and v.get("items", {}).get("type", "object") == "object"]
    if not arrays:
        return out
    lf, arr = arrays[0]
    item = arr.get("items", {}).get("properties", {})
    out["list_field"] = lf
    promise_id = [k for k, v in spec.get("tool_bodies", {}).get("create_promise", {}).items() if v == "p_product"]
    ids = [k for k in item if k in promise_id] or [k for k in item if k.endswith("_id") or k == "id"] or \
          [k for k in item if "id" in k.lower()]
    if ids:
        out["id_field"] = ids[0]
    types = [k for k, v in item.items() if v.get("enum") and k in ("type", "product_type", "kind", "product")] or \
            [k for k, v in item.items() if v.get("enum")]
    if types:
        out["type_field"] = types[0]
    return out


PASS_RE = (r"^(ok|success|successful|succeeded|created|recorded|scheduled|booked|done|accepted|transferred|queued|added|"
           r"saved|confirmed|pass|passed|verified|match|matched|correct|valid)$")


def response_props(openapi, tool):
    try:
        return openapi["paths"]["/" + tool]["post"]["responses"]["200"]["content"]["application/json"]["schema"].get("properties", {}) or {}
    except (KeyError, TypeError, AttributeError):
        return {}


def response_shape(openapi):
    """Per tool, the response field that says the call succeeded, read from the tool API file:
    a boolean ok/success flag, or a status enum and its success values; none -> HTTP status decides."""
    out = {}
    for tool in sorted(set(API_NODES.values())):
        props = response_props(openapi, tool)
        flags = [k for k, v in props.items() if v.get("type") == "boolean" and k.lower() in ("ok", "success", "succeeded", "successful")]
        if flags:
            out[tool] = {"field": flags[0], "values": ["true"]}
            continue
        out[tool] = {"field": "", "values": []}
        for k in ("status", "result", "state", "outcome"):
            en = (props.get(k) or {}).get("enum")
            vals = [e for e in en or [] if re.match(PASS_RE, str(e).lower())]
            if vals:
                out[tool] = {"field": k, "values": vals}
                break
    return out


def apply_defaults(spec, openapi):
    """Fill what the tool API file decides and the spec left out (in place): response field names and
    success flags, the shape of the account record, the verify pass values of a non-boolean field."""
    from builder import spec_model
    spec_model.normalise(spec)                # lossless format fixes only ('9:00' -> '09:00')
    spec.setdefault("products", {}).update({k: v for k, v in account_shape(openapi, spec).items()
                                            if k not in spec["products"]})
    rs = spec.setdefault("responses", {})
    for tool, v in response_shape(openapi).items():
        rp = response_props(openapi, tool)
        if tool in rs and rp and rs[tool].get("field") and rs[tool]["field"] not in rp:
            rs[tool] = v                     # a field the tool API does not have: use what the API declares
        rs.setdefault(tool, v)
    R_ = spec.setdefault("identity", {}).setdefault("response", {})
    vf = response_props(openapi, "verify_identity").get(R_.get("verified", "verified")) or {}
    if vf.get("enum") and not R_.get("pass_values"):
        R_["pass_values"] = [e for e in vf["enum"] if re.match(PASS_RE, str(e).lower())]
    return spec


def compile_workflow(spec, openapi):
    spec = apply_defaults(json.loads(json.dumps(spec)), openapi)
    base = openapi["servers"][0]["url"].rstrip("/")
    bodies = spec["tool_bodies"]
    nodes = {}
    stages = ["open", "tp", "id", "nego", "hardship", "confirm", "after"]
    for st in stages:
        nodes["talk_" + st] = talk_node(st, spec)
    first = spec["lines"]["opening"] + " " + spec["lines"]["confirm_ask"]
    for v in ["title", "first_name", "last_name"] + [x for x in spec.get("call_variables", []) or [] if x != "now"]:
        first = first.replace("{" + v + "}", "{{" + v + "}}")
    first = re.sub(r"(?<!\{)\{[a-z_0-9]+\}(?!\})", "", first)       # never speak an unfilled placeholder
    nodes["talk_open"]["on_enter"] = {"type": "tts", "text": first}
    nodes["talk_open"]["on_enter_first_entry_only"] = True
    nodes["talk_open_again"] = dict(talk_node("open", spec), name="talk_open_again")
    allv = spec["identity"].get("response", {}).get("all_verified")
    for node, tool in API_NODES.items():
        schema = openapi["paths"]["/" + tool]["post"]["requestBody"]["content"]["application/json"]["schema"]
        types = {k: ("number" if v.get("type") in ("number", "integer") else v.get("type", "string"))
                 for k, v in schema.get("properties", {}).items()}
        types = {k: (t if t in ("string", "number", "boolean") else "string") for k, t in types.items()}
        mapping = dict(bodies.get(tool, {}))
        if node == "api_close_nr":      # close without a reason field (outcomes whose table lists no reason)
            mapping = {k: v for k, v in mapping.items() if v != "c_reason"}
        sfx = re.search(r"_[23]$", node).group(0) if re.search(r"_[23]$", node) else ""
        body = {k: (v if str(v).startswith("const:") else "{{" + v + sfx + "}}") for k, v in mapping.items()}
        if "customer_id" in schema.get("properties", {}):
            body["customer_id"] = "{{customer_id}}"
        if node in ("api_close", "api_close_nr"):
            nodes[node] = api_node(node, tool, f"{base}/{tool}", body, types, nxt="end", tts="{{say}}")
            if node == "api_close":
                sorry = ((spec["lines"].get("system_error") or "I'm sorry, we have a technical problem and need to end the call. "
                          "Please call us back.") + " " + spec["lines"].get("goodbye", "")).strip()
                sorry = re.sub(r"\{[a-z_0-9]+\}", "", sorry)
                fb = {k: v.replace("{{c_outcome}}", "{{fb_outcome}}").replace("{{c_reason}}", "{{fb_reason}}") for k, v in body.items()}
                nodes["api_close_fb"] = api_node("api_close_fb", tool, f"{base}/{tool}", fb, types, nxt="end", tts=sorry)
                fb_nr = {k: v for k, v in fb.items() if v != "{{fb_reason}}"}
                nodes["api_close_fb_nr"] = api_node("api_close_fb_nr", tool, f"{base}/{tool}", fb_nr, types, nxt="end", tts=sorry)
                un = spec["events"].get("unclear", {})
                fb0 = {k: (v.replace("{{c_outcome}}", "const:" + un.get("outcome", "")).replace("{{c_reason}}", "const:" + (un.get("reason") or ""))
                           if v in ("{{c_outcome}}", "{{c_reason}}") else v) for k, v in body.items()}
                fb0 = {k: v for k, v in fb0.items() if v != "const:"}
                nodes["api_close_fb0"] = api_node("api_close_fb0", tool, f"{base}/{tool}", fb0, types, nxt="end", tts=sorry)
        elif node == "api_verify" and allv:
            # all questions passed: go straight to the account lookup (saves one code run before the disclosure)
            direct = [{"target_node": "api_account", "description": "identity fully verified",
                       "condition": {"logic": "or", "conditions": [
                           {"variable": f"api_verify_response.data.{allv}", "operator": "equals", "operand": "true"},
                           {"variable": f"api_verify_response.data.{allv}", "operator": "equals", "operand": "True"}]}}]
            nodes[node] = api_node(node, tool, f"{base}/{tool}", body, types, extra_transitions=direct)
        elif node in CHAIN_NODES:
            targets = CHAIN_NODES + ["talk_" + s for s in stages if s != "open"] + ["talk_open_again", "api_close", "api_close_nr"]
            r = spec.get("responses", {}).get(tool) or {"field": "ok", "values": ["true"]}
            nodes[node] = api_node(node, tool, f"{base}/{tool}", body, types,
                                   extra_transitions=chain_transitions(node, targets, (r.get("field"), r.get("values") or ["true"])))
        else:
            nodes[node] = api_node(node, tool, f"{base}/{tool}", body, types)
    routes = ["talk_" + s for s in stages if s != "open"] + ["talk_open_again"] + [n for n in API_NODES if not re.search(r"_[23]$", n)]
    nodes["step"] = code_step(spec, routes)
    nodes["end"] = {"name": "end", "node_type": "end_call", "on_enter": {"type": "silent"}, "disable_inactivity_monitor": True,
                    "transitions": [], "terminal": {"type": "function", "function": {
                        "name": "call_closed", "handler": "__function__:end_call",
                        "parameters": {"type": "object", "properties": {"message_mode": {"type": "string", "default": "none"}}}}}}
    return {
        "initial_node": "talk_open",
        "system_prompt": spec.get("system_prompt", "You are a bank's digital assistant on a phone call. Speak English."),
        "nodes": nodes,
        "global_nodes": {},
        "test_variables": spec.get("test_variables", {"customer_id": "c_test", "first_name": "Daniel", "last_name": "Reyes",
                                                      "title": "Mr", "now": "2026-10-14T11:00"}),
    }
