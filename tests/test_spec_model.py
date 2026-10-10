"""The Pydantic spec model: malformed specs are errors, valid-but-unsupported policies are reported (never changed),
normalisation is lossless. A structural pass says nothing about whether values match the brief."""
import copy
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from builder import compile as comp, extract, spec_model  # noqa: E402

O = json.load(open(ROOT / "kit" / "build-the-builder" / "tools" / "openapi.json"))
BASE = comp.apply_defaults(json.load(open(ROOT / "specs" / "brief-b.gold.json")), O)


def mutated(path, val, base=BASE):
    s = copy.deepcopy(base)
    d = s
    for k in path[:-1]:
        d = d[k]
    d[path[-1]] = val
    return s


def test_real_specs_are_structurally_valid():
    for f in list((ROOT / "specs").glob("*.gold.json")) + [ROOT / "submission" / n for n in
              ("brief-a.spec.json", "brief-b.spec.json", "variant-c.spec.json", "variant-d.spec.json",
               "variant-e.spec.json", "rehearsal-f.spec.json")]:
        assert spec_model.check(json.load(open(f))) == [], f


def test_malformed_specs_are_errors():
    for path, val in ((["callback", "end"], "7pm"), (["callback", "end"], "10:00"), (["calendar", "workdays"], ["Monday"]),
                      (["identity", "max_wrong"], "3"), (["identity", "max_wrong"], 0), (["events", "refused", "outcome"], ""),
                      (["calendar", "holidays", 0], "26/10/2026"), (["bands", 1, "min"], 5), (["ladder"], [])):
        assert spec_model.check(mutated(path, val)), (path, val)
        assert extract.validate(mutated(path, val), O), (path, val)


def test_valid_policies_are_not_rejected():
    no_persuasion = mutated(["ladder"], [])
    for b in no_persuasion["bands"]:
        b["attempts"] = 0
    overnight = mutated(["callback", "start"], "20:00")
    overnight["callback"]["end"] = "08:00"
    no_callbacks = mutated(["callback", "days"], [])
    gap = mutated(["bands", 1, "min"], 9)
    for s in (no_persuasion, overnight, no_callbacks, gap):
        assert spec_model.check(s) == [], spec_model.check(s)


def test_unsupported_policies_are_reported_not_changed():
    gap = mutated(["bands", 1, "min"], 9)
    rep = spec_model.unsupported(gap)
    assert any("8-8 fall in no band" in r for r in rep), rep
    assert gap["bands"][1]["min"] == 9                       # the policy itself is untouched
    assert spec_model.unsupported(BASE) == []


def test_normalisation_is_lossless_only():
    s = mutated(["callback", "start"], "9:00")
    s["callback"]["end"] = "17.30"
    spec_model.normalise(s)
    assert (s["callback"]["start"], s["callback"]["end"]) == ("09:00", "17:30")
    for raw in ("7pm", "nine", "9", "25:00", "9:75"):
        s = mutated(["callback", "start"], raw)
        spec_model.normalise(s)
        assert s["callback"]["start"] == raw and spec_model.check(s), raw   # not guessed: left as an error


def test_days_field_checked_against_the_record_the_engine_reads():
    s = copy.deepcopy(BASE)
    s["products"].pop("days_field", None)                     # engine default days_late exists in the kit API
    assert not any("days_field" in e for e in extract.validate(s, O))
    o2 = copy.deepcopy(O)
    item = o2["paths"]["/get_account"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["products"]["items"]["properties"]
    item["days_overdue"] = item.pop("days_late")
    assert any("days_field 'days_late' (the engine default)" in e for e in extract.validate(s, o2))


def test_engine_overnight_window_and_no_persuasion():
    import test_engine_edge as E
    from sim import run_case
    spec = copy.deepcopy(E.A)
    spec["callback"].update(start="20:00", end="08:00", days=["today", "next_business_day"])
    led, tr = run_case(spec, E.CASES["PUB-02"], [dict(k="third_party", t="her husband"), dict(k="gives_time", t="tomorrow at 7 in the morning")])
    assert led["callbacks"] == [dict(date="2026-10-15", time="07:00")], E.agent_lines(tr)
    spec = copy.deepcopy(E.A)
    spec["ladder"] = []
    for b in spec["bands"]:
        b["attempts"] = 0
    led, tr = run_case(spec, E.CASES["PUB-01"], E.ids("PUB-01") + [dict(k="no")])
    assert led["outcome"] == "no_promise" and not any("Could you pay" in x for x in E.agent_lines(tr)), E.agent_lines(tr)


def _engine_case(spec, case_id, script, now=None, mutate_case=None):
    import test_engine_edge as E
    from sim import run_case
    case = copy.deepcopy(E.CASES[case_id])
    if now:
        case["variables"]["now"] = now
    if mutate_case:
        mutate_case(case)
    return run_case(spec, case, script), E


def test_band_gap_without_a_rule_makes_no_offer_and_is_not_the_last_band():
    import test_engine_edge as E
    spec = copy.deepcopy(E.A)
    spec["bands"][1]["min"] = 20                                   # 8-19 days late: no band
    (led, tr), _ = _engine_case(spec, "PUB-06", E.ids("PUB-06") + [dict(k="yes"), dict(k="yes")],
                                mutate_case=lambda c: c["products"][0].update(days_late=12))
    lines = E.agent_lines(tr)
    assert led["promises"] == [] and led["outcome"] == spec["events"]["service_error"]["outcome"], lines
    assert not any("Could you pay" in x or "Can you make" in x for x in lines), lines


def test_band_gap_follows_the_brief_rule_when_there_is_one():
    import test_engine_edge as E
    spec = copy.deepcopy(E.A)
    spec["bands"][1]["min"] = 20
    spec["bands_outside"] = {"band": "early"}
    assert spec_model.check(comp.apply_defaults(copy.deepcopy(spec), O)) == []
    (led, tr), _ = _engine_case(spec, "PUB-06", E.ids("PUB-06") + [dict(k="yes"), dict(k="yes")],
                                mutate_case=lambda c: c["products"][0].update(days_late=12))
    assert len(led["promises"]) == 1, E.agent_lines(tr)
    spec["bands_outside"] = {"event": "refused"}
    (led, tr), _ = _engine_case(spec, "PUB-06", E.ids("PUB-06") + [dict(k="yes")], mutate_case=lambda c: c["products"][0].update(days_late=12))
    assert led["promises"] == [] and (led["outcome"], led["reason"]) == ("no_promise", "refused"), E.agent_lines(tr)


def test_short_ladder_repeats_the_last_step_only_when_the_brief_says_so():
    import test_engine_edge as E
    spec = copy.deepcopy(E.A)
    spec["ladder"] = spec["ladder"][:1]                            # late band: 2 attempts, 1 step
    (led, tr), _ = _engine_case(spec, "PUB-06", E.ids("PUB-06") + [dict(k="no"), dict(k="no"), dict(k="no")])
    assert sum("Could you pay" in x for x in E.agent_lines(tr)) == 1, E.agent_lines(tr)
    spec["rules"]["ladder_repeat_last"] = True
    (led, tr), _ = _engine_case(spec, "PUB-06", E.ids("PUB-06") + [dict(k="no"), dict(k="no"), dict(k="no")])
    assert sum("Could you pay" in x for x in E.agent_lines(tr)) == 2, E.agent_lines(tr)


def test_night_window_hours_after_midnight_belong_to_the_previous_evening():
    import test_engine_edge as E
    spec = copy.deepcopy(E.A)
    spec["callback"].update(start="20:00", end="02:00", days=["today"])
    tp = [dict(k="third_party", t="her husband")]
    (led, tr), _ = _engine_case(spec, "PUB-02", tp + [dict(k="gives_time", t="tonight at 1 am")], now="2026-10-14T21:00")
    assert led["callbacks"] == [dict(date="2026-10-15", time="01:00")], E.agent_lines(tr)
    (led, tr), _ = _engine_case(spec, "PUB-02", tp + [dict(k="gives_time", t="tonight at half past ten")], now="2026-10-14T21:00")
    assert led["callbacks"] == [dict(date="2026-10-14", time="22:30")], E.agent_lines(tr)
    (led, tr), _ = _engine_case(spec, "PUB-02", tp + [dict(k="gives_time", t="the 16th at 1 am"), dict(k="gives_time", t="the 16th at 1 am")],
                                now="2026-10-14T21:00")
    assert led["callbacks"] == [], E.agent_lines(tr)               # belongs to the 15th's window: not an allowed day
    assert spec_model.unsupported(comp.apply_defaults(copy.deepcopy(spec), O)) == []


def test_repair_budget_is_shared_and_bounded():
    import builder.build as B
    from builder import extract
    from builder.trace import Trace
    calls = []
    bad = mutated(["callback", "end"], "7pm")

    def always_bad(spec, problems, docs, openapi, trace):
        calls.append(1)
        return copy.deepcopy(bad)
    orig = extract.repair_behaviour
    extract.repair_behaviour = always_bad
    try:
        trace = Trace(str(ROOT / "traces" / "test-budget.trace.log"))
        budget = B.Budget(3, 60)
        budget.used = 1                                            # one round already spent during extraction
        chk = B.local_checks(copy.deepcopy(bad), O)
        spec, chk2 = B.repair_loop(copy.deepcopy(bad), chk, O, [], trace, budget)
    finally:
        extract.repair_behaviour = orig
    assert len(calls) == 2 and budget.used == 3                    # only what was left of the shared budget
    assert chk2["structural"]                                      # never accepted: still malformed, so no deploy


def _facts(spec, rec, now="2026-11-24T10:00"):
    from builder.runtime import cal, engine
    s = comp.apply_defaults(copy.deepcopy(spec), O)
    engine.S = s
    cal.P = dict(workdays=s["calendar"]["workdays"], holidays=s["calendar"]["holidays"])
    return engine.product_facts(dict(now=now), dict(rec, product_id="p1"))


def test_percentage_floor_is_rounded_once_on_the_exact_value():
    import test_engine_edge as E
    spec = copy.deepcopy(E.A)
    spec["products"].update(floor_field={}, floor_pct=30, floor_round="up_unit")
    rec = dict(type="credit_card", days_late=12)
    assert _facts(spec, dict(rec, past_due=333.34))["floor"] == 101.0      # 100.002 rounds up, not 100.00
    assert _facts(spec, dict(rec, past_due=300.0))["floor"] == 90.0        # 90.00000000000001 is 90


def test_a_floor_above_the_amount_owed_is_the_amount_owed():
    import test_engine_edge as E
    spec = copy.deepcopy(E.A)
    ff = spec["products"]["floor_field"]["credit_card"]
    assert _facts(spec, {"type": "credit_card", "days_late": 12, "past_due": 90.0, ff: 120.0})["floor"] == 90.0


def test_promise_unit_can_differ_from_the_account_unit():
    from builder.runtime import engine
    import test_engine_edge as E
    engine.S = copy.deepcopy(E.A)
    engine.S["products"].update(amount_scale=1, promise_scale=100)
    assert engine.promise_amount({"agreed": [85.5, ""]}) == 8550
    engine.S["products"].pop("promise_scale")
    assert engine.promise_amount({"agreed": [85.5, ""]}) == 85.5


def test_composite_per_type_and_rolling_deadlines():
    import test_engine_edge as E
    spec = copy.deepcopy(E.A)
    spec["calendar"]["holidays"] = []
    b = spec["bands"][0]
    b.update(min=1, max=30)
    spec["bands"] = [b]
    b["deadline"] = {"kind": "later_of", "of": [{"kind": "last_business_day_of_month"}, {"kind": "business_days_after", "n": 3}]}
    rec = dict(type="credit_card", days_late=5, past_due=100.0)
    assert _facts(spec, rec, "2026-11-10T10:00")["deadline"] == "2026-11-30"
    assert _facts(spec, rec, "2026-11-27T10:00")["deadline"] == "2026-12-02"
    b["deadline_by_type"] = {"personal_loan": {"kind": "business_days_after", "n": 2}}
    assert _facts(spec, dict(rec, type="personal_loan"), "2026-11-10T10:00")["deadline"] == "2026-11-12"
    b["deadline"] = {"kind": "last_business_day_of_month", "roll_if_not_after_today": True}
    assert _facts(spec, rec, "2026-11-30T10:00")["deadline"] == "2026-12-31"
    assert _facts(spec, rec, "2026-11-27T10:00")["deadline"] == "2026-11-30"
    assert spec_model.check(comp.apply_defaults(copy.deepcopy(spec), O)) == []
    assert not [e for e in extract.validate(spec, O) if "deadline" in e]


def test_a_weekday_can_have_its_own_callback_window():
    import datetime
    from builder.runtime import cal, engine
    import test_engine_edge as E
    spec = comp.apply_defaults(copy.deepcopy(E.A), O)
    spec["calendar"]["workdays"] = [0, 1, 2, 3, 4, 5]
    spec["callback"].update(start="09:00", end="18:00", days=["within_business_days:2"], windows={"5": {"start": "09:00", "end": "13:00"}})
    engine.S = spec
    cal.P = dict(workdays=spec["calendar"]["workdays"], holidays=[])
    V, sat = dict(now="2026-11-13T10:00"), datetime.date(2026, 11, 14)
    assert engine.callback_ok(V, sat, "12:30") and not engine.callback_ok(V, sat, "15:00")
    assert engine.callback_ok(V, datetime.date(2026, 11, 17), "15:00") is False      # third business day: not allowed
    assert spec_model.check(spec) == []
