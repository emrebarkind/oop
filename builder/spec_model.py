"""Strict typed model of the spec (builder/spec_doc.md), checked at build time with Pydantic.

Three outcomes, kept apart on purpose:
  * errors      - the spec is malformed (wrong type, an impossible value, a time the engine cannot read). They go to
                  the repair round; the build does not deploy a spec that still has them.
  * unsupported - the brief's policy is valid but the engine cannot carry it out as written (e.g. days_late values
                  that fall in no band). Reported in the trace; the repair round never asks Claude to change policy.
  * normalise   - lossless format fixes only ('9:00' -> '09:00'). No guessing: '7pm', 'nine-ish' or an amount are
                  never rewritten here.
A structural pass says nothing about whether the values match the brief; that is what the two independent
readings and the adjudication are for. The engine itself never imports this (the code node only has json/re/datetime).
"""
import datetime
import re
from typing import Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class Loose(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)


class Calendar(Loose):
    workdays: List[int] = Field(min_length=1, max_length=7)
    holidays: List[str] = []

    @field_validator("workdays")
    @classmethod
    def _days(cls, v):
        if any(d < 0 or d > 6 for d in v) or len(set(v)) != len(v):
            raise ValueError("workdays must be distinct Python weekday numbers 0 (Monday) to 6 (Sunday)")
        return v

    @field_validator("holidays")
    @classmethod
    def _iso(cls, v):
        for h in v:
            try:
                datetime.date.fromisoformat(h)
            except ValueError:
                raise ValueError(f"holiday '{h}' is not a YYYY-MM-DD date")
        return v


class Window(Loose):
    start: str = Field(pattern=HHMM)
    end: str = Field(pattern=HHMM)


class Callback(Loose):
    days: List[str] = []                     # empty = the brief allows no callback
    start: str = Field(pattern=HHMM)         # a window that crosses midnight (start > end) is valid
    end: str = Field(pattern=HHMM)
    after_now: bool = True
    business_days_only: bool = False
    max_asks: int = Field(default=2, ge=1)
    windows: Dict[str, Optional[Window]] = {}  # weekday "0".."6" -> its own window, null = no callbacks that day

    @field_validator("windows")
    @classmethod
    def _days(cls, v):
        if any(k not in [str(i) for i in range(7)] for k in v):
            raise ValueError("callback.windows keys must be weekday numbers \"0\" (Monday) to \"6\" (Sunday)")
        return v

    @model_validator(mode="after")
    def _window(self):
        if self.start == self.end:
            raise ValueError(f"callback window start and end are both {self.start}: give the window the brief states")
        return self


class Question(Loose):
    field: str = Field(min_length=1)
    kind: Literal["date", "digits", "year", "upper", "text", "email"]
    length: Optional[int] = Field(default=None, ge=1)
    format: Optional[str] = None
    ask: str = Field(min_length=1)


class Identity(Loose):
    questions: List[Question] = Field(min_length=1)
    # wrong answers that END the check: 'no wrong answer tolerated' is 1 (the first wrong answer ends it)
    max_wrong: Optional[int] = Field(default=None, ge=1)
    max_wrong_per_question: Optional[int] = Field(default=None, ge=1)
    date_order: Literal["dmy", "mdy"] = "dmy"
    hints: Dict[str, str] = {}
    response: Dict[str, Union[str, List[str]]] = {}


class Products(Loose):
    names: Dict[str, str] = Field(min_length=1)
    amount_field: Optional[str] = None       # the engine defaults to past_due / days_late; extract.validate checks
    days_field: Optional[str] = None         # the field actually used against the tool API's account record
    floor_field: Dict[str, str] = {}
    floor_pct: Optional[Union[int, float]] = Field(default=None, ge=0, le=100)
    floor_names: Dict[str, str] = {}
    amount_scale: Union[int, float] = Field(default=1, gt=0)
    promise_scale: Optional[Union[int, float]] = Field(default=None, gt=0)   # create_promise unit, if not amount_scale's


class Deadline(Loose):
    kind: str
    n: Optional[int] = Field(default=None, ge=0)
    value: Optional[str] = None
    of: Optional[List["Deadline"]] = None    # later_of / earlier_of
    roll_if_not_after_today: bool = False

    @model_validator(mode="after")
    def _of(self):
        if self.kind in ("later_of", "earlier_of") and len(self.of or []) < 2:
            raise ValueError(f"deadline kind {self.kind} needs 'of' with at least two rules")
        return self


class Step(Loose):
    amount: str = Field(pattern=r"^(full|floor|full_or_floor|pct:\d+(\.\d+)?)$")
    date: str = "deadline"


class Band(Loose):
    name: str = ""
    min: int = Field(ge=0)
    max: int = Field(ge=0)
    deadline: Deadline
    deadline_by_type: Dict[str, Deadline] = {}   # product types whose deadline differs in this band
    attempts: int = Field(ge=0)              # 0 = no persuasion in this band
    ladder: Optional[List[Step]] = None

    @model_validator(mode="after")
    def _bounds(self):
        if self.min > self.max:
            raise ValueError(f"band {self.name}: min {self.min} is above max {self.max}")
        return self


class Event(Loose):
    outcome: str = Field(min_length=1)       # close_call needs an outcome
    reason: Optional[str] = ""
    queue: Optional[str] = None
    note: Optional[str] = None


class Spec(Loose):
    currency_word: str = Field(min_length=1)
    calendar: Calendar
    callback: Callback
    confirm: Dict[str, int]
    identity: Identity
    products: Products
    bands: List[Band] = Field(min_length=1)
    ladder: List[Step] = []                  # empty is valid when no band allows an attempt
    events: Dict[str, Event]
    tool_bodies: Dict[str, Dict[str, str]]
    lines: Dict[str, str]
    max_not_talk: int = Field(default=2, ge=1)

    @field_validator("confirm")
    @classmethod
    def _confirm(cls, v):
        if v.get("max_asks", 0) < 1:
            raise ValueError("confirm.max_asks must be an integer of at least 1")
        return v

    @model_validator(mode="after")
    def _outside(self):
        rule = getattr(self, "bands_outside", None)
        if rule:
            if not isinstance(rule, dict) or not (rule.get("band") or rule.get("event")):
                raise ValueError("bands_outside must be {\"band\": <band name>} or {\"event\": <event key>}")
            if rule.get("band") and rule["band"] not in [b.name for b in self.bands]:
                raise ValueError(f"bands_outside names band '{rule['band']}', which is not a band")
            if rule.get("event") and (rule["event"] not in self.events or rule["event"].startswith("transfer_")):
                raise ValueError(f"bands_outside event '{rule['event']}' must be a non-transfer event key")
        return self

    @model_validator(mode="after")
    def _bands(self):
        bs = sorted(self.bands, key=lambda b: b.min)
        for a, b in zip(bs, bs[1:]):
            if b.min <= a.max:                # contradictory: one customer would be in two bands
                raise ValueError(f"bands {a.name} ({a.min}-{a.max}) and {b.name} ({b.min}-{b.max}) overlap")
        for b in self.bands:
            if b.attempts > 0 and not (b.ladder or self.ladder):
                raise ValueError(f"band {b.name} allows {b.attempts} persuasion attempt(s) but there are no ladder steps")
        return self


def normalise(spec):
    """Lossless format fixes, in place: H:MM / H.MM -> HH:MM. Anything ambiguous is left for the checks."""
    cb = spec.get("callback") if isinstance(spec, dict) else None
    if isinstance(cb, dict):
        for k in ("start", "end"):
            m = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", str(cb.get(k, "")))
            if m and int(m.group(1)) <= 23 and int(m.group(2)) <= 59:
                cb[k] = "%02d:%s" % (int(m.group(1)), m.group(2))
    return spec


def check(spec):
    """Structural errors (empty when the spec fits the model). Values are not compared with the brief here."""
    try:
        Spec.model_validate(spec)
        return []
    except ValidationError as e:
        out = []
        for err in e.errors():
            where = ".".join(str(x) for x in err["loc"]) or "spec"
            msg = re.sub(r"^Value error, ", "", err["msg"])
            out.append(f"{where}: {msg}" + (f" (got {err['input']!r})" if err.get("type", "").endswith(("_type", "_parsing", "pattern_mismatch")) else ""))
        return out


def unsupported(spec):
    """Valid policies the engine cannot carry out as written, or that look like a possible misreading.
    Reported in the trace for a human, never 'repaired' into another policy."""
    out = []
    try:
        bs = sorted(spec.get("bands") or [], key=lambda b: b.get("min", 0))
        rule = spec.get("bands_outside") or {}
        what = (f"the brief's rule applies (band {rule['band']})" if rule.get("band") else
                f"the brief's rule applies (event {rule['event']})" if rule.get("event") else
                "the brief gives no rule: no offer is made for such a product and it closes as service_error")
        for a, b in zip(bs, bs[1:]):
            if b.get("min", 0) > a.get("max", 0) + 1:
                out.append(f"days_late {a['max'] + 1}-{b['min'] - 1} fall in no band ({a.get('name')} / {b.get('name')}); {what}")
        if bs and bs[0].get("min", 0) > 1:
            out.append(f"days_late below {bs[0]['min']} fall in no band; {what}")
        steps = len(spec.get("ladder") or [])
        repeat = (spec.get("rules") or {}).get("ladder_repeat_last")
        for b in bs:
            if not b.get("ladder") and 0 < steps < b.get("attempts", 0) and not repeat:
                out.append(f"band {b.get('name')} allows {b['attempts']} attempts but the ladder has {steps} step(s) and the "
                           f"brief does not say to repeat the last one: persuasion stops after {steps} attempt(s)")
        if not (spec.get("callback") or {}).get("days", ["x"]):
            out.append("no callback day is allowed: every callback request closes as no_callback")
    except Exception as e:
        out.append(f"could not check engine support: {type(e).__name__}")
    return out
