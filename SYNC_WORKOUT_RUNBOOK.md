# Workout sync runbook

One-liner to say to Cowork next time you finish a workout:

> **"Sync my latest Hevy workout to TrueCoach."**

This runbook tells Claude exactly what to do. Paste or reference it if the model
gets confused.

## Services + auth

| Service      | Access                                                    |
| ---          | ---                                                       |
| Hevy API     | `api-key: <HEVY_API_KEY>` (header) — load from `.env`     |
| TrueCoach    | Logged-in browser session on `app.truecoach.co`           |
| Google Photos| Logged-in browser session on `photos.google.com` *(v2)*   |

The literal `HEVY_API_KEY` value is **not** stored in this repo. It lives in
the local `.env` file (gitignored). Substitute it into the snippets below
when invoking them.

The Hevy API is **blocked from the Cowork sandbox**. All web requests go through
**Claude in Chrome**, from a tab parked on `https://api.hevyapp.com/`.

## Steps

1. **Get tabs context** (`mcp__Claude_in_Chrome__tabs_context_mcp`). Create tabs if none.

2. **Fetch latest Hevy workout**.
   - Park a tab on `https://api.hevyapp.com/`
   - Execute:
     ```js
     fetch('https://api.hevyapp.com/v1/workouts?page=1&pageSize=1', {
       headers: {'api-key': '<HEVY_API_KEY>', 'accept': 'application/json'}
     }).then(r => r.json())
     ```
   - Note workout's `start_time` date (use UTC date for matching).

3. **Find matching TrueCoach workout by date**.
   - Navigate to `https://app.truecoach.co/client/workouts?_=true`.
   - The page has two tabs: **Upcoming** and **Past**. A same-day workout
     (i.e. the one you just did) lives under **Upcoming** until the coach
     marks it completed — it does NOT appear under Past on the day itself.
     The Upcoming tab button has `id="ember28"`; click it if Past shows no
     match for today's date. (Past is the default when loading the page.)
   - Find the card dated same day as Hevy workout's `start_time`. Each
     card's text starts with a month abbreviation + day, e.g.
     `APR\n\n22\n\nWednesday, April 22nd`.
   - Click its "View workout" link → land on `/client/workouts/{ID}/edit`.

4. **Read TrueCoach exercise list + plan text** for each exercise.
   - DOM: each exercise has a `textarea[placeholder="Enter results"]`. Collect
     them in order — this is the order Hevy exercises will be mapped into.
   - For each exercise ALSO capture the **plan description text** (the
     instructions shown above/alongside the textarea — typically a sibling
     element to the textarea that contains lines like `Warmup / Bar × 10 /
     35kg × 5 / ... / Working sets / 5 x 5 / RIR 3`).
   - The plan text is fed to `parse_warmup_count()` so the translator knows
     the true warmup-count per exercise (rather than guessing from RPE).

5. **Match Hevy exercises → TrueCoach slots.** Mark logs Hevy in roughly
   the same order as the TrueCoach plan, but not always exactly. Don't
   assume index-order is correct — always do name-matching first.

   Algorithm:
   1. For each TrueCoach slot (A, B, C, D, E1, E2, ...) find the Hevy
      exercise whose title best matches the TrueCoach plan title. Fuzzy
      is fine — `"Bench Press (Barbell)"` ↔ `"Bench"`, `"Bicep Curl
      (Dumbbell)"` ↔ `"Dumbbell Biceps Curl"`, `"Triceps Pushdown"` ↔
      `"Tricep Extension"`, `"Straight Leg Deadlift"` ↔ `"Stiff Leg
      Deadlift"`.
   2. Expect **ordering swaps within a superset** (E1/E2 especially). The
      E1/E2 pair is effectively an unordered set — match by name, not by
      position.
   3. **Hevy often has extras** — warm-ups, mobility, cool-downs like
      `Dead Hang`, `wall angel`, etc. — that aren't in the TrueCoach
      plan. Quietly **ignore any Hevy exercise that doesn't match a
      TrueCoach slot**. Do not warn Mark about them; this is expected.
   4. Only STOP and ask Mark if:
      - A TrueCoach slot has no plausible Hevy match (missing data), OR
      - A Hevy exercise matches multiple TrueCoach slots ambiguously.

6. **Run the translator**. Compute `warmup_counts` per exercise from the plan
   text, then pass to the translator:
   ```python
   from hevy_to_truecoach import translate_workout, parse_warmup_count
   warmup_counts = [parse_warmup_count(plan) for plan in plan_texts]
   translated = translate_workout(hevy_workout, warmup_counts=warmup_counts)
   ```
   - `parse_warmup_count` returns an int when the plan has a `Warmup` /
     `Warm-up` heading, otherwise `None` (→ translator falls back to the
     RPE heuristic).
   - Returning `0` means "plan has a warmup heading but no sets under it" →
     no blank line inserted.

7. **Fill each textarea**. Default behaviour (confirmed 2026-04-22):
   - Empty textarea → set its value to the translator output.
   - Non-empty textarea → **append** the translator output, separated from
     the existing text by a single blank line. NEVER overwrite.
   - Use React-compatible value setting:
     ```js
     const setter = Object.getOwnPropertyDescriptor(
       window.HTMLTextAreaElement.prototype, 'value'
     ).set;
     const trimmed = el.value.replace(/\s+$/, '');
     const newValue = trimmed ? (trimmed + "\n\n" + block) : block;
     setter.call(el, newValue);
     el.dispatchEvent(new Event('input', {bubbles: true}));
     ```
   - If Mark explicitly says "replace" or "overwrite" instead, set the
     value directly — but the default is append.

8. **DO NOT submit**. Leave Mark to review in-browser and click "Update results"
   himself. This is deliberate — the whole point of review is to catch errors.

9. **Report** what was done, per exercise, with a short summary. E.g.
   "Filled 5 exercises; skipped 1 (chin-up had an existing note)."

## Video handling (v2 — not yet implemented)

Defer this for now. Manual video upload for the prototype.

Proposed v2:
- Navigate Google Photos, filter by the workout's UTC time window.
- Download each video (one trigger per thumbnail).
- Upload to TrueCoach via the `add attachment` button for each exercise,
  in order (video N → exercise N).

Complexity: ~5-10 Chrome tool calls per video × 6 exercises = 30-60 extra
interactions. Worth doing, but separate from the text pipeline.

## Scheduling

Scheduled tasks run in the sandbox and **cannot reach Claude in Chrome
autonomously**. Treat "scheduling" as reminders:

- A scheduled Claude task at Mon-Fri 12:00 and 19:00 Irish time can:
  - Check whether there's a Hevy workout from today that's not yet
    reflected in TrueCoach (by reading both via Chrome, assuming Mark
    has a browser open with the extension signed in).
  - If there is one, ping Mark: "Sync your workout to TrueCoach? Yes / No."
- If Mark says yes, it runs the flow above.
- If Mark's machine is asleep, the task silently fails and retries next slot.

## Files

- `hevy_to_truecoach.py` — pure translator, no side effects, 18 passing tests.
- `test_hevy_to_truecoach.py` — tests. Run with `python3 test_hevy_to_truecoach.py`.
- `verify_2026_04_20.py` — prints translator output vs. what Mark typed on 4/20.
- `workout_sync_report_2026-04-20.html` — HTML dry-run for that workout.
- `SYNC_WORKOUT_RUNBOOK.md` — this file.

## Rules inside the translator (quick reference)

- Every set (warmup + working) is logged.
- Blank line inserted between warmups and working sets. Where does the
  boundary come from?
  1. **Preferred:** count warmup-set lines under the `Warmup` heading in the
     TrueCoach plan (`parse_warmup_count`). Pass the result via
     `translate_workout(..., warmup_counts=[...])`.
  2. **Fallback** (no plan text supplied): RPE heuristic — blank line where
     RPE first jumps from <7 to ≥7.
- RPE → RIR: integer RPE → single number (`rir 3`). Half-step RPE → range (`rir 3-4`).
- Dumbbell weights halved (Hevy total → TrueCoach per-hand).
- Bodyweight-only exercises (chin-up, push-up) use accumulate format:
  `N total` + blank + per-set reps.
- Mixed-weight exercises (e.g. back extension 0kg then 10kg) keep `0kg x 15`
  for clarity — do NOT strip the 0kg.

### What counts as a warmup-set token in the plan?

TrueCoach sometimes renders the plan with newlines between entries, and
sometimes as a run-on single line (e.g. `Warm-up Bar x 10 30 x 5-10 Start at
45kg 4-5 x 10-12 RIR 2-3 Progress by 2.5kg`). The parser treats whitespace
— any kind — as a token delimiter, so both work.

Tokens that match `<weight> × <reps>` or `<weight> x <reps>`:
- `Bar × 10`, `Bar x 10`
- `35kg × 5`, `45 kg × 5`, `57.5 kg × 3`
- `30 x 5-10` (concrete weight, rep range — still a warmup)

Tokens that DON'T match (end the warmup count):
- `4-5 x 10-12` (set-count × rep-range — this is the working-set template)
- `Start at 45kg`, `RIR 2-3`, `Progress by 2.5kg` (instructions)
- A `Working sets` heading
