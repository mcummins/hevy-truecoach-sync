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
  2. Read the file; for each entry in `cookies`, set
     `document.cookie = '<name>=<value>; path=/; max-age=33177600; secure; samesite=lax'`.
     Values are stored in raw URI-encoded `document.cookie` form — set
     them exactly as-is, do not decode or re-encode.
  3. Navigate to `https://app.truecoach.co/client/workouts` and verify
     the tab is the Workouts view, not "Login | TrueCoach".
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
`results_by_date` array for that date is empty.

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
                             "tc_workout_id", "mode": "ui", "error"? } ],
  "forward_auto_synced": [ <copy of plan bucket verbatim> ],
  "reverse":             [ { "day_name", "status": "ok|skipped|error",
                             "tc_content_hash", "payload_hash",
                             "routine_id"?,    // present when POST created a new one
                             "error"? } ],
  "reverse_tombstones":  [ { "day_name"?, "routine_id",
                              "status": "ok|error", "error"? } ]
}
```

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

   Then ensure the Completed toggle is on:
   - Read the toggle's class list.
   - If it explicitly indicates completed (class string contains
     "complete"), leave it alone.
   - Otherwise click it, re-read the class list, and repeat — up to **a
     maximum of 3 clicks** total. The toggle may cycle through
     intermediate states (`is-pending` → `is-saved` → `is-completed`)
     so one click isn't always enough.
   - If after 3 clicks the class still doesn't indicate completed →
     log an error for that exercise (`status: error`, error: "couldn't
     advance toggle to Completed") and continue.

   The toggle class is read ONLY to know when to stop clicking — never as
   a skip signal.

   **Persistence quirk — already-completed exercises (important).**
   "Update results" only saves exercises TrueCoach considers dirty, and a
   text-only edit on an exercise that was *already* `is-completed` before
   this session does NOT mark it dirty — so your appended results silently
   fail to persist (confirmed via reload; synthetic `input` events and
   `execCommand('insertText')` both looked applied in the DOM but were
   dropped on save). The reliable fix: after setting the textarea value,
   **re-touch that exercise's Completed toggle** (a programmatic
   `button.exerciseStatus` `.click()` re-asserts `is-completed`, flips it to
   `is-saving`, and triggers a per-exercise save that captures the current
   textarea content). Exercises whose toggle you *had* to advance this run
   (empty/pending/missed → completed) already save their text fine — this
   only bites the rows that started completed and just got an appended note.
   **Always verify by reloading the edit page** and re-reading all five
   textareas before recording `status: ok`.
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
   empty, set it to `" "` (PUT rejects empty). Normalise each set to
   include `distance_meters: null, duration_seconds: null,
   custom_metric: null`. Do **not** include `folder_id` on PUT.

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

(Chrome fallback only: also close the **tab group** the extension
created — see the fallback section below.)

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

## Fallback: Claude in Chrome (legacy path)

Use when the `mcp__Claude_Browser__*` tools are absent from the run
environment, or (TrueCoach side only) when the `.tc_session.json`
bootstrap lands on the login page. This was the primary path before
2026-07-17; it rides Mark's real Chrome, where he stays logged into
TrueCoach — no `.tc_session.json` bootstrap is needed (and Chrome's own
TC session is untouched by the cookie-file flow). Partial fallback is
fine: TC via Chrome while Hevy API calls stay in the built-in browser.

- **Hevy API**: look for an existing Chrome tab at
  `https://api.hevyapp.com/`; create one if missing. The Chrome MCP's
  `javascript_tool` does NOT await promises, so extract GET responses
  with `document.write` + `get_page_text` (base64, blob, and localhost
  proxies are all blocked):

  ```js
  fetch('https://api.hevyapp.com/v1/workouts?page=1&pageSize=1',
        { headers: { 'api-key': '<HEVY_API_KEY>' } })
    .then(r => r.text())
    .then(t => { document.open(); document.write('<pre>' + t.replace(/</g,'&lt;') + '</pre>'); document.close(); });
  ```
  Then `get_page_text` on that tab and JSON-parse the `<pre>` contents.

- **TrueCoach**: `https://app.truecoach.co/` — already logged in.

- **Cleanup**: close every tab you opened AND the tab group the
  extension created for them — closing the tabs alone leaves an empty
  group header in the tab strip. Ungroup or close the group after the
  tabs are gone (e.g. `tabs_close_mcp` with the group's tab IDs, then
  remove the group itself, or a shortcut to close the group). If you
  can't find a programmatic way, at least ungroup the tabs before
  closing them.
