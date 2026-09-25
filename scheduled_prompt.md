# Scheduled Task Prompt — Bidirectional Hevy↔TrueCoach Sync

Dispatched at 12:09 & 19:09 Mon–Fri Irish time. Nothing in context at dispatch.

## Design goal

On a steady-state run (nothing changed): one Hevy API call, one TC page read,
zero writes, well under 30 seconds. Drill in only when a cheap probe detects
change. No email path — rely on the Cowork scheduled-task log for errors.

## Files (stable paths — don't reference per-session `work/` dirs)

The scripts live under the `personal_automation/` mount, which is the same
directory across sessions. Discover its path first:

```bash
AUTO_DIR=$(ls -d /sessions/*/mnt/personal_automation 2>/dev/null | head -1)
cd "$AUTO_DIR"   # use this for the rest of the run
```

Files in `$AUTO_DIR`:

| File | What |
|---|---|
| `bidirectional_sync.py` | Planner + committer + cache helper |
| `truecoach_to_hevy.py` | TC→Hevy per-exercise parser (`load_resolver`, `build_hevy_exercise`) |
| `hevy_to_truecoach.py` | Hevy→TC translator (`translate_workout`) |
| `feedback_tips.py` | Coach-feedback ingestion (form-tip map, 30-day TTL) |
| `.sync_cache.json` | Cache (auto-managed; module discovers the path itself) |

All Python modules are mutually importable from `$AUTO_DIR`.

Scratch snapshots for this run go in a throwaway dir — `/tmp/sync-run-<iso>/`.

## Access

Use the **built-in browser** (`mcp__Claude_Browser__*` tools) for both
Hevy and TrueCoach. If those tools are not available in this run's
environment, fall back to the legacy Claude-in-Chrome flow (see
"Fallback: Claude in Chrome" at the end of this file) and say so in the
Step 6 log line.

> **Check first, don't assume.** Verified 2026-08-31 and again
> 2026-09-07: the scheduled-task environment ships
> `mcp__Claude_Browser__*`, and the whole run (Hevy API + TC cookie
> bootstrap + TC edit-page DOM + Past tab + forward push + routine
> PUT) works on it. That is the primary path again. Between 2026-08-21 and
> 2026-08-31 only `mcp__claude-in-chrome__*` was present, so if you find
> the built-in tools missing, the Chrome fallback is still fully
> maintained — look at the actual tool list at the start of the run
> rather than assuming either way.
>
> **The built-in browser does not truncate `javascript_exec` output.**
> An 18 KB response came back whole (2026-08-31). The ~1 000-char cap
> described in the Chrome fallback section does NOT apply here, so on
> this path do **not** hand-rebuild API objects from chunked
> projections — fetch, and pass the parsed object straight through. That
> chunking workaround is what produced the guessed-`equipment` bug on
> 2026-08-24 (see Step 2a).
>
> **It does, however, cap on time: ~45 s per `javascript_exec` call.**
> Size is free, wall-clock is not — the constraint is the opposite of
> the Chrome fallback's. Anything that polls or sleeps has to be
> budgeted against that ceiling and split across calls when it doesn't
> fit (the TC Completed-toggle sweep is the one that bites — see Step 4
> FORWARD). A call that overruns returns an error, but the DOM work it
> already did still stands, so treat a timeout as "unknown state, go
> look", never as "nothing happened".

- **Hevy API**: header `api-key: <HEVY_API_KEY>`. The literal value is
  not stored in this repo — load it at task-dispatch time from the local
  `.env` file (`HEVY_API_KEY=...`, gitignored). Sandbox networking is
  blocked, so calls go through the browser: navigate a tab to
  `https://api.hevyapp.com/` (any path — it just establishes the
  origin), then run same-origin fetches with `javascript_tool`. The
  built-in browser's `javascript_exec` awaits promises, so return the
  fetch directly — no `document.write` tricks needed:

  ```js
  fetch('https://api.hevyapp.com/v1/workouts?page=1&pageSize=1',
        { headers: { 'api-key': '<HEVY_API_KEY>' } }).then(r => r.text())
  ```
  JSON-parse the returned string.

- **TrueCoach**: browser-only, `https://app.truecoach.co/`. The built-in
  browser starts logged out; bootstrap the session from
  `$AUTO_DIR/.tc_session.json` (gitignored, Dropbox-synced):

  1. Navigate a tab to `https://app.truecoach.co/` (landing on the
     login page is expected).
  2. **Expire any existing `ember_simple_auth-*` cookies first**, then
     set the ones from the file.

     The built-in browser keeps a persistent profile across sessions, so
     a previous visit can leave a *logged-out* session cookie behind
     (`ember_simple_auth-session={"authenticated":{}}`). Because that
     cookie may be scoped to a different path or domain, a plain
     `document.cookie = 'ember_simple_auth-session=...'` **adds a second
     cookie rather than replacing it**, and TC keeps reading the empty
     one — you stay on the login page and would wrongly conclude the
     token was revoked. Hit on 2026-08-31.

     **Clear and set in ONE `javascript_exec` call.** Do not split them
     across two calls. The Ember app is live on the page and rewrites a
     logged-out `ember_simple_auth-session={"authenticated":{}}` cookie
     within a second or so of the clear, so a clear-call followed by a
     separate set-call reliably lands you back at two cookies and the
     login page — the exact failure the clear was meant to prevent.
     Hit on 2026-09-07: clear returned `[]`, and by the time the next
     call ran the empty cookie was back and TC read that one.

     Clear across every plausible scope, then set, in the same script:

     ```js
     const names = ['ember_simple_auth-session',
                    'ember_simple_auth-session-expiration_time'];
     const paths = ['/', '/login', '/client', '/client/workouts', '', '/index.html'];
     for (const n of names)
       for (const p of paths)
         for (const d of ['', '.truecoach.co', 'app.truecoach.co', '.app.truecoach.co'])
           document.cookie = n + '=; path=' + p + (d ? '; domain=' + d : '') +
                             '; expires=Thu, 01 Jan 1970 00:00:00 GMT';
     const mid = document.cookie.split('; ').filter(x => x.startsWith('ember_simple_auth'));
     // mid must be [] — if not, widen `paths` further before setting.
     for (const [k, v] of Object.entries(cookiesFromFile))
       document.cookie = k + '=' + v + '; path=/; max-age=33177600; secure; samesite=lax';
     JSON.stringify({ mid, after: document.cookie.split('; ')
                                    .filter(x => x.startsWith('ember_simple_auth')) });
     ```

     Check `mid` is empty in the returned JSON — that's the "no stale
     cookies left" assertion, made mid-script where it's still true.
     Values are stored in raw URI-encoded `document.cookie` form — set
     them exactly as-is, do not decode or re-encode.

     The wider `paths` list above is what 2026-09-07 needed; the
     original three-path version left a survivor. Widen, don't narrow.
  3. Navigate to `https://app.truecoach.co/client/workouts` and verify
     the tab is the Workouts view, not "Login | TrueCoach". (Seeing the
     authenticated cookie listed *twice* afterwards is fine — duplicates
     of the same authenticated value are harmless. Only a duplicate
     carrying `{"authenticated":{}}` breaks the session.)
  4. Still on the login page ⇒ the token has been revoked. **Fall back
     to Claude in Chrome for the TrueCoach side** (see the fallback
     section — Mark's real Chrome stays logged in) and carry on with
     the run. Mixed mode is fine: keep using the built-in browser for
     the Hevy API (it doesn't need TC auth). Still surface the problem
     in the Step 6 log line ("TC session invalid — used Chrome
     fallback; log in and re-export .tc_session.json") so Mark knows to
     refresh the file. Abort only if the Chrome fallback is also
     unavailable or logged out. Never attempt a password login.
  5. After a successful login check, re-read the
     `ember_simple_auth-session` cookie from `document.cookie`; if its
     value differs from the file, write the new value back to
     `.tc_session.json` (keeps the stored copy fresh if TC ever
     rotates tokens).

  Upcoming list: `https://app.truecoach.co/client/workouts`. Dismiss
  the OneTrust cookie banner with "Reject All" if it appears.

## Hevy folder & routine model (v3)

The Hevy "Mark" folder (folder_id **2355979**) mirrors the **active week**
of TC's Upcoming. Each routine in the folder is titled with a bare day
name (`Monday`, `Tuesday`, …, `Sunday`). When the active week's days
shift, the planner first **repurposes** existing slots (a stale
day-name routine, or a leftover `_` tombstone) by PUT-renaming them to
the new day. Slots that can't be repurposed get tombstoned (Hevy's
public API doesn't support DELETE — see Step 4 details). Days with no
existing slot are POSTed.

The planner picks the active week:
- **Default**: the current calendar week (Monday → Sunday).
- **Promote to next week** when every TC workout falling in the current
  calendar week is already "handled" — meaning either dated strictly
  before today, OR already logged in Hevy (its date appears in
  `hevy_snapshot`), OR previously forward-synced (its tc_id appears in
  `cache.forward`). Vacuously true if the current week has zero TC
  workouts. The "completed" arms let the planner promote on a Wednesday
  afternoon once the week's last session is done — populating Hevy with
  next week's routines immediately rather than leaving the folder full
  of tombstones.

Same-day collisions inside the active week (rare): take the earliest by
date, defer the rest. Workouts outside the active week are deferred.
Deferred workouts get picked up automatically on a later run when they
enter the window.

**Folder protection**: deletion is restricted to routines whose title is
either a bare day name (`Monday`–`Sunday`) or a legacy `<Day> YYYY-MM-DD`
extras-format title (e.g. `Friday 2026-05-08`). Manual routines like
`Arm Snack` are left alone.

The current well-known routine IDs (used as initial state in the cache;
new ones are POSTed and discovered dynamically):
- Monday:    `8d32d602-1736-40e3-bea1-5e7b3ae5147a`
- Wednesday: `dccfa660-ad53-4845-aedc-0e6bcf7cefc2`
- Friday:    `b1bfc59d-acba-4f2c-a9e4-04ec04731328`

---

## Step 0 — Cooldown check (local, no network)

```
cd "$AUTO_DIR"
python3 bidirectional_sync.py stage0
```

Exit 10 ⇒ cooldown active (ran within last 30 min). Log one line and finish.
Exit 0 ⇒ proceed.

## Step 1 — Cheap probes

### 1a. Hevy newest workout

One GET via the browser: `/v1/workouts?page=1&pageSize=1`. Read the response id.

- If it matches `cache.stage1.last_hevy_workout_id` **and** that id is already
  in `cache.forward` → forward is done for this run. Skip forward drill.
- Otherwise we need to consider it. Keep the response around for Step 2a.

### 1b. TC Upcoming list + Hevy folder

Two cheap calls:

**TC**: navigate to `https://app.truecoach.co/client/workouts` (Upcoming
tab). Extract `(tc_id, date, day_name)` for every upcoming workout.

**Hevy folder**: GET `https://api.hevyapp.com/v1/routines?page=1&pageSize=10`
via the browser (paginate up to `page_count`). Filter to `folder_id == 2355979`
and capture `[{id, title}, ...]`. This is the current Mark folder state.

Fingerprint the TC list:
```
python3 bidirectional_sync.py fingerprint-tc-list --pairs '[["tc1","2026-04-27"],...]'
```

- If fingerprint matches `cache.stage1.tc_upcoming_fingerprint` AND the
  Hevy folder shape matches the days currently in `cache.day_routines`
  AND there's a `cache.reverse` entry for each of those days AND
  Step 1a said "no drill" → reverse is unchanged. Skip the drill.
- Otherwise drill (Step 2b). In particular, if Step 1a says drill
  (a new Hevy workout exists) we always drill reverse too — the
  newly-completed day may need its routine tombstoned, and that needs
  the active-week routine list and the Hevy folder state.

If **both** forward and reverse probes say "nothing to do", persist the
fingerprints and exit — but still run the feedback step (Step 1c) first.
If feedback is also a no-op, then:
```
python3 bidirectional_sync.py stage1 \
  --tc-fingerprint <fp> --hevy-workout-id <id>
```
Then:
```
python3 bidirectional_sync.py commit --results <empty results.json>
```
(An empty results file updates `last_run_at` and nothing else.)

### 1c. Coach-feedback scan

Cillian leaves freeform notes on completed TC workouts. We extract the
form-tip portions and stamp them onto the matching exercises in the next
Hevy routine push. Encouragement is dropped.

**Tip lifecycle (additive only)**: a tip lives for **30 days from its
captured_at, OR until a newer form tip on the same exercise replaces it
— whichever comes first**. That's the only way a tip leaves `form_tips`.
Silence does NOT clear a tip: an exercise that's mentioned with
encouragement-only, an "all good" remark, or no mention at all leaves
its prior tip in place. Cillian sometimes reviews workouts several days
late, so we never treat current silence as endorsement.

**Pending lifecycle**: a workout stays in `feedback_pending` until a
coach note actually appears on it — Cillian *always* comments
eventually, sometimes a few days after the session. A pending workout
is **never dropped just because no note was visible on a given run**.
The single exception is a hard age cap: `feedback_tips.py prune` retires
pending workouts older than **14 days** (`PENDING_MAX_AGE_DAYS`), on the
assumption that a comment that old is never coming. Don't hand-remove
pending entries.

**Probe (cheap, always)**:
```bash
python3 feedback_tips.py list-pending   # queued by prior forward syncs
python3 feedback_tips.py bootstrap-status   # {needs_bootstrap, workout_count}
```
- If `feedback_pending` is empty AND `needs_bootstrap` is false → only run
  `python3 feedback_tips.py prune` to drop expired tips, and skip the rest.
- Otherwise drill below.

**Drill (TC past-workouts list)**:

Navigate to `https://app.truecoach.co/client/workouts` and click the
**Past** tab. The tabs sit just under the page header; the inactive one
is labelled `Past` (with a hidden " Workouts" sr-only suffix). The list
shows each completed workout with its inline coach note when one
exists — no per-workout drill is needed just to read the note text.
A workout with a coach note shows a line like `CO Cillian O'Connor a
day ago` followed by the note body underneath the exercise list.
A workout with no such line has **no note yet**.

> **Check the author. Mark's own notes look identical.** Mark comments
> on his own workouts, and a client note renders in exactly the same
> place and shape as a coach note — only the name differs (`Mark
> Cummins 4 hours ago` vs `CO Cillian O'Connor a day ago`). Observed
> 2026-08-31 on workout `615171617`, whose only note was Mark's: *"Just
> FYI I'll be doing all my sessions in FlyeFit in Stillorgan from now
> on, so more equipment available."* Classifying that as coach feedback
> would have stamped a bogus `Coach Tip:` onto six exercises and pushed
> it into Hevy.
>
> So: a note counts as coach feedback **only** when its author line is
> Cillian's. Anything authored by Mark is client context — never a form
> tip, never a prescription, regardless of how instructional it sounds.
> If a card has only Mark's note, that is the **no note yet** case:
> call `apply-note` with `"note_present": false` so the workout stays
> in `feedback_pending` for Cillian's eventual review.
>
> When a card carries both authors, keep only Cillian's text before
> classifying. When concatenating multiple notes, filter by author
> first, then join.
>
> Client notes can still matter to Mark for other reasons (the FlyeFit
> one changes what equipment is available). Don't act on them, but do
> surface anything that looks like it affects the plan in the Step 6
> log line so he sees it.

Click it like this — **match on `textContent`, not `innerText`**. The
label is inside an sr-only span, so `innerText` is empty on these
buttons and any `innerText`-based lookup silently finds nothing:

```js
const past = [...document.querySelectorAll('button[role="tab"]')]
  .find(b => (b.textContent || '').trim().toLowerCase().startsWith('past'));
past.click();
await new Promise(r => setTimeout(r, 3000));
```

Then map cards to ids with
`[...document.querySelectorAll('a[href*="/client/workouts/"]')]` — the
hrefs come back in display order, so the first is the most recent
completed workout. To grab one card's note, walk up from its anchor to
the nearest ancestor whose `innerText` contains both the coach name and
the first exercise title.

Collect work for this run:
- Every entry in `feedback_pending`.
- If `needs_bootstrap == true`: also the **last 25** completed workouts
  whose `tc_workout_id` is not in `cache.feedback_processed`.

For each workout to process:

1. **Get the note** from the list view. Strip TC chrome, keep newlines.
   Occasionally a workout has more than one note attached — concatenate
   them (in display order, separated by a blank line) and treat the
   result as a single note for classification.

   **If there is no coach note on the past-tab card yet** (no `CO
   <Coach Name>` line / no note body), still call `apply-note` for this
   workout but with `"note_present": false` (classifications can be
   empty). That records nothing and **leaves the workout in
   `feedback_pending`** so it's re-checked on a later run once Cillian
   reviews. Then move on to the next workout. Do NOT set
   `note_present: false` together with real classifications — the flag
   means "no note existed at all". The `prune` step (run at the end) is
   what eventually retires a pending workout that's gone >14 days with
   no note; you never drop one by hand.

   This "no note yet" case is common for a workout completed in the last
   day or two — Cillian often reviews asynchronously, but he always
   comments eventually, so the workout must stay queued.
2. **Get the canonical exercise order** for that workout. Click into the
   workout (or use the existing detail-extractor flow from Step 2b) to
   capture `[{position, title}]`. Order matters — the coach typically
   reviews exercises in the same order they appear in the workout, even
   when his shorthand differs (`Chins` → `Chin-Up`, `Push-Up` →
   `Push-Up`, etc.).
3. **Classify** the note. This is YOUR job (Claude), not a regex:
   - For each exercise in the canonical order, decide if the note
     contains an **actionable** comment for it. Keep **any actionable
     instruction**, which is broader than just technique. Two kinds count:
     - **Technique cues** — "tuck the elbows", "drive through your
       heels", "slow the eccentric".
     - **Next-session prescriptions** — load/rep/tempo/progression
       instructions for next time, e.g. "Try the 20kg next time",
       "add a rep", "drop the rest to 90s", "go a bit deeper next set".
     Drop **pure encouragement** with no instruction: "good job",
     "looking great", "love the creativity", milestone calls ("getting
     to 10kg is great!"), "all good" / "no notes" affirmations, and bare
     observations with no action. If a comment both praises and
     instructs, keep the instruction.
   - Extract **verbatim, lightly trimmed**: quote the actionable
     sentence(s) and stitch together immediately related observations
     (e.g. "Keep thinking about tucking the elbows — your elbows flared
     out on reps 3 and 4."). Don't paraphrase. Don't summarise.
   - Output one entry per canonical exercise: a string for an actionable
     tip, `null` for encouragement-only / "all good" / no-mention. The
     `null` entries do NOT clear prior tips — they're recorded only so
     `feedback_processed.exercises_seen` reflects what was in the
     workout.
4. **Apply**. Write a JSON file (`note_present: true` because a note
   exists — the no-note case in step 1 uses `false`):
   ```json
   {
     "tc_id": "597481198",
     "note_present": true,
     "exercises": ["Bench Press", "Squat", "Chin-Up", ...],
     "classifications": {
       "Bench Press": "Keep thinking about tucking the elbows...",
       "Squat": "Try the 20kg next time.",
       "Chin-Up": null
     }
   }
   ```
   Then:
   ```bash
   python3 feedback_tips.py apply-note --file /tmp/sync-run-<iso>/feedback_<tc_id>.json
   ```
   With `note_present: true` the CLI moves the workout from
   `feedback_pending` to `feedback_processed` and **only sets** the tip
   slots that have a truthy classification. Null classifications never
   mutate `form_tips` (additive-only rule). Notes are immutable in TC so
   a workout id appearing in `feedback_processed` is sufficient — we
   won't re-process it on future runs. With `note_present: false` the
   CLI records nothing and leaves the workout in `feedback_pending`.

After processing all workouts:
```bash
python3 feedback_tips.py prune          # drop tips >30d AND pending workouts >14d
# Only mark bootstrap done once you've actually applied a note (i.e.
# called apply-note) for every workout in the last-25 window that has
# a note. Workouts you left in feedback_pending because Cillian hadn't
# reviewed yet do NOT count as "processed" for bootstrap purposes —
# but on subsequent runs they're picked up via feedback_pending
# anyway, so it's fine to mark bootstrap done as long as the
# unreviewed ones are queued. Skip the mark-bootstrap-done call until
# every last-25 workout with a coach note has been applied:
python3 feedback_tips.py mark-bootstrap-done
```

A repeat run with no new pending workouts and `feedback_bootstrap_done`
true falls back to the cheap-probe path automatically.

## Step 2 — Drill only where change was detected

### 2a. Forward drill (only if Step 1a said to)

Fetch enough recent Hevy workouts to cover the active week and a small
buffer — `/v1/workouts?page=1&pageSize=10` is plenty. Write
`hevy_snapshot.json`:
```json
{ "workouts": [
    { "id": "...", "date": "YYYY-MM-DD", "raw": <full workout obj> },
    ...
] }
```
The first entry is the most recent (used by the forward step — only
ever pushes one). The remaining entries cover the active week so the
planner can detect "completed days" and tombstone their stale Hevy
routines automatically. Each `raw` is the whole workout object from
Hevy. The `date` is the calendar date of `start_time` (UTC is fine).

> **Never invent fields when reconstructing `raw`.** Because the browser
> transport truncates output, you will normally rebuild `raw` in the
> sandbox from a projection you read back in chunks (see the fallback
> section). Copy **only** keys that the API actually returned. If a key
> `translate_workout` looks at is absent from the API response, leave it
> absent — do not fill it in from the exercise title, the TC plan, or
> your own knowledge of the movement.
>
> Specifically: **`/v1/workouts` does NOT return `equipment`** (verified
> 2026-08-24 — it is `undefined` on every exercise). `_is_dumbbell()` in
> `hevy_to_truecoach.py` falls back to matching `dumbbell`/`kettlebell`
> in the Hevy exercise **title**, which is the correct behaviour. Adding
> a guessed `"equipment": "dumbbell"` short-circuits that fallback and
> silently **halves** the weights pushed into TrueCoach. Hit on
> 2026-08-24: a guessed `equipment` on `Goblet Squat` turned
> 22.5/27.5/30/32.5kg into 11.25/13.75/15/16.25kg. Caught before the
> push only because the halving looked wrong by eye.
>
> Sanity check before the forward push: compare the translated weights
> against the Hevy set weights you read back. Any exercise whose numbers
> are exactly half should be treated as a bug, not a per-hand
> conversion, unless its Hevy title actually contains "Dumbbell" or
> "Kettlebell".

Also determine whether a TC slot exists for the most recent workout's
date. Open the TC Upcoming page (or filter the client workouts page by
date) and collect every TC workout on that date. Write `tc_recent.json`:
```json
{ "results_by_date": { "2026-04-23": [ { "tc_id": "..." } ] } }
```

You do **not** need to inspect whether the slot already has user-entered
results. The forward step is append-aware (see below) and skips
already-Completed exercises, so it's safe to push into a partially-filled
slot. The planner will only auto-mark "no_tc_slot_on_date" when the
`results_by_date` array for that date is empty **and** no day-name
fallback matches.

**Off-date workouts (day-name fallback, added 2026-09-25).** Mark sometimes
trains a day early or late. When there's no TC slot on the Hevy date, the
planner matches the Hevy workout's **title** (a bare day name, since he
starts from the day-named routine) to the nearest not-yet-synced TC
Upcoming workout with that `day_name` within ±3 days, and emits a normal
`forward` item for it. That TC day then counts as done for active-week
promotion. So always pass a complete `tc_upcoming.json` when forward
drills — the fallback reads it. (Hit 2026-09-25: "Friday" logged Thu 24
was wrongly auto-synced because TC's Friday was dated the 25th.)

**Completed-day tombstoning.** When a Hevy workout's date matches an
active-week TC day, the planner treats that day as "done" — it skips
the reverse push and emits a tombstone for the day's routine in the
Mark folder. So once you log Wednesday in Hevy, the Wednesday routine
gets retired the same run. The day still shows up in `deferred` with
reason `completed_in_hevy` for visibility.

### 2b. Reverse drill (only if Step 1b said to)

For **every** TC workout in the Upcoming list, open the detail page and
extract each exercise's title, plan text, and position code (`A`, `C1`,
`E2`, etc.). The planner decides which workouts are in the active week —
you don't need to filter; just write all of them.

**Plan-text fidelity matters.** Use the rendered exercise card's
`innerText` (preserves the visual line breaks) — do NOT collapse newlines
to spaces. The parser's inline preprocessor handles single-paragraph
input as a fallback, but a properly line-broken `\n`-delimited plan
gives the highest-fidelity parse (correct warmup/working split,
progression detection, notes capture).

**TC edit-page DOM (verified 2026-08-21).** The whole extraction is one
selector triple — no screenshots or coordinate clicking needed:

| What | Selector |
|---|---|
| Exercise card (one per slot, in order) | `li.workoutDisplay-exercise` |
| Position code (`A`, `C1`, …) | `.exercisePrefix` (within the card) |
| Exercise title | `h4[data-test="workout-item-title"]` |
| Plan text | `p.til` |
| Results box | `textarea` (exactly one per card) |
| Completed toggle | `button.exerciseStatus` |
| Save | the `button` whose text is `Update results` **or** `Finish workout` — see below |

```js
[...document.querySelectorAll('li.workoutDisplay-exercise')].map(li => ({
  position: (li.querySelector('.exercisePrefix')?.innerText || '').trim(),
  title:    (li.querySelector('h4[data-test="workout-item-title"]')?.innerText || '').trim(),
  plan:     (li.querySelector('p.til')?.innerText || '')
              .split('\n').map(s => s.replace(/\s+$/, '')).join('\n').trim(),
}))
```

Note the page has **7** textareas but only 6 exercise cards on a
six-exercise workout — there's a workout-level one too. Always scope the
textarea lookup to the `li`, never index into a flat `document`-wide
`querySelectorAll('textarea')`.

**The save button's label depends on workout state.** On a workout with
every exercise still `is-pending` it reads **`Finish workout`**; once
exercises have been marked completed it reads **`Update results`**
(verified 2026-08-31). The forward flow happens to toggle before saving,
so it sees `Update results` — but never look the button up before the
toggle step, and never treat a missing `Update results` as "the DOM
doesn't match". Match either label:

```js
const save = [...document.querySelectorAll('button')]
  .find(b => ['update results', 'finish workout']
               .includes((b.textContent || '').trim().toLowerCase()));
```

Both submit the same form. Only if *neither* is present should you log
the workout as a DOM mismatch and skip it per the guardrails.

Give the SPA ~3s after `navigate` before querying; it renders empty
otherwise (and `get_page_text` returns "No text content found").

**Inside `browser_batch` this is not reliable even at 3.5s.** The
batched `navigate` resolves well before the Ember app boots, so a
`navigate` + `await sleep(3500)` + query batch can still find **0**
`li.workoutDisplay-exercise` cards and abort the whole batch (hit
2026-09-07 on the forward push). Two options, both fine:
- batch `navigate` → `computer{action:"wait", duration:5}` → query, or
- run the query as its own call after the navigate.

Either way, **make the query assert its own card count**
(`if (lis.length !== N) throw`) rather than silently operating on an
empty list — an empty-list write would look like a clean no-op.

Write `tc_upcoming.json`:
```json
{ "workouts": [ {
    "tc_id": "595828381",
    "date": "2026-04-27",
    "day_name": "Monday",
    "raw_content": {
      "exercises": [
        { "position": "A", "title": "Deadlift", "plan": "Warm-ups\n..." },
        { "position": "B", "title": "Barbell Overhead Press", "plan": "..." }
      ]
    }
} ] }
```
Strip trailing whitespace per line; skip UI chrome. `raw_content` is what
the cache hashes — keep it stable across cosmetic whitespace/layout edits.

Also write `hevy_folder.json` from the folder state captured in Step 1b:
```json
{ "routines": [
    { "id": "8d32d602-...", "title": "Monday" },
    { "id": "dccfa660-...", "title": "Wednesday" },
    { "id": "b18cc8d6-...", "title": "Arm Snack" }
] }
```
Include **only** routines in folder_id 2355979.

If Step 2a didn't run, supply an empty `hevy_snapshot.json`
(`{"workouts": []}`) and `tc_recent.json` (`{"results_by_date": {}}`).

## Step 3 — Plan

```
python3 bidirectional_sync.py plan \
  --hevy         /tmp/sync-run-<iso>/hevy_snapshot.json \
  --tc-upcoming  /tmp/sync-run-<iso>/tc_upcoming.json \
  --tc-recent    /tmp/sync-run-<iso>/tc_recent.json \
  --hevy-folder  /tmp/sync-run-<iso>/hevy_folder.json \
  --out          /tmp/sync-run-<iso>/plan.json
```

Plan emits these buckets:
- `forward` — push Hevy results into a TC slot (append-aware; safe even
  if the slot already has content).
- `forward_auto_synced` — no action needed, cache as synced (only when
  no TC slot exists for the workout's date).
- `reverse` — for each day in the active week, push a Hevy routine. Each
  item carries `existing_routine_id`: if non-null, **PUT** that routine;
  if null, **POST** a new one (title = the bare day name). The planner
  may also **repurpose** a stale day-named slot or an existing `_`
  tombstone in the folder by setting `existing_routine_id` to that slot's
  id — the runner simply PUTs and Hevy's body title takes effect as a
  rename. (`repurposed_from` carries the former day name when applicable;
  the runner doesn't need to act on it but may surface it in the log.)
  The planner's reverse skip-when-unchanged check folds the active
  form-tip signature into `tc_content_hash`, so a tip change alone (new
  tip, expiry, or clear) is enough to emit a reverse item — even when
  the TC plan text is byte-identical to last run.
- `reverse_tombstones` — Hevy folder entries that need to become inert:
  bare-day-name slots no longer in the active week AND not already
  recycled, plus legacy `<Day> YYYY-MM-DD` extras. **PUT** each with a
  tombstone body (title `"_"`, placeholder exercise) — Hevy doesn't
  support DELETE.
- `deferred` — workouts the planner skipped this run (outside the active
  week or same-day collision). Surfaced in the Step 6 log line; no
  action.
- `active_week` — `{start, end, promoted}` for logging.

## Step 4 — Execute

Collect outcomes into `/tmp/sync-run-<iso>/results.json`:
```json
{
  "forward":             [ { "hevy_workout_id", "status": "ok|error",
                             "tc_workout_id", "date", "mode": "ui", "error"? } ],
  "forward_auto_synced": [ <copy of plan bucket verbatim> ],
  "reverse":             [ { "day_name", "status": "ok|skipped|error",
                             "tc_content_hash", "payload_hash",
                             "routine_id"?,    // present when POST created a new one
                             "repurposed_from"?,  // copy from the plan item — see below
                             "error"? } ],
  "reverse_tombstones":  [ { "day_name"?, "routine_id",
                              "status": "ok|error", "error"? } ]
}
```

**Copy `repurposed_from` through from the plan item verbatim** whenever
the planner set it. `commit()` reads it off the *result*, not the plan, and
uses it to `pop()` the old day out of `cache.day_routines` and
`cache.reverse`. Drop it and the cache keeps a stale entry pointing the
old day at a routine that has since been renamed — e.g. after Friday's
slot was renamed to Monday, `day_routines` held BOTH `Friday` and
`Monday` mapped to the same id, which would have let a later run
tombstone a live routine. (Hit on 2026-08-21; fixed by re-running
`commit` with the field present.)

### FORWARD (Hevy → TC) — UI

Only runs when `plan.forward` is non-empty (normally empty on a morning run;
only the most recent Hevy workout is considered).

1. **Translate** with `translate_workout(item["hevy_raw"])` →
   list of `{ title, text }` in TrueCoach slot order.
2. Navigate to `https://app.truecoach.co/client/workouts/<tc_slot_id>/edit`.
3. For each exercise card, matching by slot order, decide based on the
   **textarea content** alone (not on the toggle state):
   - **Empty** → set the translated text.
   - **Already contains the translated block** (substring match on the
     per-exercise text) → leave the textarea alone.
   - **Anything else** (your manual note, partial entry, coach text) →
     append the translated text after a single blank line. Never
     overwrite existing content.

   Track per exercise whether you **modified its textarea** this run
   (set or appended). You need this flag in the toggle step below.

   Then handle the Completed toggle. Read its class list, then follow
   exactly one of these branches:
   **The toggle is a 3-state CYCLE, not a switch.** Each `.click()`
   advances one step and wraps around:

   ```
   is-pending → is-completed → is-missed → is-pending → …
   ```

   (Observed 2026-08-21; a row can also start at `is-missed`.) So a click
   never "re-asserts" the current state — it always moves you off it.
   Both branches below therefore end with the same loop: **click, re-read
   the class list, repeat until `is-completed`.** Allow up to **4 clicks**
   (one full cycle plus one).

   **Wait out `is-saving` before you read the class list — a fixed sleep
   is not enough.** After a click the button carries `is-saving`
   alongside a *stale* state class, and the settled class can differ from
   what's on the element mid-flight. Reading during that window makes the
   loop both over- and under-click. Hit on 2026-09-07 with a flat 1.5s
   sleep: two rows read `is-saving … is-pending`, got clicked the full 4
   times, and only landed on `is-completed` by wrapping the cycle; a
   third row (already-completed, re-touched) read `is-saving
   is-completed`, so the loop exited immediately and the row settled on
   `is-missed` — Mark's completed exercise left marked missed, which is
   exactly the failure this loop exists to prevent.

   Poll instead of sleeping:

   ```js
   const settle = async (btn) => {
     for (let k = 0; k < 12; k++) {
       await new Promise(r => setTimeout(r, 1000));
       if (!btn.className.includes('is-saving')) break;
     }
     return btn.className.trim();
   };
   let cls = await settle(btn), clicks = 0;
   while (!cls.includes('is-completed') && clicks < 4) {
     btn.click(); clicks++; cls = await settle(btn);
   }
   ```

   Call `settle()` **before** the first read too, not just after each
   click — a row can still be saving from an earlier action. And in the
   already-completed-and-modified branch, `settle()` after the re-touch
   click before evaluating the loop condition, or you'll exit on the
   stale `is-completed` and leave the row on `is-missed`.

   Whatever the loop reports, **re-read every row's class list once more
   after a few seconds** before saving, and repair any row that isn't
   `is-completed`. That final sweep is what caught the 2026-09-07 miss.
   Run the sweep in its **own** call — see the budget note below.

   **Budget the calls: `javascript_exec` is killed at 45 s.** `settle()`
   polls for up to 12 s per row, so six rows can burn 72 s of polling on
   their own and a single call that does *set text → toggle → sweep →
   save* will not fit. Hit 2026-09-09: the sweep-plus-save call timed out
   at 45 s. The save had actually fired — the tab had redirected to
   `/client/workouts?_=true` — but the call returned an error instead of
   its report, so the run was left guessing what state it had produced.

   Split the forward push into three calls:

   1. set the textareas **and** run the per-row toggle loop (returns the
      per-row report);
   2. re-read every row's class list and repair any row that isn't
      `is-completed` — **no save in this call**;
   3. look the save button up and click it. Keep this one short: don't
      `await` a long sleep after `save.click()`, just return.

   On a wide workout (8+ exercises) split step 1 as well, three or four
   cards per call, scoping each call to `lis.slice(a, b)` — and keep the
   `lis.length !== N` assertion on the full list in every call.

   **A timed-out call is not a failed push.** The DOM work before the
   cut-off has usually landed. Do not retry it blind and do not re-run
   the toggle loop — a second pass over rows that already advanced would
   cycle them straight off `is-completed`. Instead re-read the page
   (a URL that has left `/edit` means the save went through) and let the
   mandatory reload verification below decide the outcome.

   - **Toggle already completed AND you modified the textarea** → you
     MUST still **re-touch it**. This is not optional — see the
     persistence quirk below. Do NOT "leave it alone": TrueCoach only
     saves exercises it considers dirty, and a text-only edit on an
     already-completed exercise is NOT dirty, so without the re-touch
     your appended text silently fails to persist. This is exactly the
     rows where Mark left a manual note and completed the exercise
     himself — the highest-risk case.

     The first click marks the row dirty but also moves it to
     `is-missed`. **You are not done.** Keep clicking round the cycle
     until it reads `is-completed` again, or you will leave Mark's
     completed exercises marked as missed. (Earlier versions of this
     runbook claimed the click flipped to `is-saving` and returned to
     `is-completed` on its own. It does not.)
   - **Toggle already completed AND textarea untouched** → do nothing.
     Don't click a row you didn't edit — you'd knock it off Completed
     for no reason.
   - **Toggle not completed** (`is-pending` / `is-missed`) → run the
     same click-until-`is-completed` loop. (Rows advanced this way save
     their text fine as a side effect — no extra re-touch needed.)

   If after 4 clicks the class still isn't `is-completed` → log an error
   for that exercise (`status: error`, error: "couldn't advance toggle to
   Completed") and continue.

   The toggle class is read ONLY to know which branch applies and when
   to stop clicking — never as a skip signal.

   **Why the re-touch (persistence quirk).** "Update results" only saves
   dirty exercises. Synthetic `input` events and `execCommand
   ('insertText')` both LOOK applied in the DOM but are dropped on save
   for already-completed rows (confirmed via reload). The toggle
   re-touch triggers a per-exercise save that captures the current
   textarea content.

   **Mandatory verification (do not skip).** After saving, reload the
   edit page and re-read **every** exercise's textarea. For each
   exercise you modified, confirm the expected text is present:
   - All present → proceed to record `status: ok`.
   - Any missing → re-apply the text for the missing rows, re-touch
     each of their toggles, save, and reload-verify **once more**.
   - Still missing after the retry → record `status: error` for the
     workout (error: "append lost on save: <exercise titles>") so
     `commit()` leaves it uncached and the next run retries. Surface
     the affected exercises in the Step 6 log line. NEVER record
     `status: ok` on a failed or skipped verification — an `ok` here
     caches the workout as synced and the lost text will never be
     retried.
4. Save the workout. Record `status: ok` + `tc_workout_id`. Pass the
   workout date through as the `date` field too — `commit()` uses it
   when auto-queuing the TC workout for coach-feedback processing
   (Step 1c) on a subsequent run. No manual `record-pending` call is
   needed; `commit()` runs `feedback_tips.record_pending(...)` for every
   successful forward result, and the call is idempotent.

### FORWARD AUTO-SYNCED

No action. Copy each `plan.forward_auto_synced` item verbatim into the
results file's `forward_auto_synced` bucket. Commit will mark them cached.

### REVERSE (TC → Hevy) — API

Once per run, load the resolver:
```python
from truecoach_to_hevy import load_resolver, build_hevy_exercise
resolver = load_resolver()
```

For each `plan.reverse[i]`:

1. Build per-exercise outputs. TC position codes like `C1`/`C2` form
   supersets (same letter, numeric suffix). Assign superset ids as
   consecutive integers starting at 0 for each distinct letter that has
   ≥2 entries; pass `None` for singletons.

   Also load the cache once at the top of the run and look up an active
   form tip per exercise (the helper handles TTL automatically):
   ```python
   import json
   from pathlib import Path
   from feedback_tips import get_active_tip
   from bidirectional_sync import CACHE_PATH

   cache = json.loads(Path(CACHE_PATH).read_text()) if Path(CACHE_PATH).exists() else {}
   ```
   Then per exercise:
   ```python
   tip = get_active_tip(cache, ex["title"])   # None if expired/missing
   built = build_hevy_exercise(
       ex["title"], ex["plan"], resolver,
       position_code=ex["position"],
       superset_id=super_map[ex["position"]],
       form_tip=tip,
   )
   ```
   `build_hevy_exercise` appends a `Coach Tip: <tip>` block after the
   parsed notes — separated by `---` when prior notes exist, or as a
   bare line when notes are empty (no orphan separator). Passing
   `form_tip=None` leaves notes alone. The helper also strips any prior
   coach-tip block (either shape, including the legacy `Form Tip:`
   label) before appending, so re-pushes don't accumulate duplicates
   when the tip changes.

   **Set-count sanity check (prose set counts).** The plan parser reads
   set counts from a line whose *leading* token is the count — `4 x 8-12`,
   `5-6 sets x RIR 2`. When Cillian buries the count mid-sentence, the
   parser doesn't see it and emits a **single placeholder set** with null
   weight and null reps. This survives `validate-put` (one set, not zero,
   so nothing is silently dropped) and ships a routine card with one blank
   set instead of the prescribed five. Hit on 2026-08-24 with
   `Band Assisted Chins` — plan text
   `"Use an amount of band tension that allows 5 x 8-12 at RIR 2"`
   produced 1 set.

   So after building, for each exercise where `len(built["sets"]) <= 1`
   **and** the single set has null reps, re-read the TC plan text and
   look for a count expressed in prose. A match for
   `(\d+)\s*(?:-\s*\d+)?\s*(?:x|×|sets?)\b` that is **not** at the start
   of a line is exactly the case the parser missed.

   **Fix by re-parsing, not by hand-expanding the sets array.** Hoist the
   matched fragment onto its own leading line and call
   `build_hevy_exercise` again on the patched plan text:

   ```python
   if len(built["sets"]) <= 1 and built["sets"][0].get("reps") is None:
       m = re.search(r'(\d+\s*(?:-\s*\d+)?\s*(?:x|×)\s*\d+(?:\s*-\s*\d+)?)', plan)
       if m and not re.match(r'^\s*' + re.escape(m.group(1)), plan):
           built = build_hevy_exercise(
               ex["title"], m.group(1) + "\n" + plan, resolver,
               position_code=ex["position"],
               superset_id=super_map[ex["position"]],
               form_tip=tip,
           )
   ```

   This routes through the parser's normal path, so warmup/working
   splits, the rep-range rules (HIGH end for working sets, LOW for
   warmups) and the bodyweight `weight_kg: 0.0` convention all come out
   identical to a well-formed plan. Hand-expanding the placeholder
   instead leaves `weight_kg: null` where the parser would have written
   `0.0`, producing a payload that differs from the same plan written
   properly. Verified 2026-08-24: the `Band Assisted Chins` text above
   goes from 1 null set to 5 × `(0.0, 12)`, and the untouched original
   prose is still preserved in `notes`.

   Then re-check `len(built["sets"])`. If it's still ≤ 1, leave it — and
   note it in the Step 6 log line, e.g.
   `reverse=2 (1 unparsed set count: Friday/Band Assisted Chins)`.

   If the plan genuinely has no set count anywhere (a hold, a "to
   failure" instruction, a free-text-only slot), leave the single set as
   is — that's the parser behaving correctly, not a miss.

   The durable fix is in `truecoach_to_hevy.py` (let the set-count
   matcher fire mid-line, not just line-anchored). Until that lands, the
   re-parse above is the workaround.

   **Confidence gate.** After building, inspect each
   `built["confidence"]`. The accepted ("unambiguous") values are:
   `history`, `catalog-exact`, `approved`, `history-fuzzy-strong`,
   `catalog-fuzzy-strong`. Everything else (`history-fuzzy`,
   `catalog-fuzzy`, `*-weak`, `no-match`) is low-confidence.

   The runner can't AskUserQuestion in scheduled-task context (the user
   isn't present). For each low-confidence exercise, record it in
   `pending_approvals.json` so Mark can clear it later via the
   `review-mappings` skill — but otherwise proceed with the best guess:
   ```
   python3 bidirectional_sync.py record-pending \
     --tc-title       "<TC title>" \
     --best-guess-id  "<built.exercise_template_id>" \
     --best-guess-title "<built.resolved_title>" \
     --best-guess-conf  "<built.confidence>" \
     --alternatives '[{"id":"...","title":"...","conf":"..."}, ...]'
   ```
   Pass the top 4-5 alternatives from `built["alternatives"]`. Then
   continue — the best guess goes into Hevy so the routine ships
   populated.

2. Build the routine body. **Title is the bare day name** (e.g.
   `"Thursday"`, `"Monday"`):
   ```python
   body = {
     "title": item["day_name"],
     "notes": item["date"],
     "exercises": [ strip_preview_fields(b) for b in builts ],
   }
   ```
   Strip preview-only fields from each exercise (`resolved_title`,
   `confidence`, `alternatives`, `warnings`, `tc_title`,
   `position_code`). Keep only `exercise_template_id`, `superset_id`,
   `rest_seconds`, `notes`, `sets`. For each exercise: if `notes` is
   empty, set it to `" "` (PUT rejects empty). Do **not** include
   `folder_id` on PUT.

   **The sets come out of `build_hevy_exercise` PUT-ready — do not
   rewrite them.** Every set already carries `distance_meters`,
   `duration_seconds` and `custom_metric` (null when unused), so there
   is nothing left to normalise. Older versions of this runbook told you
   to stamp `duration_seconds: null` onto every set; doing that now
   **destroys timed work** — a 4 × 60s hollow hold would arrive in Hevy
   as four empty sets. If you must touch a set dict, only ever add a
   *missing* key (`setdefault`), never overwrite one.

   **Always read `exercise_template_id` from the payload JSON your
   Python generated — never hand-type template IDs.** Save the built
   payloads to `/tmp/sync-run-<iso>/put_payloads.json` first, then read
   from it.

3. `payload_hash = bidirectional_sync.sha(body)`.

4. If `item.existing_routine_id` is set AND
   `payload_hash == item["prev_payload_hash"]`: record
   `{ day_name, status: "skipped", tc_content_hash, payload_hash }`. Skip
   the API call.

5. **PUT or POST** depending on `existing_routine_id`:
   - **`existing_routine_id` set**: `PUT https://api.hevyapp.com/v1/routines/<id>`
     with `{ "routine": body }`. On 200, record
     `{ day_name, status: "ok", tc_content_hash, payload_hash,
        routine_id: <existing_routine_id> }`. (Echo `routine_id` even on
     PUT — when the planner repurposed a slot, `commit()` needs it to
     update `cache.day_routines[day_name]` to the reused id.)
   - **`existing_routine_id` is null**: `POST https://api.hevyapp.com/v1/routines`
     with `{ "routine": { ...body, "folder_id": 2355979 } }`. The Hevy
     API **requires** `folder_id` on POST (PUT does not). On 201/200,
     parse the response — the new routine's id is at
     `response.routine[0].id` (or `response.routine.id` — inspect the
     body once and adapt). Record
     `{ day_name, status: "ok", tc_content_hash, payload_hash,
        routine_id: "<new_id>" }`. `commit()` will store the new id in
     `cache.day_routines` so subsequent runs PUT instead of POST.

6. **Verify the response before recording `status: "ok"`.** Hevy can
   return 200 while silently dropping exercises (historically: any
   exercise with `sets: []` is removed without error). Persist both the
   request body and the response, then run:

   ```bash
   python3 bidirectional_sync.py validate-put \
     --payload  /tmp/sync-run-<iso>/put_body_<day>.json \
     --response /tmp/sync-run-<iso>/put_response_<day>.json
   ```

   Exit 0 = clean (record `status: "ok"` as above). Exit 11 = silent
   drop detected; the printed JSON has a `dropped` array with each
   missing exercise. In that case record
   `{ day_name, status: "error", tc_content_hash,
      error: "silent_drop: <template ids>", routine_id: <id> }`
   — **do NOT** include `payload_hash`, so `commit()` won't cache the
   bad hash and the next run will retry. Surface the drop in the Step 6
   log line (e.g. `reverse=1 error (silent_drop Monday: Chin-Up)`).

7. Non-success status → `{ day_name, status: "error", error: "<status>
   <body-snippet>" }`.

### REVERSE TOMBSTONES (Hevy folder cleanup)

The Hevy public API has no DELETE on `/v1/routines/{id}` (only `GET, HEAD,
PUT` per OPTIONS) and rejects `folder_id` on PUT, so we can't delete or
move routines programmatically. Instead we **tombstone** stale routines:
PUT them with title `"_"` and a single-set placeholder body. They stay
in the folder but are inert and visually distinct (Mark batch-deletes
them in the Hevy UI when convenient).

For each `plan.reverse_tombstones[i]`:

1. `PUT https://api.hevyapp.com/v1/routines/<routine_id>` with this body:
   ```json
   {
     "routine": {
       "title": "_",
       "notes": " ",
       "exercises": [{
         "exercise_template_id": "79D0BB3A",
         "superset_id": null,
         "rest_seconds": 60,
         "notes": " ",
         "sets": [{
           "type": "normal", "weight_kg": null, "reps": null,
           "distance_meters": null, "duration_seconds": null,
           "custom_metric": null
         }]
       }]
     }
   }
   ```
   `79D0BB3A` is just an arbitrary placeholder (Bench Press) — the title
   `_` is what marks it as a tombstone.
2. Record `{ day_name, routine_id, status: "ok" }` on 200;
   `{ day_name, routine_id, status: "error", error: "<status>" }` otherwise.

## Step 5 — Commit

```
python3 bidirectional_sync.py stage1 \
  --tc-fingerprint <fp from 1b> \
  --hevy-workout-id <id from 1a>

python3 bidirectional_sync.py commit \
  --results /tmp/sync-run-<iso>/results.json
```

Only `ok` and `skipped` touch the reverse cache. `forward_auto_synced` entries
are committed unconditionally. Errors leave entries alone so the next run
retries.

## Step 6 — Log

One log line is enough. Surface the active week, deferred count,
pending-approvals count, and feedback activity if non-zero. Examples:

- Steady state (Step 1 stopped early):
  `sync: no changes (active=2026-05-04→2026-05-10)`
- Did work:
  `sync: active=2026-05-04→2026-05-10 (promoted) — reverse=3 (PUT Mon, PUT Wed, POST Thu), tombstones=3 (Friday, Friday 2026-05-08, Wednesday 2026-05-13), deferred=2 (Wed 2026-05-13, Fri 2026-05-15)`
- With pending approvals:
  `sync: active=… — reverse=3, tombstones=0, deferred=0 — 2 pending approvals (run /review-mappings to clear)`
- With coach feedback processed:
  `sync: active=… — reverse=2, feedback=3 notes (set 4 tips, cleared 2, expired 1)`
- Errors surface via the scheduled-task log in Cowork.

## Step 7 — Clean up the browser

Built-in browser: close every extra tab you created during this run
(`tabs_close`). The main tab can't be closed — leave it on the TC
workouts page or `about:blank`.

(Chrome fallback only: also tidy the **tab group** the extension
created — but only close tabs you opened yourself; see the fallback
section below.)

## Guardrails

- If Hevy returns 401/login page: log and abort, don't commit.
- If TC DOM doesn't match expectations (can't find a textarea or the
  Completed toggle): log that routine, skip it, continue with the others.
- If a single run exceeds ~5 minutes total: abort remaining work, log a
  partial-run summary, commit only what was already confirmed ok.
- Never delete TC content; forward is append-only. Reverse is full PUT-replace
  (by design).
- Template IDs come from the generated payload JSON file — never hand-typed.
- Never enter the TrueCoach password anywhere. TC auth is either the
  `.tc_session.json` cookie bootstrap or Chrome's existing logged-in
  session; if both are unavailable, abort and report.
- **A `javascript_exec` call that times out (~45 s) has still done
  whatever it did before the cut-off.** Don't retry it blind — re-read
  the page, work out what landed, and continue from there. See the
  budget note in Step 4 FORWARD for how to split long polling loops.
- **A `javascript_exec` write can be refused by the permissions
  classifier.** Seen 2026-09-07: the tombstone PUT was denied once
  ("Blocked by classifier"), then went through unchanged on a retry.
  One straightforward retry is reasonable. If it's refused again, do
  **not** try to disguise the call or route it somewhere else — record
  the item as `status: "error"` (so `commit()` leaves it uncached and
  the next run retries) and surface it in the Step 6 log line.

## Known-good behaviours — do NOT report these as bugs

Things that look wrong at a glance but are deliberate. Check here before
flagging a parser oddity in the Step 6 log.

- **`112.5 kg × 1+` → 12 reps.** A `+` suffix on a working set is an
  AMRAP marker. Mark wants it rendered as a 12-rep target so the Hevy UI
  shows a sensible goal rather than a literal single. Confirmed
  2026-08-21; locked in by
  `test_amrap_plus_still_maps_to_twelve_reps`. Warmup rep *ranges* take
  the LOW end and working ranges take the HIGH end — also deliberate.
- **`27 total` on the first line of a bodyweight block.** Chin-ups,
  push-ups and other all-zero-weight exercises use the accumulate
  format: sum-of-reps, blank line, then the per-set lines. **Timed holds
  are NOT this case** — an exercise whose sets are all
  `duration_seconds` with null reps takes the duration path and renders
  one `60 seconds` line per set (weight prefix and per-set RIR added when
  present). A timed hold coming out as `0 total` followed by blank lines
  means the duration check regressed; that is a bug. Fixed 2026-09-14,
  locked in by `test_timed_hold_renders_seconds_not_zero_total`.
- **Timed holds in the REVERSE direction ship `duration_seconds`, not
  reps.** `4 x 60 seconds` → four sets of `duration_seconds: 60` with
  `reps: null`. A Hevy routine card showing a hold as **60 reps** is the
  bug this replaced (hit 2026-09-18, Hollow Hold: `_TEMPLATE_SET_RE`
  claimed the line before anything looked at the unit word). Both
  directions now agree on the shape, so a hold round-trips losslessly.
  Fixed 2026-09-21; locked in by
  `test_timed_hold_parses_as_duration_not_reps` and
  `test_timed_hold_round_trips_through_hevy_to_truecoach`. Recognised
  units: seconds/secs/s and minutes/mins (minutes are converted). A
  duration mentioned in prose — "rest 90 seconds between sets" — stays
  in notes and is NOT a set; that is deliberate, not a miss.
- **`3-6 sets of 1-3 reps` → 6 sets × 3 reps.** The word-form connector
  ("sets of") is a template line like `3-6 x 1-3`, so both ends take the
  HIGH value. It is not the blank-reps placeholder case. Fixed
  2026-09-14, locked in by `test_sets_of_reps_wording_takes_high_ends`.
  The bare `<n> sets` / `<n>-<m> sets x RIR 2` wording still has no rep
  target and still ships placeholder sets — that one is correct.
- **`12 . kg` → 12.5kg.** Cillian types the decimal point and drops the
  5. The parser resolves a dangling decimal point to `.5` rather than
  failing the line. Confirmed 2026-08-21.
- **Dumbbell/kettlebell weights differ by a factor of 2 between the two
  systems.** TC plans are per hand; Hevy stores the two-hand total. The
  forward path halves, the reverse path doubles. If a mapping sends a
  neutrally-named TC exercise to a two-implement Hevy template (e.g.
  "Step Up" → "Dumbbell Step Up"), the override carries
  `"per_hand": true` to keep the round-trip symmetric.

  Confirmed instance (2026-09-07): Hevy `Kettlebell Shoulder Press`
  12/16/16/20 kg → TC `One Arm Kettlebell Press` 6/8/8/10 kg. The Hevy
  title contains "Kettlebell", `equipment` was verified absent on every
  exercise in the response, and the TC plan is per hand — so this
  halving is correct, not the guessed-`equipment` bug. Note the TC side
  is the one that names the movement single-arm; **match on the Hevy
  title, not the TC title.**

  **This bullet is not a blanket excuse for halved weights.** It applies
  only when the Hevy exercise title contains "Dumbbell" or "Kettlebell",
  or an explicit `per_hand` override is configured. A single-implement
  movement (goblet squat, landmine press, one-dumbbell suitcase carry)
  must NOT be halved. If you see halving on one of those, it means a
  guessed `equipment` field crept into the reconstructed workout — see
  the boxed warning in Step 2a. That is a bug; report it.

## Fallback: Claude in Chrome (legacy path)

Use when the `mcp__Claude_Browser__*` tools are absent from the run
environment, or (TrueCoach side only) when the `.tc_session.json`
bootstrap lands on the login page *after* the expire-then-set step in
the Access section — a duplicate stale cookie is not a revoked token.
This was the primary path before 2026-07-17 and again from 2026-08-21
to 2026-08-31; it rides Mark's real Chrome, where he stays logged into
TrueCoach — no `.tc_session.json` bootstrap is needed (and Chrome's own
TC session is untouched by the cookie-file flow). Partial fallback is
fine: TC via Chrome while Hevy API calls stay in the built-in browser.

- **Hevy API**: look for an existing Chrome tab at
  `https://api.hevyapp.com/`; create one if missing. **`javascript_tool`
  DOES await promises now** (verified 2026-08-21) — top-level `await`
  works and the last expression is returned, so just write:

  ```js
  const r = await fetch('https://api.hevyapp.com/v1/workouts?page=1&pageSize=1',
                        { headers: { 'api-key': '<HEVY_API_KEY>' } });
  JSON.stringify({ status: r.status, body: await r.text() })
  ```

  The old `document.write` + `get_page_text` dance is no longer needed.
  (Keep it in mind only if a future runtime regresses.)

- **Output is truncated at roughly 1 000 characters per
  `javascript_tool` call.** The INPUT is not truncated, so this only
  constrains what you read back. Two consequences:
  - **Reading big responses**: don't return a whole Hevy workout or
    routine list. Stash it on `window` (e.g. `window.__S = ...`) and
    return a compact projection — the fields the Python side actually
    needs — then reconstruct the object in the sandbox. For
    `hevy_snapshot.json` only `workouts[0].raw` is ever read by the
    planner, and `translate_workout` only touches per-exercise `title`,
    `notes`, `equipment` and each set's `type`/`weight_kg`/`reps`/`rpe`.
    Everything else can be dropped. The rest of the workouts need only
    `id` + `date`.

    **Project, never reconstruct from memory.** Build the projection in
    the page with a `.map()` over the real objects and read the result
    back; don't retype the workout by hand in the sandbox. And note that
    **`equipment` is not returned by `/v1/workouts`** — it comes back
    `undefined` on every exercise, so it will simply be missing from the
    projection. That is correct. Do NOT add it back. See the boxed
    warning in Step 2a for what a guessed `equipment` does to the
    forward-sync weights.
  - **Full ids**: never return a truncated/abbreviated id "to save
    space" — you'll have to re-fetch. Return them in a compact
    newline-joined string instead of pretty JSON.

- **Writing big payloads**: to PUT/POST a routine, paste the payload
  into the JS source as an object literal (`const body = {…};`) and
  `JSON.stringify(body)` in the fetch. JSON is valid JS, so this is a
  straight copy-paste from the generated `put_payloads.json` — do NOT
  wrap it in a template literal and `JSON.parse` it, because the `\n`
  escapes inside notes strings get expanded to real newlines first and
  `JSON.parse` then fails on them.

- **TrueCoach**: `https://app.truecoach.co/` — already logged in.

- **Cleanup**: close every tab you opened. Then deal with the tab group:
  closing the tabs alone can leave an empty group header in the tab
  strip. **But check the group's tab list first** — `tabs_context_mcp`
  can report tabs that Mark opened himself sitting in the same group
  (seen 2026-08-21). Close only the tabs you created; never close the
  group wholesale if it still contains a tab you didn't open. Leaving a
  tidy group header behind is much cheaper than closing Mark's tabs.
