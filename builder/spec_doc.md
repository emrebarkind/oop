# Spec format (what the extractor must produce)

The phone agent is a fixed state machine (opening → someone else / identity → account → negotiation →
read-back → recording → closing). The spec parameterises it. Produce ONE JSON object with exactly these keys.
Every value must come from the brief and the tool API file you are given. Where the brief says nothing,
use the default shown and record it in `assumptions`.

```
{
 "currency_word": "dollars",                       // how amounts are spoken ("dollars", "euros", "pounds", "lira")
 "calendar": {
   "workdays": [0,1,2,3,4],                        // Python weekday numbers of business days (Mon=0 ... Sun=6)
   "holidays": ["YYYY-MM-DD", ...]                 // every non-business date the brief lists
 },
 "date_rules": {"next_weekday": "following_week"}, // "following_week": 'next Friday' = Friday of the next calendar week;
                                                   // "upcoming": 'next Friday' = the first Friday after today
 "confirm": {"max_asks": 2},                       // how many times the agent asks "Am I speaking with ...?" before closing as unclear
 "callback": {                                     // when someone else answers and a callback is booked
   "days": ["today", "next_business_day"],         // allowed callback days. Items: "today", "next_business_day", "tomorrow",
                                                   // "within_business_days:N" (next N business days), "within_days:N" (next N days)
   "start": "10:00", "end": "19:00",               // allowed time window, inclusive, 24h HH:MM; a window that crosses
                                                   // midnight has start > end (the days name the evening it starts on)
   "after_now": true,                              // the callback must be later than now
   "business_days_only": false,
   "windows": {},                                  // only if some weekday has its own window: {"5": {"start": "09:00",
                                                   // "end": "13:00"}} (weekday 0 = Monday ... 6 = Sunday; null = no callbacks)
   "max_asks": 2                                   // total asks for a time before giving up (ask once more after the first bad one = 2)
 },
 "identity": {
   "questions": [                                  // in the order the brief asks them, one per verify call
     {"field": "<value the verify tool expects for this question>",
      "kind": "date" | "digits" | "year" | "upper" | "text" | "email",  // date -> a date; digits -> digits only;
                                                   // upper -> letters+digits code (postcode, reference); text -> words (a name)
      "length": 4,                                 // for digits/upper: exact number of characters expected (omit otherwise)
      "format": "%Y-%m-%d",                        // date: how the verify tool wants the date (strftime), default %Y-%m-%d;
                                                   // upper: "space_before_last:N" if the tool wants one space before the last N
                                                   // characters (e.g. a UK postcode "KM4 7RT" = space_before_last:3); omit = no spaces
      "ask": "<the spoken question>"}
   ],
   "max_wrong": 3,                                 // wrong answers in total that end the check
   "max_wrong_per_question": null,                 // only if the brief counts wrong answers per question instead
   "date_order": "dmy",                            // how a numeric-only date is read: "dmy" or "mdy"
   "hints": {"<hint value from the tool API>": "<what the agent says when that hint comes back>"},
   "purpose_is_decline": true,                     // asking what the call is about instead of answering counts as declining
   "question_is_decline": false,
   "response": {"ok": "ok", "verified": "verified", "all_verified": "all_verified",
                "left": "wrong_answers_left", "hint": "hint",   // field names in the verify tool's response
                "pass_values": [],                 // only if `verified` is not a boolean: the values that mean the answer matched
                "reject_values": []}               // values meaning the answer was not checked (e.g. "unreadable"), not counted
 },
 "products": {
   "names": {"<product type>": "<spoken name, may use {field} from the account record, e.g. credit card ending {card_last4}>"},
   "amount_field": "past_due",                     // account field with the amount owed
   "days_field": "days_late",                      // account field with how many days late the product is
   "floor_field": {"<product type>": "<account field holding the minimum acceptable amount>"},
   "floor_pct": null,                              // only if the floor is a percentage of the amount owed instead (0 = any amount)
   "floor_names": {"<product type>": "<what the brief calls the floor, e.g. minimum payment>"},
   "amount_scale": 1,                              // 100 only if get_account gives amounts in cents/pence (minor units)
   "promise_scale": null,                          // only if create_promise takes another unit than get_account gives:
                                                   // 100 when get_account is in pounds/euros but create_promise wants pence/cents
   "floor_round": null                             // with floor_pct only: "up_unit" (round up to a whole unit) or "nearest_unit"
   // If the account records have no product type, use the key "*" in names / floor_field / floor_names.
   // Optional, normally read from the tool API file: "list_field" (the array of products in the get_account
   // response), "id_field" (product id), "type_field" (product type).
 },
 "bands": [                                        // lateness bands, by the account's days_late, inclusive bounds
   {"name": "early", "min": 1, "max": 7,
    "deadline": {"kind": "business_days_after", "n": 12},  // kinds: business_days_after (n-th business day after today),
                                                   // calendar_days_after, end_of_month, end_of_next_month, last_business_day_of_month,
                                                   // end_of_week, end_of_next_week (last business day), day_of_next_month (n-th day),
                                                   // business_days_after_or_end_of_month (whichever is first),
                                                   // later_of / earlier_of with "of": [rule, rule] (e.g. the later of the
                                                   // last business day of the month and the 3rd business day).
                                                   // "roll_if_not_after_today": true when the brief says to use the next
                                                   // period's date if the computed one is today (e.g. 'or next month's')
    "deadline_by_type": {},                        // only if a product type has its own deadline in this band:
                                                   // {"<product type>": <deadline rule>}
    "attempts": 1,                                 // persuasion attempts allowed in this band
    "ladder": null}                                // only if this band has its own persuasion steps (same format as "ladder")
 ],
 "bands_outside": null,                            // only if the brief says what happens to a days_late value that is in no
                                                   // band: {"band": "<band name>"} or {"event": "<event key>"}; omit otherwise
 "ladder": [                                       // persuasion attempts in order; attempt k uses ladder[k-1]
   {"amount": "full" | "floor" | "full_or_floor" | "pct:N",  // full_or_floor: the floor if the customer already said the full
                                                   // amount is too much; pct:N = N percent of the amount owed
    "date": "last_business_day_this_week" | "last_business_day_next_week" | "deadline" | "today" | "tomorrow" |
            "next_business_day" | "business_days_after:N" | "calendar_days_after:N"}
 ],
 "rules": {"refuse_deadline_is_refusal": true,
           "deadline_adjust": "previous_business_day",    // if a computed deadline is not a business day: previous_business_day,
                                                   // next_business_day, or none (only if the brief says promise dates need not be business days)
           "first_ask": "today",                   // the first payment question after disclosure: "today" (full amount today) or
                                                   // "deadline" (full amount by the deadline); a plain yes agrees to that
           "too_much_floor": "attempt_1",
           "ladder_repeat_last": false},           // true only if the brief says the last step is repeated for further attempts         // when the customer says the full amount is too much before any attempt:
                                                   // "attempt_1" = the floor is attempt 1 (on attempt 1's date, uses the attempt);
                                                   // "asked_date" = the floor for the date just asked, using no attempt
 "after_promise": {"ask_anything_else": true},     // give the customer one more turn after a recorded promise
 "notes": {"enabled": false},                      // true if the brief requires add_note for products that end without a promise
 "multi": {"clamp_single_date": true,              // one date for all products: a product whose deadline is earlier gets its own deadline
           "continue_after_paid": true, "continue_after_dispute": true},
 "human": {"ask_first": true},                     // asks once whether it can help before transferring to a human
 "max_not_talk": 2,                                // "I don't want to talk" this many times ends the call
 "events": {                                       // how each situation ends in the bank's records. Use the brief's exact codes.
   "promise":                {"outcome": "...", "reason": ""},
   "paid":                   {"outcome": "...", "reason": "", "note": "<add_note reason value>"},
   "transfer_restructuring": {"outcome": "...", "reason": "...", "queue": "<transfer queue value>"},
   "transfer_human":         {"outcome": "...", "reason": "...", "queue": "..."},
   "transfer_support":       {"outcome": "...", "reason": "...", "queue": "..."},
   "refused":                {"outcome": "...", "reason": "...", "note": "..."},
   "hardship":               {"outcome": "...", "reason": "...", "note": "..."},
   "dispute":                {"outcome": "...", "reason": "...", "note": "..."},
   "service_error":          {"outcome": "...", "reason": "...", "note": "..."},
   "id_failed":              {"outcome": "...", "reason": "..."},
   "id_declined":            {"outcome": "...", "reason": "..."},
   "callback":               {"outcome": "...", "reason": "..."},
   "no_callback":            {"outcome": "...", "reason": "..."},
   "wrong_number":           {"outcome": "...", "reason": "..."},
   "deceased":               {"outcome": "...", "reason": "..."},
   "unclear":                {"outcome": "...", "reason": "..."}
   // optional: "callback_requested": {"outcome": "...", "reason": "..."} only if the brief says what to do when the
   // VERIFIED customer asks to be called back later (the agent then books a callback in the callback window)
 },                                                // reason "" when the outcome table lists none
                                                   // queue "": the brief does not transfer in this situation (the call
                                                   // closes with that event instead); note "": no add_note for this outcome
 "tool_bodies": {                                  // request body field -> engine slot, for each tool. Slots:
   "verify_identity": {"<question field param>": "q_field", "<answer param>": "q_answer"},
   "get_account": {},
   "create_promise": {"<product id param>": "p_product", "<amount param>": "p_amount", "<date param>": "p_date"},
   "add_note": {"<product id param>": "n_product", "<reason param>": "n_reason"},
   "schedule_callback": {"<date param>": "cb_date", "<time param>": "cb_time"},   // or {"<datetime param>": "cb_datetime"}
                                                   // (sent as YYYY-MM-DDTHH:MM) if the tool takes one field
   "transfer_to_agent": {"<queue param>": "t_queue"},
   "close_call": {"<outcome param>": "c_outcome", "<reason param>": "c_reason"}
 },                                                // customer_id is added automatically; do not list it.
                                                   // Every other REQUIRED request field must be mapped: to a slot, or to
                                                   // "const:<value>" for a fixed value the brief or API gives
 "responses": {},                                  // optional, normally read from the tool API: per tool, the success flag of its
                                                   // response, e.g. {"create_promise": {"field": "status", "values": ["created"]}}
 "intents": {},                                    // optional: how the brief defines a situation, one line each, quoting its
                                                   // examples, for keys among hardship, dispute, already_paid, restructure, vague,
                                                   // too_much, human. Only where the brief defines it.
 "call_variables": [],                             // call variables the brief lists besides customer_id, first_name,
                                                   // last_name, title, now (e.g. "company_name"), if lines use them
 "lines": { ... },                                 // every sentence the agent speaks, see below
 "assumptions": ["..."]                            // anything you had to decide that the brief does not say
}
```

## Lines

Write each sentence in plain spoken English, short. Where the brief prescribes words, use them exactly.
Placeholders in braces are filled by the engine: {title} {first_name} {last_name} {name} (product name)
{amount} (spoken amount with currency) {date} (spoken date) {days} (days late) {time} {deadline} {floor} {full},
plus any other call variable the brief lists (declare it in "call_variables", e.g. ["company_name"]). No other placeholders.
The engine joins lines into one turn, so: these lines are statements that another sentence follows, and must NOT end
with a question: robot, scam, tp_no_info, not_talk_once, id_wrong, id_why, id_explain, no_info, below_floor_again,
paid_line, dispute_line, recorded, sms_line, record_failed. These are one bare question with no preamble (no "Thank
you." or "I'm sorry" in front): confirm_ask, tp_ask, tp_ask_time, ask_today, ask_today_multi, offer_short, confirm_q,
hardship_short, anything_else.
Never mention the debt, a payment, the product or the bank department before identity is verified, in any
line used before verification (opening, confirm_ask, robot, scam, purpose_before_verify, tp_*, wrong_number,
condolence, id_*). Use the word "recorded" ONLY in the lines `recorded`, `sms_line` and `record_failed`
("can record" in an offer is fine): a judge reading the transcript treats any other "recorded" as a claim that a promise was saved. If the
brief requires telling the customer that the call is recorded, say "we record this call" (active voice, never
"this call is recorded").

Required keys:
opening, confirm_ask, robot, scam, purpose_before_verify, no_info, not_talk_once,
tp_ask, tp_ask_time, tp_outside_window, tp_no_info, tp_booked, wrong_number, condolence,
id_intro, id_why, id_explain, id_declined, id_repeat, id_continue, id_wrong, id_failed,
disclose_intro, product_line, ask_today, ask_today_multi, next_product,
offer, offer_floor_date, offer_floor, below_floor, below_floor_again, date_past, not_business_day,
after_deadline, vague_ask, propose_deadline, ask_date_for_amount, repeat_offer,
readback_intro, readback_item, clamped_note, confirm_q, recorded, sms_line, record_failed,
paid_line, dispute_line, hardship, hardship_declined, hardship_short, restructure_line, human_ask_help, transfer,
offer_short, offer_short_date, anything_else, promise_change, go_on, goodbye

Meaning of the less obvious ones:
- opening: the first sentence of the call (who is calling). confirm_ask: "Am I speaking with {title} {last_name}?"-style question.
- tp_*: someone else answered. tp_ask asks when the customer can be reached; tp_booked confirms a booked callback ({date} {time}).
- id_continue: said when the customer gave only part of an answer (e.g. the day and month without the year): an
  invitation to go on, e.g. "Yes, and the year?" or "Please go on." (never "Thank you.", which sounds like the answer was accepted).
- id_intro precedes the first security question; id_why/id_explain explain why the questions are needed; id_failed is said after too many wrong answers (include where to go instead, if the brief says).
- disclose_intro: the sentence after verification that says why you call; product_line: one late product ({name} {amount} {days}).
- ask_today / ask_today_multi: the first payment question (one product / several).
- offer: a persuasion offer ({amount} on {date}); offer_floor_date: THE one offer of the floor ({amount} on {date}) after the customer
  said the full amount is too much: include what happens to the rest if the brief says so (this line is said only once per product);
  offer_floor: the same without a date;
  below_floor: the amount is under the floor; after_deadline: the date is past the latest allowed ({date} = latest date); not_business_day: ({date} = next business day);
  vague_ask: ask once for the exact day; propose_deadline: propose ({amount} on {date}).
- In offers, {amount} already names the floor when the amount is the floor ("the minimum payment of 126 dollars"), so lines need not
  name it again.
- readback_item: one promise ({name} {amount} {date}); confirm_q: the confirmation question the brief prescribes.
- recorded: said only after the bank confirmed the promise; sms_line: any sentence the brief requires after a recorded promise ("" if none).
- record_failed: the promise could not be recorded.
- hardship: one sentence of sympathy plus the offer the brief prescribes (e.g. connect to a support team).
- hardship_short: a short re-prompt after an unclear answer to the hardship offer, WITHOUT naming the team again ("Would you like that?").
- hardship_declined: said when the customer declines the hardship offer; do not mention the team or the offer again.
- below_floor_again: a short acknowledgement WITHOUT any amount (another offer may follow in the same turn).
- paid_line / dispute_line / hardship_declined: never claim that something was noted or recorded (the note is written
  later, and not at all if the call ends in a transfer); describe what happens next instead.
- offer_short: a neutral re-prompt after a side question, WITHOUT the amount ("Would that work for you?").
- offer_short_date: the date of the pending floor offer ({date}), e.g. "That would be on {date}."
- go_on: said when the customer stopped mid-sentence ("Sorry, please go on.").
- anything_else: asked once after a recorded promise ("Is there anything else I can help you with?").
- promise_change: what to say if the customer wants to change a promise that is already recorded (use the brief's rule,
  e.g. "You can change it in the mobile app."; if the brief says nothing, say it cannot be changed on this call).
- goodbye: thanks and goodbye, said at the end of every call.

Optional lines (a sensible default is used when absent): callback_failed (the callback could not be booked),
transfer_failed (the transfer did not go through), confirm_again (a second, plainer read-back question, e.g. "Sorry, I
did not catch that. Do you confirm, yes or no?"), cb_ask (asks a verified customer when to call back, if
callback_requested is used), system_error (the bank's system does not respond), human_no_transfer (REQUIRED when
events.transfer_human has no queue: what the agent says instead of transferring, using the brief's words),
restructuring_no_transfer / support_no_transfer (optional, the same for those situations). no_band_line (said for a product whose days late fall in no band when the brief gives no rule,
{name}), below_floor_final (said when the
customer insists on an amount below the floor after it was offered: "I'm sorry, I can't record less than {amount}.").
