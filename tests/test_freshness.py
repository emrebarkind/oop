"""An extraction result from an earlier customer turn is never used as a new answer. The step node can run again
without a new customer message (a chained call failing back to the step, a recovery path); the platform also rejects
some code writes (enum variables), so a value can stay in a variable after it was used. Freshness comes from the
platform's own <node>.total_turns counters (observed in every call snapshot); without them, only a value that is
not a reset value counts."""
import copy, json, pathlib
from builder import selftest
from builder.runtime import engine
from builder import compile as comp

ROOT = pathlib.Path(__file__).resolve().parent.parent
OA = json.load(open(ROOT / "kit" / "build-the-builder" / "tools" / "openapi.json"))
CASE = [c for c in json.load(open(ROOT / "kit" / "build-the-builder" / "public-test-set.json")) if c["id"] == "PUB-01"][0]
SA = json.load(open(ROOT / "eval" / "clean-runs" / "cand8-a" / "cand8-a.spec.json"))


def T(k, t, d="", a="", cut="no"):
    return dict(k=k, t=t, d=d, a=a, f="none", side="none", cut=cut)


def at_readback(extra=()):
    """Run to the read-back ('Do you confirm?') and stop there, with the variables kept."""
    spec = comp.apply_defaults(copy.deepcopy(SA), OA)
    wf = comp.compile_workflow(spec, OA)
    prods = [dict(p, product_id=f"p{i + 1}") for i, p in enumerate(CASE["products"])]
    bank = selftest.GenericBank(copy.deepcopy(spec), CASE["identity"], prods)
    V = dict(customer_id="c", **CASE["variables"])

    def feed(stage, u):
        V.update({stage + "_kind": u.get("k", "none"), stage + "_text": u.get("t", ""), stage + "_date": u.get("d", ""),
                  stage + "_amount": u.get("a", ""), stage + "_side": "none", stage + "_for": "none", stage + "_cut": u.get("cut", "no")})
        if stage == "id":
            V["id_norm"] = u.get("norm", "")
    turns = [T("confirmed", "yes"), dict(k="answer", t="14 March 1988", norm="1988-03-14"), dict(k="answer", t="4471", norm="4471"),
             T("proposes", "I can pay the full amount on Friday.", d="Friday")] + list(extra)
    tr = selftest.walk(wf, spec, V, lambda tool, body, slots: bank.call(tool, slots), turns, feed, 400)
    return V, bank, tr


def step_again(V):
    """The step node runs again with no new customer message."""
    engine.main(V)
    return V.get("next"), engine.st(V)


def test_no_new_message_after_a_used_answer_is_not_a_customer_turn():
    V, bank, tr = at_readback()
    assert tr[-1] == ("customer", "<silence>") and "confirm" in json.dumps(engine.st(V).get("stage"))
    s0 = copy.deepcopy(engine.st(V))
    nxt, s1 = step_again(V)
    assert nxt == "talk_confirm" and "create_promise" not in bank.led["calls"]
    assert s1.get("confirm_unclear", 0) == s0.get("confirm_unclear", 0) and s1.get("stall", 0) == s0.get("stall", 0)


def test_an_old_yes_left_in_the_variable_never_authorises_a_write():
    V, bank, tr = at_readback()
    V["confirm_kind"] = "yes"                                       # an earlier yes the platform did not let us clear
    V["confirm_text"] = "Yes, I confirm."
    nxt, s1 = step_again(V)
    assert nxt != "api_promise" and "create_promise" not in bank.led["calls"]


def test_an_old_no_left_in_the_variable_uses_no_attempt():
    V, bank, tr = at_readback()
    V["confirm_kind"] = "no"
    nxt, s1 = step_again(V)
    assert nxt == "talk_confirm" and all(not p.get("refused_offers") for p in s1["products"])


def test_an_old_cut_flag_does_not_make_a_new_fragment():
    V, bank, tr = at_readback()
    V["confirm_cut"] = "yes"
    s0 = copy.deepcopy(engine.st(V))
    nxt, s1 = step_again(V)
    assert s1.get("go_on_total", 0) == s0.get("go_on_total", 0)


def test_a_genuine_new_answer_identical_to_the_previous_one_is_taken_and_written_once():
    V, bank, tr = at_readback([T("unclear", "hmm"), T("yes", "Yes, I confirm.")])
    assert bank.led["promises"] == [("p1", 420.0, "2026-10-16")] and bank.led["calls"].count("create_promise") == 1


def test_without_the_counters_a_reset_value_is_still_not_an_answer():
    selftest.COUNTERS[0] = False
    try:
        V, bank, tr = at_readback()
        V["confirm_text"] = ""
        s0 = copy.deepcopy(engine.st(V))
        nxt, s1 = step_again(V)
        assert "create_promise" not in bank.led["calls"]
    finally:
        selftest.COUNTERS[0] = True


def test_without_the_counters_an_old_valid_yes_does_not_write_and_the_plan_is_kept():
    selftest.COUNTERS[0] = False
    try:
        V, bank, tr = at_readback()
        plan = [p.get("agreed") for p in engine.st(V)["products"]]
        V["confirm_kind"] = "yes"                     # a valid value: it does not prove that the answer is new
        nxt, s1 = step_again(V)
        assert nxt != "api_promise" and "create_promise" not in bank.led["calls"]
        assert [p.get("agreed") for p in s1["products"]] == plan and s1["stage"] == "confirm"
    finally:
        selftest.COUNTERS[0] = True
