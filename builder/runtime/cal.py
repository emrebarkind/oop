"""Calendar arithmetic that runs INSIDE the phone agent (Freya code nodes / hooks).

Constraints of the Freya sandbox: only `json`, `re`, `datetime` are known to import; the source must not
contain the two-character brace sequences used by Freya templating. Every brief-specific value comes from P,
a dict the compiler injects (hex-encoded JSON). Keep this file self-contained and free of other imports.
"""
import datetime
import json
import re

# Injected by the compiler. Defaults keep the module importable for local tests.
P = dict(workdays=[0, 1, 2, 3, 4], holidays=[])

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]


def parse_now(now):
    s = str(now or "").strip().replace(" ", "T")
    try:
        return datetime.datetime.fromisoformat(s[:16])
    except Exception:
        return datetime.datetime.fromisoformat(s[:10] + "T00:00")


def iso(d):
    return d.isoformat()


def is_business_day(d):
    return d.weekday() in P["workdays"] and iso(d) not in P["holidays"]


def next_business_day(d, include_self=True):
    if not include_self:
        d = d + datetime.timedelta(days=1)
    while not is_business_day(d):
        d = d + datetime.timedelta(days=1)
    return d


def add_business_days(d, n):
    """The n-th business day after d (d itself not counted). n=0 returns d."""
    k = 0
    while k < n:
        d = d + datetime.timedelta(days=1)
        if is_business_day(d):
            k += 1
    return d


def add_calendar_days(d, n):
    return d + datetime.timedelta(days=n)


def end_of_month(d):
    nxt = (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    return nxt - datetime.timedelta(days=1)


def week_bounds(d, offset_weeks=0):
    monday = d - datetime.timedelta(days=d.weekday()) + datetime.timedelta(weeks=offset_weeks)
    return monday, monday + datetime.timedelta(days=6)


def last_business_day_of_week(d, offset_weeks=0):
    monday, sunday = week_bounds(d, offset_weeks)
    x = sunday
    while x >= monday:
        if is_business_day(x):
            return x
        x = x - datetime.timedelta(days=1)
    return None


def weekday_in_week(today, weekday, which):
    """which='this' -> that weekday in the current Mon-Sun week; 'next' -> in the following week."""
    monday, _ = week_bounds(today, 1 if which == "next" else 0)
    return monday + datetime.timedelta(days=weekday)


def upcoming_weekday(today, weekday, include_today=False):
    delta = (weekday - today.weekday()) % 7
    if delta == 0 and not include_today:
        delta = 7
    return today + datetime.timedelta(days=delta)


def deadline(today, rule):
    """rule: dict(kind=..., n=...). kinds: business_days_after, calendar_days_after, end_of_month,
    end_of_next_month, last_business_day_of_month, fixed_date (value=YYYY-MM-DD), later_of / earlier_of (of=[rules])."""
    kind, n = rule.get("kind"), int(rule.get("n") or 0)
    if kind == "business_days_after":
        return add_business_days(today, n)
    if kind == "calendar_days_after":
        return add_calendar_days(today, n)
    if kind == "end_of_month":
        return end_of_month(today)
    if kind == "end_of_next_month":
        return end_of_month(end_of_month(today) + datetime.timedelta(days=1))
    if kind == "last_business_day_of_month":
        x = end_of_month(today)
        while not is_business_day(x):
            x = x - datetime.timedelta(days=1)
        return x
    if kind == "fixed_date":
        return datetime.date.fromisoformat(rule["value"])
    if kind == "end_of_week":           # last business day of the current week (next week's if today is it)
        d = last_business_day_of_week(today, 0)
        return d if d is not None and d >= today else last_business_day_of_week(today, 1)
    if kind == "end_of_next_week":
        return last_business_day_of_week(today, 1)
    if kind == "day_of_next_month":     # the n-th calendar day of next month
        first = end_of_month(today) + datetime.timedelta(days=1)
        return first.replace(day=min(max(n, 1), 28))
    if kind == "business_days_after_or_end_of_month":   # whichever comes first
        return min(add_business_days(today, n), end_of_month(today))
    if kind in ("later_of", "earlier_of"):              # of: [rule, rule, ...]
        ds = [deadline(today, r) for r in rule.get("of") or []]
        return max(ds) if kind == "later_of" else min(ds)
    raise ValueError("unknown deadline kind " + str(kind))


# ---------------------------------------------------------------- spoken-language parsing
NUM_WORDS = {
    "zero": 0, "oh": 0, "o": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
ORD_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
    "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
    "twentieth": 20, "thirtieth": 30,
}


def words_to_number(text):
    """'four hundred and fifty-five point five' -> 455.5 ; 'six three three oh' -> 6330 (digit run).
    Returns None when nothing numeric is found."""
    t = str(text or "").lower().replace("-", " ").replace(",", "")
    m = re.search(r"\d+(?:\.\d+)?", t)
    if m and not re.search(r"[a-z]", t.replace("point", "").replace("and", "").replace("dollars", "").replace("euros", "").replace("pounds", "")):
        return float(m.group())
    toks = [w for w in re.findall(r"[a-z]+|\d+(?:\.\d+)?", t) if w not in ("and", "a", "dollars", "dollar", "euros", "euro", "pounds", "pound", "cents", "lira")]
    if not toks:
        return None
    # pure digit run spoken one by one: "six six three zero"
    if all(w in NUM_WORDS and NUM_WORDS[w] < 10 or re.fullmatch(r"\d", w) for w in toks) and len(toks) >= 2:
        return float("".join(str(NUM_WORDS.get(w, w)) for w in toks))
    total, cur, frac, in_frac, seen = 0.0, 0.0, "", False, False
    for w in toks:
        if w == "point":
            in_frac = True
            continue
        if in_frac:
            if w in NUM_WORDS and NUM_WORDS[w] < 10:
                frac += str(NUM_WORDS[w]); continue
            if re.fullmatch(r"\d+", w):
                frac += w; continue
            v = NUM_WORDS.get(w)
            if v is not None:
                frac += str(v); continue
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?", w):
            cur += float(w); seen = True
        elif w in NUM_WORDS:
            cur += NUM_WORDS[w]; seen = True
        elif w == "hundred":
            cur = (cur or 1) * 100; seen = True
        elif w == "thousand":
            total += (cur or 1) * 1000; cur = 0; seen = True
    if not seen:
        return None
    val = total + cur
    if frac:
        val += float("0." + frac)
    return val


HOUR_WORDS = dict(one=1, two=2, three=3, four=4, five=5, six=6, seven=7, eight=8, nine=9, ten=10, eleven=11, twelve=12)
MIN_WORDS = dict(oclock=0, fifteen=15, thirty=30, fortyfive=45, five=5, ten=10, twenty=20, twentyfive=25, fifty=50)


def spoken_time(text):
    """Last time of day mentioned -> 'HH:MM' or None. Handles 11:30, 3pm, 3 o'clock, eleven in the morning,
    half past two, quarter to five, noon. Hours 1-7 without am/pm are read as afternoon (business hours)."""
    t = str(text or "").lower().replace("-", " ").replace("o'clock", "oclock").replace("o clock", "oclock")
    t = re.sub(r"forty five", "fortyfive", t)
    t = re.sub(r"twenty five", "twentyfive", t)
    cands = []
    W = "|".join(HOUR_WORDS)
    for m in re.finditer(r"\b(noon|midday)\b", t):
        cands.append((m.start(), 12, 0, "pm"))
    for m in re.finditer(r"\b(\d{1,2})[:.](\d{2})\s*(am|pm|a\.m\.|p\.m\.)?", t):
        cands.append((m.start(), int(m.group(1)), int(m.group(2)), m.group(3) or ""))
    for m in re.finditer(r"(?<![:.\d])\b(\d{1,2})\s*(am|pm|a\.m\.|p\.m\.|oclock)\b", t):
        cands.append((m.start(), int(m.group(1)), 0, "" if m.group(2) == "oclock" else m.group(2)))
    for m in re.finditer(r"\b(?:at|around|about|after|by|before|from|until|till)\s+(\d{1,2})\b(?!\s*(?:st|nd|rd|th|dollars?|days?|weeks?|[:.]\d))", t):
        cands.append((m.start(1), int(m.group(1)), 0, ""))
    for m in re.finditer(r"\b(half|quarter)\s+(past|to)\s+(" + W + r"|\d{1,2})\b", t):
        h = HOUR_WORDS.get(m.group(3)) or int(m.group(3))
        mi = 30 if m.group(1) == "half" else 15
        if m.group(2) == "to":
            h, mi = h - 1, 60 - mi
        cands.append((m.start(), h, mi, ""))
    for m in re.finditer(r"\b(" + W + r")(?:\s+(oclock|fifteen|thirty|fortyfive|twentyfive|twenty|fifty|oh five|five))?\b", t):
        before = t[max(0, m.start() - 12):m.start()]
        after = t[m.end():m.end() + 12]
        if re.search(r"(past|to)\s*$", before) or re.search(r"^\s*(of|th|st|nd|rd|days?|weeks?|months?|years?|dollars?|hundred|thousand)\b", after):
            continue
        timeish = m.group(2) or re.search(r"(at|around|about|by|after|before|from|until|till)\s*$", before) or \
            re.search(r"^\s*(in the|this|tonight|am|pm|a\.m|p\.m)", after)
        if not timeish:
            continue
        mi = MIN_WORDS.get((m.group(2) or "oclock").replace(" ", ""), 5 if m.group(2) == "oh five" else 0)
        cands.append((m.start(), HOUR_WORDS[m.group(1)], mi, ""))
    if not cands:
        return None
    pos, h, mi, ap = sorted(cands)[-1]
    tail = t[pos:pos + 40]
    if ap.startswith("p") and h < 12:
        h += 12
    elif ap.startswith("a") and h == 12:
        h = 0
    elif not ap:
        if (re.search(r"afternoon|evening|tonight", tail) or re.search(r"\b(tonight|this evening)\b", t)) and h < 12:
            h += 12
        elif not re.search(r"morning", tail) and 1 <= h <= 7:
            h += 12
    if h > 23 or mi > 59:
        return None
    return "%02d:%02d" % (h, mi)


CORRECTION = re.compile(r"\b(sorry|i mean|i meant|actually|make that|no wait|wait no|rather|correction|let me correct)\b[,.]?", re.I)


def after_correction(text):
    """'the 21st, sorry, I mean the 20th' -> 'the 20th' (the part after the last correction marker)."""
    t = str(text or "")
    parts = CORRECTION.split(t)
    if len(parts) > 1:
        tail = parts[-1].strip(" ,.")
        if re.search(r"\d|[a-z]", tail, re.I):
            return tail
    return t


def parse_spoken_date(text, today, rules=None):
    corrected = after_correction(text)
    if corrected != str(text or ""):
        d = _parse_spoken_date(corrected, today, rules)
        if d is not None:
            return d
    return _parse_spoken_date(text, today, rules)


SMALL_N = dict(a=1, an=1, one=1, two=2, three=3, four=4, five=5, six=6, seven=7, eight=8, nine=9, ten=10, couple=2)
MONTH_RE = r"\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b"


WEEKDAY_RE = r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
NEG_RE = r"\b(not|can't|cannot|can not|won't|wont|couldn't)\s+"


def _month_next_to(t, s, e):
    """A month word right before ('october 5') or right after ('5 october', '5th of may') t[s:e]."""
    return bool(re.search(MONTH_RE + r"\s+$", t[:s]) or re.match(r"\s+(?:of\s+)?" + MONTH_RE, t[e:]))


def _parse_spoken_date(text, today, rules=None):
    """Best-effort spoken payment date -> datetime.date or None (relative to `today`, never the real clock).
    Handles ISO, today/tomorrow/day after tomorrow, 'in 3 days', 'a week from today', '(this|next) <weekday>',
    '<weekday> next week', 'the <weekday> after next', bare weekday (next occurrence), '<day> <month>',
    '<month> <day>', 'the 23rd', ordinal words. Negated mentions ('not today', 'not Friday') are ignored; the
    last weekday mentioned wins; today/tomorrow beat a bare stray number. Never raises."""
    try:
        return _parse_spoken_date_x(text, today, rules)
    except Exception:
        return None


def _parse_spoken_date_x(text, today, rules=None):
    rules = rules if isinstance(rules, dict) else dict()
    t = " " + str(text or "").lower().replace("-", " ").replace(",", " ") + " "
    m = re.search(r"(\d{4}) (\d{2}) (\d{2})", t) or re.search(r"(\d{4})/(\d{2})/(\d{2})", t)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    t = re.sub(NEG_RE + r"(?:pay\s+|do\s+it\s+|make\s+it\s+)?(today|tomorrow|now|right now|this week)\b", " ", t)
    t = re.sub(NEG_RE + r"(?:pay\s+|do\s+it\s+|make\s+it\s+)?(?:on\s+)?(?:this\s+(?:coming\s+)?|next\s+)?" + WEEKDAY_RE + r"s?\b", " ", t)
    # day.month written with a dot or a slash ('20.10', '20/10'): day first
    m = re.search(r"\b([0-3]?\d)[./](1[0-2]|0?[1-9])\b(?![./]\d)", t)
    if m and 1 <= int(m.group(1)) <= 31 and not re.search(r"\b(at|around|about|by|until|till)\s*$", t[:m.start()]) \
            and not re.match(r"\s*(am|pm|a\.m|p\.m|o'clock)", t[m.end():]):
        try:
            d = datetime.date(today.year, int(m.group(2)), int(m.group(1)))
            return d if d >= today else datetime.date(today.year + 1, d.month, d.day)
        except ValueError:
            pass
    # 'a week from Friday', 'Friday week', 'a week on Friday', 'the Friday after next': that weekday one week later
    m = re.search(r"\ba week (?:from|on)\s+(?:this\s+)?" + WEEKDAY_RE + r"\b", t) or \
        re.search(r"\b" + WEEKDAY_RE + r"\s+week\b", t)
    if m:
        wd = WEEKDAYS.index(m.group(1))
        return today + datetime.timedelta(days=(wd - today.weekday()) % 7 + 7)
    # relative offsets
    nums = r"(a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|twenty|thirty|couple of|\d+)"
    m = re.search(r"\b(?:in|within)\s+" + nums + r"\s+(day|days|week|weeks)\b", t) or \
        re.search(r"\b" + nums + r"\s+(day|days|week|weeks)\s+from\s+(?:today|now)\b", t)
    if m:
        n = m.group(1).replace(" of", "")
        n = int(n) if n.isdigit() else SMALL_N.get(n, NUM_WORDS.get(n, 1))
        return today + datetime.timedelta(days=n * (7 if m.group(2).startswith("week") else 1))
    month = None
    for mm in re.finditer(MONTH_RE, t):
        w = mm.group(1)
        if w == "may":   # 'may' is a month only next to a day number / ordinal
            ctx = t[max(0, mm.start() - 15):mm.end() + 15]
            if not re.search(r"\d|\b(first|second|third|[a-z]+th|twenty|thirty)\b", ctx):
                continue
        month = [i for i, mn in enumerate(MONTHS) if mn.startswith(w[:3])][0] + 1
    # day number; `strong` = ordinal suffix, 'the' before it, or a month next to it
    day, day_pos, strong = None, -1, False
    for mm in re.finditer(r"\b(\d{1,2})(st|nd|rd|th)?\b(?!\s*(?::|am|pm|a\.m|p\.m|o'clock|oclock|dollars?|euros?|pounds?|days?|weeks?|hundred))", t):
        before = t[max(0, mm.start() - 8):mm.start()]
        if re.search(r"\b(at|around|about|after|before|by)\s+$", before) and not month:
            continue
        if 1 <= int(mm.group(1)) <= 31:
            day, day_pos = int(mm.group(1)), mm.start()
            strong = bool(mm.group(2)) or bool(re.search(r"\bthe\s+$", t[:mm.start()])) or \
                _month_next_to(t, mm.start(), mm.end())
    if day is None:
        for w, v in sorted(ORD_WORDS.items(), key=lambda kv: -len(kv[0])):
            mm = re.search(r"\b(twenty|thirty)\s+" + w + r"\b", t)
            if mm:
                day, day_pos, strong = NUM_WORDS[mm.group(1)] + v, mm.start(), True
                break
            mm = re.search(r"\b" + w + r"\b", t)
            if mm and not re.search(r"\b" + w + r"\s+(week|time|attempt|thing|things|of all|payment|instal?ment|place|choice|option)", t) \
                    and not (w == "second" and re.search(r"\b(a|one|just a)\s+second\b", t)):
                day, day_pos = v, mm.start()
                strong = w not in ("first", "second", "third") or bool(re.search(r"\bthe\s+$", t[:mm.start()])) or \
                    _month_next_to(t, mm.start(), mm.end())
                break
    if day is None and month:
        toks = [x for x in t.split() if x]
        d0, _ = spoken_day([x for x in toks if not re.fullmatch(MONTH_RE.strip("\\b"), x)])
        if d0:
            day, strong = d0, True
            mm = re.search(MONTH_RE, t)
            day_pos = mm.start() if mm else 0
    # explicit relative words beat a bare stray number ('tomorrow, I get paid on 5'), not a real date
    # ('tomorrow, sorry, the 20th'); a correction marker between them makes the later one win
    rel, rel_pos = None, -1
    for pat, n in ((r"\bday after tomorrow\b", 2), (r"\btomorrow\b", 1), (r"\b(today|tonight|this afternoon|this evening)\b", 0)):
        mm = re.search(pat, t)
        if mm:
            rel, rel_pos = n, mm.start()
            break
    if rel is not None and day is not None:
        lo, hi = sorted((rel_pos, day_pos))
        marker = CORRECTION.search(t[lo:hi]) is not None
        keep = (strong or marker) if day_pos > rel_pos else (strong and not marker)
        if not keep:
            day = None
    if rel == 2 and day is None:
        return today + datetime.timedelta(days=2)
    nxt = rules.get("next_weekday", "following_week")
    hits = [(mm.start(), mm.end(), WEEKDAYS.index(mm.group(1))) for mm in re.finditer(r"\b" + WEEKDAY_RE + r"s?\b", t)]
    if hits:
        s0, e0, i = max(hits)          # the last weekday mentioned wins ('Friday, no, Thursday')
        before, after = t[:s0], t[e0:]
        if re.match(r"\s+after\s+next\b", after):
            return weekday_in_week(today, i, "this") + datetime.timedelta(weeks=2)
        if re.match(r"\s+(?:next\s+)?week\b", after) or re.search(r"\ba\s+week\s+on\s+$", before) or \
                re.search(r"\bnext\s+week\s+(?:on\s+|maybe\s+|probably\s+|say\s+)*$", before):
            return weekday_in_week(today, i, "next")
        if re.search(r"\bnext\s+$", before):
            return weekday_in_week(today, i, "next") if nxt == "following_week" else upcoming_weekday(today, i)
        if re.search(r"\bthis\s+(coming\s+)?$", before):
            d = weekday_in_week(today, i, "this")
            return d if d >= today else upcoming_weekday(today, i)
        if day is None:
            return upcoming_weekday(today, i, include_today=False)
        # "Monday the 26th" -> use the day number below
    if day is not None:
        y = today.year
        mth = month or today.month
        try:
            d = datetime.date(y, mth, day)
        except ValueError:
            return None
        if month is None and d < today:
            mth = mth % 12 + 1
            y = y + (1 if mth == 1 else 0)
            try:
                d = datetime.date(y, mth, day)
            except ValueError:
                return None
        elif d < today - datetime.timedelta(days=180):
            d = datetime.date(y + 1, mth, day)
        return d
    if rel == 1:
        return today + datetime.timedelta(days=1)
    if rel == 0 or re.search(r"\b(now|right away|immediately)\b", t):
        return today
    return None


def _tokens(text):
    t = str(text or "").lower()
    t = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", t)
    t = re.sub(r"[-,./]", " ", t)
    return [w for w in t.split() if w not in ("the", "of", "and", "a", "um", "uh", "er", "its", "it's", "is", "my")]


UNITS = dict(zero=0, oh=0, o=0, one=1, two=2, three=3, four=4, five=5, six=6, seven=7, eight=8, nine=9)
TEENS = dict(ten=10, eleven=11, twelve=12, thirteen=13, fourteen=14, fifteen=15, sixteen=16, seventeen=17,
             eighteen=18, nineteen=19)
TENS = dict(twenty=20, thirty=30, forty=40, fifty=50, sixty=60, seventy=70, eighty=80, ninety=90)


def digit_groups(tokens):
    """Spoken digit string -> digits, keeping leading zeros: 'oh three five eight' -> '0358',
    'forty four seventy one' -> '4471', 'double seven two one' -> '7721'."""
    out, i = "", 0
    while i < len(tokens):
        w = tokens[i]
        if re.fullmatch(r"\d+", w):
            out += w
        elif w in ("double", "triple") and i + 1 < len(tokens) and tokens[i + 1] in UNITS:
            out += str(UNITS[tokens[i + 1]]) * (2 if w == "double" else 3)
            i += 1
        elif w in UNITS:
            out += str(UNITS[w])
        elif w in TEENS:
            out += str(TEENS[w])
        elif w in TENS:
            if i + 1 < len(tokens) and tokens[i + 1] in UNITS and UNITS[tokens[i + 1]] > 0 and tokens[i + 1] not in ("oh", "o"):
                out += str(TENS[w] + UNITS[tokens[i + 1]])
                i += 1
            else:
                out += str(TENS[w])
        elif w == "hundred" and out:
            out += "00"
        i += 1
    return out


def cardinal(tokens):
    """'four thousand four hundred seventy one' -> 4471 ; None if no number words."""
    total, cur, seen = 0, 0, False
    for w in tokens:
        if re.fullmatch(r"\d+", w):
            cur += int(w); seen = True
        elif w in UNITS:
            cur += UNITS[w]; seen = True
        elif w in TEENS:
            cur += TEENS[w]; seen = True
        elif w in TENS:
            cur += TENS[w]; seen = True
        elif w == "hundred":
            cur = (cur or 1) * 100; seen = True
        elif w == "thousand":
            total += (cur or 1) * 1000; cur = 0; seen = True
    return total + cur if seen else None


def digits_only(text):
    toks = _tokens(text)
    if any(w in ("hundred", "thousand") for w in toks):
        n = cardinal(toks)
        return str(n) if n is not None else ""
    return digit_groups(toks)


NATO = dict(alpha="A", alfa="A", bravo="B", charlie="C", delta="D", echo="E", foxtrot="F", golf="G", hotel="H",
            india="I", juliet="J", juliett="J", kilo="K", lima="L", mike="M", november="N", oscar="O", papa="P",
            quebec="Q", romeo="R", sierra="S", tango="T", uniform="U", victor="V", whiskey="W", whisky="W",
            xray="X", yankee="Y", zulu="Z")
CODE_FILLER = ("it's", "its", "it", "my", "postcode", "post", "code", "zip", "is", "the", "number", "and", "uh", "um",
               "er", "erm", "hmm", "reference", "ref", "that's", "thats", "space", "dash", "hyphen", "slash", "then",
               "yes", "yeah", "ok", "okay", "so", "please")
CODE_NOUNS = ("postcode", "post", "code", "zip", "number", "reference", "ref")


def _code_char(tok):
    """One spoken character of a code -> 'A'..'Z' / '0'..'9', or None."""
    w = tok.lower()
    if w in UNITS:
        return str(UNITS[w])
    if w in NATO:
        return NATO[w]
    if re.fullmatch(r"[a-z0-9]", w):
        return w.upper()
    return None


def spoken_code(text):
    """Alphanumeric code said aloud (postcode, reference) -> uppercase letters and digits only, '' if none.
    "it's SW1A 1AA", 'S W one A one A A', 'sierra whiskey one alpha, one alpha alpha' -> 'SW1A1AA';
    'double one' -> '11'; 'S for Sugar' -> 'S'. Unknown lowercase words are dropped."""
    try:
        return _spoken_code(text)
    except Exception:
        return ""


def _spoken_code(text):
    raw = re.sub(r"(?i)\bx[\s-]?ray\b", "xray", str(text or ""))
    toks = re.findall(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)?", raw)
    low = [w.lower() for w in toks]

    def known(j):
        w = low[j]
        return w in UNITS or w in TEENS or w in TENS or w in NATO or w in ("double", "triple", "hundred") or \
            bool(re.fullmatch(r"[a-z0-9]", w)) or bool(re.search(r"\d", w)) or (toks[j].isupper() and len(w) <= 4)

    def word_like(j):      # a word of the sentence around the code ('I think', 'a postcode')
        return j < len(toks) and (low[j] in CODE_NOUNS or ("'" in low[j] or "’" in low[j]) or
                                  (low[j] not in CODE_FILLER and not known(j)))

    out, i = [], 0
    while i < len(toks):
        tok, w = toks[i], low[i]
        nx = low[i + 1] if i + 1 < len(toks) else ""
        if re.fullmatch(r"[a-z]", w) and i + 2 < len(toks) and (nx == "for" or (nx == "as" and low[i + 2] == "in")):
            k = i + 2 if nx == "for" else i + 3                 # 'S for Sugar', 'B as in Bravo'
            if k < len(toks) and low[k][:1] == w and len(low[k]) > 1:
                out.append(w.upper())
                i = k + 1
                continue
        if "'" in w or "’" in w or w in CODE_FILLER:
            i += 1
            continue
        if w in ("double", "triple"):
            c = _code_char(toks[i + 1]) if i + 1 < len(toks) else None
            if c:
                out.append(c * (2 if w == "double" else 3))
                i += 2
            else:
                i += 1
            continue
        if w in ("a", "i") and word_like(i + 1):            # article / pronoun, not a letter
            i += 1
            continue
        if w in TENS:
            if nx in UNITS and UNITS[nx] > 0:
                out.append(str(TENS[w] + UNITS[nx]))
                i += 2
                continue
            out.append(str(TENS[w]))
        elif w in TEENS:
            out.append(str(TEENS[w]))
        elif w == "hundred":
            if out:
                out.append("00")
        elif _code_char(tok):
            out.append(_code_char(tok))
        elif re.fullmatch(r"[a-z0-9]+", w) and (re.search(r"\d", w) or (tok.isupper() and len(w) <= 4)):
            out.append(tok.upper())                         # 'SW1A', '1AA', 'EC' as written by the transcriber
        i += 1
    return "".join(out)


def _btokens(text):
    """Tokens for birth dates / years: like _tokens but ordinal suffixes stay ('3rd'), so a day said as an
    ordinal is never read as part of a year."""
    t = str(text or "").lower()
    t = re.sub(r"[^a-z0-9' ]", " ", t)
    return [w for w in t.split() if w not in ("the", "of", "and", "a", "um", "uh", "er", "erm", "its", "it's", "is",
                                              "my", "on", "in", "born", "was")]


def _is_num_tok(w):
    return w in UNITS or w in TEENS or w in TENS or w in ("hundred", "thousand") or bool(re.fullmatch(r"\d{1,4}", w))


def _is_ord_tok(w):
    return w in ORD_WORDS or bool(re.fullmatch(r"\d{1,2}(st|nd|rd|th)", w))


def _num_toks(tokens):
    if isinstance(tokens, str):
        tokens = _btokens(tokens)
    return [str(w).lower() for w in (tokens or []) if _is_num_tok(str(w).lower())]


def year_is_prefix(text):
    """True when the only year words are a century prefix still waiting for the rest: 'nineteen', 'twenty',
    'two thousand', 'one nine', 'two zero', 'nineteen oh' (the caller treats it as a partial answer)."""
    try:
        nt = _num_toks(text)
        if not nt:
            return False
        if "thousand" in nt or "hundred" in nt:
            return nt[-1] in ("thousand", "hundred") and cardinal(nt) in (1900, 2000)
        return digit_groups(nt) in ("19", "20", "190", "200")
    except Exception:
        return False


def _year_exact(nt):
    """(year, kind) when ALL the number tokens `nt` form one year phrase; kind 'full' (four-digit style:
    1984, nineteen eighty-four, one nine eight four, two thousand and one) or 'short' (eighty-six -> 1986).
    (None, '') otherwise, and for a bare century prefix."""
    if not nt or year_is_prefix(nt):
        return None, ""
    if len(nt) == 1 and re.fullmatch(r"(19|20)\d\d", nt[0]):
        return int(nt[0]), "full"
    if "thousand" in nt or "hundred" in nt:
        n = cardinal(nt)
        return (n, "full") if n and 1900 <= n <= 2099 else (None, "")
    if nt[0] in ("nineteen", "twenty", "19", "20") and len(nt) >= 2:
        if not (nt[0] == "twenty" and len(nt) == 2 and nt[1] in UNITS and UNITS[nt[1]] > 0):  # 'twenty-one' = '21
            rest = digit_groups(nt[1:])
            if rest.isdigit() and 1 <= len(rest) <= 2 and rest != "0":
                return (1900 if nt[0] in ("nineteen", "19") else 2000) + int(rest), "full"
    g = digit_groups(nt)
    if len(g) == 4 and g[:2] in ("19", "20"):
        return int(g), "full"
    if len(g) == 2:
        y = int(g)
        return (1900 + y if y > 30 else 2000 + y), "short"
    return None, ""


def spoken_year(tokens, round_ok=False):
    """Year from the tokens left after month/day are removed (a list, or a text). None when there is no year
    or only a century prefix ('nineteen', 'two thousand' ...: see year_is_prefix)."""
    try:
        nt = _num_toks(tokens)
        for w in nt:
            if re.fullmatch(r"(19|20)\d\d", w):
                return int(w)
        y, _ = _year_exact(nt)
        if y is None and len(nt) > 2 and nt[0] in ("oh", "o"):     # "oh, nineteen eighty-eight"
            y, _ = _year_exact(nt[1:])
        if y is None and round_ok and nt[-2:] in (["two", "thousand"], ["nineteen", "hundred"], ["twenty", "hundred"]):
            y = dict(two=2000, nineteen=1900, twenty=2000)[nt[-2]]  # the year itself, said as a round number
        return y
    except Exception:
        return None


def spoken_day(tokens):
    """(day, tokens_without_day)"""
    for i, w in enumerate(tokens):
        if w in ("twenty", "thirty") and i + 1 < len(tokens) and tokens[i + 1] in ORD_WORDS and ORD_WORDS[tokens[i + 1]] < 10:
            return TENS[w] + ORD_WORDS[tokens[i + 1]], tokens[:i] + tokens[i + 2:]
    for i, w in enumerate(tokens):
        if w in ORD_WORDS:
            return ORD_WORDS[w], tokens[:i] + tokens[i + 1:]
    for i, w in enumerate(tokens):
        if re.fullmatch(r"\d{1,2}", w) and 1 <= int(w) <= 31:
            return int(w), tokens[:i] + tokens[i + 1:]
    for i, w in enumerate(tokens):   # "January eight 1991", "twenty-one August", "thirty January"
        if w in ("twenty", "thirty"):
            nx = tokens[i + 1] if i + 1 < len(tokens) else ""
            if nx in UNITS and UNITS[nx] > 0 and nx not in ("oh", "o") and TENS[w] + UNITS[nx] <= 31:
                return TENS[w] + UNITS[nx], tokens[:i] + tokens[i + 2:]
            return TENS[w], tokens[:i] + tokens[i + 1:]
        if w in UNITS and UNITS[w] > 0:
            return UNITS[w], tokens[:i] + tokens[i + 1:]
        if w in TEENS and i + 1 < len(tokens) and tokens[i + 1] in TENS:
            continue
        if w in TEENS:
            return TEENS[w], tokens[:i] + tokens[i + 1:]
    return None, tokens


def _day_opts(toks, i):
    """Day expressions starting at toks[i], longest first: [(day, n_tokens, is_ordinal)]."""
    if i >= len(toks):
        return []
    w = toks[i]
    nx = toks[i + 1] if i + 1 < len(toks) else ""
    out = []
    m = re.fullmatch(r"(\d{1,2})(st|nd|rd|th)?", w)
    if m:
        if 1 <= int(m.group(1)) <= 31:
            out.append((int(m.group(1)), 1, bool(m.group(2))))
    elif w in ("twenty", "thirty"):
        if nx in ORD_WORDS and ORD_WORDS[nx] < 10 and TENS[w] + ORD_WORDS[nx] <= 31:
            out.append((TENS[w] + ORD_WORDS[nx], 2, True))
        elif nx in UNITS and UNITS[nx] > 0 and TENS[w] + UNITS[nx] <= 31:
            out.append((TENS[w] + UNITS[nx], 2, False))
        out.append((TENS[w], 1, False))
    elif w in ORD_WORDS:
        out.append((ORD_WORDS[w], 1, True))
    elif w in TEENS:
        out.append((TEENS[w], 1, False))
    elif w in UNITS and UNITS[w] > 0:
        out.append((UNITS[w], 1, False))
    return out


def _last_day(toks):
    """The last day expression in toks (scanned left to right) -> (day, start, n) or None."""
    best, i = None, 0
    while i < len(toks):
        o = _day_opts(toks, i)
        if o:
            best = (o[0][0], i, o[0][1])
            i += o[0][1]
        else:
            i += 1
    return best


def _ymd(y, m, d):
    try:
        return datetime.date(int(y), int(m), int(d)).isoformat()
    except Exception:
        return ""


def _month_day_year(after, month):
    """'<month> <day> <year>': the day is the first day expression right after the month, the year the
    rest. A month followed only by a year phrase ('March nineteen eighty-eight') has no day -> ''."""
    k = 0
    while k < len(after) and not (_is_num_tok(after[k]) or _is_ord_tok(after[k])):
        k += 1
    if k >= len(after):
        return ""
    tail = [w for w in after[k:] if _is_num_tok(w) or _is_ord_tok(w)]
    if not any(_is_ord_tok(w) for w in tail) and _year_exact(tail)[1] == "full":
        return ""
    for d, n, _ in _day_opts(after, k):
        y = spoken_year(after[:k] + after[k + n:], round_ok=True)
        if y:
            return _ymd(y, month, d)
    if re.fullmatch(r"(19|20)\d\d", after[k]):               # "March 1988, the 14th"
        for d, n, _ in _day_opts(after, k + 1):
            return _ymd(after[k], month, d)
    return ""


def _find_month(toks):
    hits = []
    for i, w in enumerate(toks):
        for j, mn in enumerate(MONTHS):
            if len(w) >= 3 and mn.startswith(w[:3]) and (w == mn or w == mn[:3] or w == mn[:4] or mn.startswith(w)):
                hits.append((i, j + 1, w))
                break
    if not hits:
        return None, None
    for i, mth, w in hits:              # 'it may be March 3rd ...': the modal 'may' gives way to another month
        if w != "may":
            return mth, i
    return hits[0][1], hits[0][0]


def _birth_with_month(toks, month, mi):
    before, after = toks[:mi], toks[mi + 1:]
    nb = [w for w in before if _is_num_tok(w)]
    if nb and not any(_is_ord_tok(w) for w in before) and _year_exact(nb)[1] == "full":   # "1988 March 14"
        y = _year_exact(nb)[0]
        k = 0
        while k < len(after) and not _day_opts(after, k):
            k += 1
        o = _day_opts(after, k)
        return _ymd(y, month, o[0][0]) if o else ""
    bd = _last_day(before)
    if bd is None:
        return _month_day_year(after, month)
    # "<day> <month> <year>": day before the month, year after it. An ordinal right after the month with a
    # year behind it ("forty two, March 3rd 1984") is the day instead.
    k = 0
    while k < len(after) and not (_is_num_tok(after[k]) or _is_ord_tok(after[k])):
        k += 1
    o = _day_opts(after, k)
    if o and o[0][2]:
        y = spoken_year(after[:k] + after[k + o[0][1]:], round_ok=True)
        if y:
            return _ymd(y, month, o[0][0])
    day, s, n = bd
    y = spoken_year(after, round_ok=True)
    if y is None:                       # "1988, the 14th of March": a full year phrase before the day
        rest = [w for w in before[:s] + before[s + n:] if _is_num_tok(w)]
        y = _year_exact(rest)[0] if _year_exact(rest)[1] == "full" else None
    return _ymd(y, month, day) if y else ""


def parse_birth_date(text, order="dmy"):
    """Spoken or numeric birth date -> 'YYYY-MM-DD' or ''. Numeric-only dates use `order` (dmy or mdy).
    Spoken dates are split around the month word: '<day> <month> <year>' or '<month> <day> <year>'."""
    try:
        return _parse_birth_date(text, order)
    except Exception:
        return ""


def _parse_birth_date(text, order="dmy"):
    raw = str(text or "").lower()
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if m:
        return _ymd(m.group(1), m.group(2), m.group(3))
    toks = _btokens(raw)
    month, mi = _find_month(toks)
    if month:
        return _birth_with_month(toks, month, mi)
    nums = re.findall(r"\d+", raw)
    if len(nums) >= 3:
        a, b, c = int(nums[0]), int(nums[1]), int(nums[2])
        if len(nums[0]) == 4:
            return _ymd(a, b, c)
        if c < 100:
            c += 1900 if c > 30 else 2000
        d, mth = (a, b) if order == "dmy" else (b, a)
        if 1 <= mth <= 12 and 1 <= d <= 31:
            return _ymd(c, mth, d)
    return ""


def speak_date(d):
    return d.strftime("%A ") + str(d.day) + d.strftime(" %B")


def speak_amount(x):
    x = round(float(x), 2)
    return ("%d" % x) if x == int(x) else ("%.2f" % x)


def calendar_text(today, days=21):
    rows = []
    for i in range(days):
        d = today + datetime.timedelta(days=i)
        tag = "business day" if is_business_day(d) else ("holiday" if iso(d) in P["holidays"] else "weekend")
        rows.append(iso(d) + " " + d.strftime("%A") + " (" + tag + ")")
    return "\n".join(rows)


def dumps(x):
    return json.dumps(x, ensure_ascii=False)
