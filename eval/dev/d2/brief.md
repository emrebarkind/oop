# Brackwater Building Society: source pack for the arrears voice agent

Brackwater Building Society is fictional. Every customer, balance and payment in this pack is synthetic.

The project team pasted the four items below exactly as the Society sent them. Nobody has tidied them up. Where two items disagree, the later-dated item wins.

---

## Item 1: forwarded email

**From:** Morven Aske (Collections Operations, Brackwater)
**Sent:** Wednesday 14 October 2026 16:52
**To:** voice agent project team
**Subject:** FW: voicebot scope + the policy bits you asked for

Hi all,

Forwarding what Tam sent me below. Short version: the bot handles ONE product per call. That product is either a revolving credit account or an instalment loan, never both on one call. The policy extract I've attached (item 2) is the rulebook. Tam's numbers in his mail are from the old 2025 draft, so please ignore them and use the extract.

Three things legal want that are not in the extract:

- Open every call with: "Hello, this is Brackwater Building Society's automated service. Calls are recorded." Nothing about arrears until security is passed.
- If the customer asks for a real person, try once to help. If they still want a person, transfer to Q-GEN and close D30 / HU.
- If they say they've already paid, don't argue and don't take a PTP. Write an N-PCL note and close D20.

Cheers,
Morven

> **From:** Tam Oduya
> **Sent:** Friday 2 October 2026 09:14
> **Subject:** voicebot scope
>
> Morven, from memory: promise has to be inside 14 days, the bot gets three goes at it, and callbacks are 8 till 8. I'll dig out the proper document. Tam

---

## Item 2: policy extract

**Arrears Contact Policy ACP-7, version 4.1, effective 1 September 2026. Section 5: automated contact (extract).**

5.1 A business day is Monday to Friday, except a Society holiday listed in 5.2.

5.2 Society holidays: 2026-11-20, 2026-12-11, 2026-12-23, 2026-12-29, 2027-01-05.

5.3 "The Nth business day after today" means: count the business days that follow today, not counting today; the Nth one counted is the date. The 1st business day after today is therefore the next business day.

5.4 If anyone other than the customer answers, do not say why we are calling and do not discuss the account, even if they offer to pay. Ask when the customer can be reached.

5.5 A callback may be booked for today or for the next business day, at a time from 09:00 to 17:00 (both times included) that is later than the current time. Book it with `schedule_callback` and close D60 / CB.

5.6 If the time offered is outside 5.5, ask once more. If the second time is also outside it, book nothing and close D60 / NC.

5.7 If the person says the number is not the customer's, close D60 / WN. If the person says the customer has died, offer condolences, ask nothing further and close D60 / DC. If the agent cannot establish who is speaking after asking twice, close D60 / UN.

5.8 Security has two checks, asked one at a time: first date of birth (`DOB`), then house number (`HOUSE_NO`). Send the date of birth to `verify_identity` as eight digits, year then month then day, with no separators. A date the customer gives only as numbers is read day, then month, then year.

5.9 A failed check may be repeated. The fourth failed answer in total, across both checks, ends security. Then give no account details, suggest the customer visits a branch, and close D50 / IF.

5.10 If the customer will not answer, explain once that the checks protect their account. If they refuse again, close D50 / ID.

5.11 Arrears bands, from `dpd` (days past due):

| Band | `dpd` | Latest promise date | Attempts in total |
|---|---|---|---|
| A | 1 to 14 | the 8th business day after today | 2 |
| B | 15 to 45 | the 10th business day after today | 3 |

5.12 A promise date must be today or later, a business day, and not after the latest promise date of the band. A customer who names a non-business day inside the limit is offered the next business day. A customer who names a day after the limit is offered the limit.

5.13 Minimum acceptable amount ("floor"): for `revolving_credit` it is `min_repay`; for `instalment_loan` it is `scheduled_instalment`. An amount the customer offers from the floor up to the full arrears, both included, is accepted. An amount below the floor is refused and the floor is offered once.

5.14 After security, state the product and the arrears and `dpd`, then make the opening ask: the full arrears, today. The opening ask counts as attempt 1 for the attempt totals in 5.11.

5.15 Each flat refusal or non-answer uses one attempt. After the opening ask, the attempts continue in this order, while the band allows:
(a) attempt 2: the full arrears on the 3rd business day after today;
(b) attempt 3: the floor on the latest promise date of the band.
A date or an amount the customer proposes is never a refusal. Never make the same offer twice.

5.16 When the attempts are used up, close D40 / RF.

5.17 If the customer mentions illness, bereavement, job loss or other hardship: one sentence of sympathy, no further attempts, write an `N-HRD` note and close D40 / HS. If they still name a valid date or amount themselves, record the promise.

5.18 If the customer disputes the debt, do not argue and do not take a promise. Write an `N-DSP` note and close D40 / DS.

5.19 A request for a payment plan, reduced payments or a freeze: describe nothing, transfer to `Q-ARR` and close D30 / RS.

5.20 Read the amount and date back and ask for a clear yes before `create_promise`. If `create_promise` fails twice, tell the customer and close D40 / SE. After a recorded promise close D10.

5.21 No discounts, waivers, plans or terms other than those in this section may be offered.

---

## Item 3: memo

**MEMO CP-2026-19**
**Date:** Monday 26 October 2026
**From:** Head of Collections Policy
**To:** Collections Operations; voice agent project team
**Subject:** Changes to ACP-7 v4.1, section 5

With immediate effect, the three rules below replace the versions in ACP-7 v4.1. Every other rule stands.

1. **Rule 5.5:** the callback window now closes at 19:30 instead of 17:00. It still opens at 09:00, and both times are still included.
2. **Rule 5.11, band B, instalment loans only:** for an `instalment_loan` in band B the latest promise date is now the 6th business day after today. A `revolving_credit` account in band B keeps the 10th business day. Band A is unchanged for both products.
3. **Rule 5.18:** disputes now go to the Disputes team. Still write the `N-DSP` note first, then transfer to `Q-DSP` and close D30 / DQ. Do not close disputes as D40 / DS any more.

---

## Item 4: code list

**Dispositions (`close_call.disposition`)**

| Code | Meaning |
|---|---|
| D10 | Promise to pay recorded. No sub-code. |
| D20 | Customer says already paid. No sub-code. |
| D30 | Transferred to a team |
| D40 | No promise |
| D50 | Security not passed |
| D60 | Customer not reached |

**Sub-codes (`close_call.sub_code`)**

| Code | Use with | Meaning |
|---|---|---|
| RS | D30 | payment plan / restructure request |
| HU | D30 | asked for a person |
| DQ | D30 | dispute sent to Disputes team |
| RF | D40 | refused after all attempts |
| HS | D40 | hardship |
| DS | D40 | dispute (withdrawn, see memo CP-2026-19) |
| SE | D40 | system error |
| IF | D50 | failed checks |
| ID | D50 | declined checks |
| CB | D60 | callback booked |
| NC | D60 | no callback booked |
| WN | D60 | wrong number |
| DC | D60 | deceased |
| UN | D60 | unclear who answered |

**Note codes (`add_note.note_code`):** N-HRD hardship; N-DSP dispute; N-PCL customer claims already paid. No other situation is noted.

**Teams (`transfer_to_agent.team`):** Q-ARR arrears solutions; Q-GEN general enquiries; Q-DSP disputes.
