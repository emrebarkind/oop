"""Edges that mutation testing (mutmut, eval trial branch) showed no test pinned down: date and amount bounds,
identity pass values and wrong-answer limits, refused offers, callback window edges."""
import copy
import datetime
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from builder.runtime import cal, engine  # noqa: E402

D = datetime.date


@pytest.fixture
def spec():
    import test_engine_edge as E
    s = copy.deepcopy(E.A)
    s["calendar"]["holidays"] = ["2026-11-30"]
    engine.S = s
    cal.P = dict(workdays=[0, 1, 2, 3, 4], holidays=["2026-11-30"])
    return s


V0 = {"now": "2026-11-24T10:00"}               # Tuesday


def test_promise_dates_today_and_the_deadline_itself_are_allowed(spec):
    p = {"deadline": "2026-11-27"}
    assert engine.check_date(V0, p, D(2026, 11, 24)) == ("ok", D(2026, 11, 24))
    assert engine.check_date(V0, p, D(2026, 11, 27)) == ("ok", D(2026, 11, 27))
    assert engine.check_date(V0, p, D(2026, 11, 23)) == ("past", None)
    assert engine.check_date(V0, p, D(2026, 11, 28)) == ("after_deadline", D(2026, 11, 27))
    p = {"deadline": "2026-12-01"}
    assert engine.check_date(V0, p, D(2026, 11, 28)) == ("not_business", D(2026, 12, 1))   # Sat -> Tue (Mon is a holiday)
    p = {"deadline": "2026-11-30"}
    assert engine.check_date(V0, p, D(2026, 11, 29)) == ("after_deadline", D(2026, 11, 30))


def test_amount_bounds_are_inclusive(spec):
    p = {"full": 300.0, "floor": 90.0}
    assert engine.check_amount(p, 300) == ("ok", 300.0)
    assert engine.check_amount(p, 90) == ("ok", 90.0)
    assert engine.check_amount(p, 89.99) == ("below_floor", 90.0)
    assert engine.check_amount(p, 300.01) == ("ok", 300.0)
    assert engine.check_amount(p, 301) == ("ok", 300.0)
    assert engine.check_amount(p, None) == ("none", None)


def test_identity_pass_values_decide_when_the_spec_has_them(spec):
    spec["identity"]["response"] = {"pass_values": ["MATCH"]}
    assert engine.passed("match") and engine.passed(True)
    assert not engine.passed("no_match") and not engine.passed("false")
    spec["identity"]["response"] = {}
    assert engine.passed("verified") and not engine.passed("mismatch")


class Calls:
    def __init__(self, monkeypatch):
        self.events, self.calls, self.talks = [], [], 0
        monkeypatch.setattr(engine, "close", lambda V, s, event, pre="": self.events.append(event))
        monkeypatch.setattr(engine, "call", lambda V, s, api, **k: self.calls.append(api))
        monkeypatch.setattr(engine, "talk", lambda V, stage, s: setattr(self, "talks", self.talks + 1))
        monkeypatch.setattr(engine, "say", lambda V, *a: None)


def verify(spec, s, **data):
    V = {"api_verify_response.status_code": 200}
    V.update({f"api_verify_response.data.{k}": v for k, v in data.items()})
    engine.after_verify(V, s)


def test_wrong_answer_limit_counts_every_wrong_answer(spec, monkeypatch):
    c = Calls(monkeypatch)
    spec["identity"]["max_wrong"] = 2
    s = {"qi": 0}
    verify(spec, s, ok=True, verified=False)
    assert c.events == [] and s["wrong"] == 1 and c.talks == 1           # asked again
    verify(spec, s, ok=True, verified=False)
    assert c.events == ["id_failed"]


def test_the_bank_saying_no_answers_left_ends_the_check(spec, monkeypatch):
    c = Calls(monkeypatch)
    spec["identity"]["max_wrong"] = 5
    verify(spec, {"qi": 0}, ok=True, verified=False, wrong_answers_left=0)
    assert c.events == ["id_failed"]
    c.events.clear()
    verify(spec, {"qi": 0}, ok=True, verified="locked")
    assert c.events == ["id_failed"]


def test_per_question_limit(spec, monkeypatch):
    c = Calls(monkeypatch)
    spec["identity"].pop("max_wrong", None)
    spec["identity"]["max_wrong_per_question"] = 2
    s = {"qi": 0}
    verify(spec, s, ok=True, verified=False)
    assert c.events == []
    verify(spec, s, ok=True, verified=False)
    assert c.events == ["id_failed"]


def test_an_unreadable_answer_is_not_a_wrong_answer(spec, monkeypatch):
    c = Calls(monkeypatch)
    spec["identity"]["max_wrong"] = 1
    s = {"qi": 0}
    verify(spec, s, ok=False, verified=False, error="unreadable date")
    assert c.events == [] and s.get("wrong", 0) == 0 and s["verify_rejects"] == 1
    verify(spec, s, ok=False, verified=False)
    verify(spec, s, ok=False, verified=False)
    assert c.events == ["id_failed"]                                      # three rejects in a row


def test_a_hint_is_spoken_without_counting_a_wrong_answer(spec, monkeypatch):
    c = Calls(monkeypatch)
    spec["identity"]["hints"] = {"day_month_swapped": "Day first, please."}
    spec["identity"]["max_wrong"] = 1
    s = {"qi": 0}
    verify(spec, s, ok=True, verified=False, hint="day_month_swapped")
    assert c.events == [] and s.get("wrong", 0) == 0 and c.talks == 1


def test_a_passed_question_moves_on_and_the_last_one_opens_the_account(spec, monkeypatch):
    c = Calls(monkeypatch)
    s = {"qi": 0}
    verify(spec, s, ok=True, verified=True, all_verified=False)
    assert s["qi"] == 1 and c.calls == [] and c.talks == 1
    verify(spec, s, ok=True, verified=True)
    assert c.calls == ["api_account"]


def test_a_verify_http_failure_is_retried_once_then_closes(spec, monkeypatch):
    c = Calls(monkeypatch)
    s = {"qi": 0}
    for _ in range(2):
        engine.after_verify({"api_verify_response.status_code": 500}, s)
    assert c.calls == ["api_verify"] and c.events == ["service_error"]


def test_a_refused_offer_is_remembered(spec, monkeypatch):
    monkeypatch.setattr(engine, "advance", lambda V, s, pre="": None)
    p = {"offer": [300.0, "2026-11-27"], "refused_offers": [], "used": 2, "attempts": 2, "status": "open"}
    engine.refusal(V0, {"products": [p]}, p)
    assert p["refused_offers"] == [[300.0, "2026-11-27"]] and p["status"] == "refused" and p["offer"] is None


def test_callback_window_edges_are_inclusive(spec):
    spec["callback"].update(start="09:30", end="18:00", days=["today", "next_business_day"], after_now=True, business_days_only=False)
    spec["callback"].pop("windows", None)
    V = {"now": "2026-11-24T10:00"}
    assert engine.callback_ok(V, D(2026, 11, 24), "18:00")
    assert not engine.callback_ok(V, D(2026, 11, 24), "18:01")
    assert engine.callback_ok(V, D(2026, 11, 25), "09:30")
    assert not engine.callback_ok(V, D(2026, 11, 25), "09:29")
    assert not engine.callback_ok(V, D(2026, 11, 24), "10:00")            # not later than now
    assert engine.callback_ok(V, D(2026, 11, 24), "10:01")
    assert not engine.callback_ok(V, D(2026, 11, 26), "12:00")            # not an allowed day
    spec["callback"]["days"] = ["tomorrow"]
    spec["callback"]["business_days_only"] = True
    assert not engine.callback_ok({"now": "2026-11-27T10:00"}, D(2026, 11, 28), "12:00")   # Saturday
    assert engine.callback_ok({"now": "2026-11-26T10:00"}, D(2026, 11, 27), "12:00")
