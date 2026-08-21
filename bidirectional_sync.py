"""Bidirectional sync planner/committer — v3 (calendar-week model).

The Hevy "Mark" folder mirrors the upcoming TC week. Each routine is titled
just by day name ("Monday", "Wednesday", …). The planner determines an
"active week":

  - **Default**: the current calendar week (Monday–Sunday).
  - **Promotion**: when every TC workout in the current week is in the past,
    use next week instead. (Vacuously true if the current week has zero TC
    workouts.)

Within the active week, each weekday with a TC workout becomes one Hevy
routine. Same-day collisions (rare; e.g. two Wednesdays this week) take the
earliest and defer the rest. Workouts outside the active week are deferred.
Routines in the Hevy folder for days NOT in the active week are deleted.

Cache schema::

    {
      "forward":  { "<hevy_workout_id>": {"synced_at", "tc_workout_id",
                                          "mode", "reason"?} },
      "day_routines": { "Monday": "<hevy_routine_id>",
                        "Wednesday": "<hevy_routine_id>", ... },
      "reverse":  { "<DayName>": {"tc_content_hash", "payload_hash",
                                  "last_pushed_at"} },
      "stage1":   { "tc_upcoming_fingerprint", "last_hevy_workout_id" },
      "last_run_at": ISO
    }

Snapshots passed to plan()::

    hevy_snapshot:     {"workouts": [{"id", "date", "raw"}, ...]}
    tc_upcoming:       {"workouts": [{"tc_id", "date", "day_name",
                                      "raw_content"}, ...]}
    tc_recent:         {"results_by_date": {"YYYY-MM-DD":
                                            [{"tc_id"}, ...]}}
    hevy_folder:       {"routines": [{"id", "title"}, ...]}  # Mark folder
    today (optional):  ISO date string overriding "now" for testing

Plan output::

    {
      "forward":              [...],
      "forward_auto_synced":  [...],
      "reverse":              [{"day_name", "tc_workout_id", "date",
                                "tc_content_hash", "raw_content",
                                "prev_payload_hash", "existing_routine_id"}],
      "reverse_tombstones":   [{"routine_id", "former_title", "day_name"?}],
      "deferred":             [{"day_name", "date", "tc_id", "reason"}],
      "active_week":          {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD",
                               "promoted": bool},
      "generated_at": ISO
    }

Results passed to commit()::

    {
      "forward": [...],
      "forward_auto_synced": [...],
      "reverse": [{"day_name", "status": "ok|skipped|error",
                   "tc_content_hash"?, "payload_hash"?,
                   "routine_id"?, "error"?}],   # routine_id present after POST
      "reverse_tombstones": [{"day_name"?, "routine_id",
                              "status": "ok|error", "error"?}]
    }
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

COOLDOWN_MINUTES = 30


# ---------- path discovery --------------------------------------------------

def _discover_cache_path() -> Path:
    """Find the personal_automation mount, whichever session owns it today.

    Precedence:
      1. $SYNC_CACHE_PATH (explicit override)
      2. Any /sessions/*/mnt/personal_automation/ that exists on disk
      3. Sibling of this script (development fallback)
    """
    env = os.environ.get("SYNC_CACHE_PATH")
    if env:
        return Path(env)
    sessions = Path("/sessions")
    try:
        if sessions.exists():
            for s in sorted(sessions.iterdir()):
                p = s / "mnt" / "personal_automation"
                try:
                    if p.exists():
                        return p / ".sync_cache.json"
                except (PermissionError, OSError):
                    continue
    except (PermissionError, OSError):
        pass
    return Path(__file__).resolve().parent / ".sync_cache.json"


CACHE_PATH = _discover_cache_path()
_OVERRIDES_PATH = CACHE_PATH.parent / "tc_mapping_overrides.json"
_PENDING_PATH   = CACHE_PATH.parent / "pending_approvals.json"


def _normalise_tc_title(title: str) -> str:
    """Normalise a TC title to the canonical key used in overrides/pending.
    Mirrors ExerciseResolver._normalize's leading lowercasing/cleanup so the
    keys match — light version, the resolver does heavier synonym work but
    the override key only needs to be stable, not canonical-perfect."""
    import re as _re
    s = (title or "").lower().strip()
    s = _re.sub(r"\s*\(([^)]*)\)", r" \1", s)
    s = _re.sub(r"[^a-z0-9 ]+", " ", s)
    s = _re.sub(r"\s+", " ", s).strip()
    return s


def _read_json_or_default(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def add_override(tc_title: str, template_id: str, resolved_title: str,
                 notes_prefix: str | None = None,
                 per_hand: bool = False) -> dict:
    """Append/update an approved mapping. Removes any pending entry for it.

    `per_hand` marks a mapping whose Hevy template loads two implements
    (dumbbells/kettlebells) even though the TC title is neutral — e.g.
    "Step Up" → "Dumbbell Step Up". TC plans are written per hand and Hevy
    stores the two-hand total, so the reverse push doubles the working-set
    weights. See `truecoach_to_hevy._apply_per_hand_override`.
    """
    key = _normalise_tc_title(tc_title)
    overrides = _read_json_or_default(_OVERRIDES_PATH, {})
    overrides[key] = {
        "exercise_template_id": template_id,
        "resolved_title": resolved_title,
        "notes_prefix": notes_prefix,
        "per_hand": bool(per_hand),
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "tc_title_seen": tc_title,
    }
    _OVERRIDES_PATH.write_text(json.dumps(overrides, indent=2, sort_keys=True))
    # Drop any matching pending entry now that it's approved.
    pending = _read_json_or_default(_PENDING_PATH, {})
    if key in pending:
        del pending[key]
        _PENDING_PATH.write_text(json.dumps(pending, indent=2, sort_keys=True))
    return overrides[key]


def record_pending_approval(tc_title: str, best_guess: dict,
                            alternatives: list) -> None:
    """Upsert the pending-approvals file. Keyed by normalised TC title."""
    key = _normalise_tc_title(tc_title)
    pending = _read_json_or_default(_PENDING_PATH, {})
    now = datetime.now(timezone.utc).isoformat()
    entry = pending.get(key, {"first_seen": now})
    entry["last_seen"] = now
    entry["tc_title_seen"] = tc_title
    entry["best_guess"] = best_guess
    entry["alternatives"] = alternatives
    pending[key] = entry
    _PENDING_PATH.write_text(json.dumps(pending, indent=2, sort_keys=True))


# ---------- cache -----------------------------------------------------------

_EMPTY_CACHE = {
    "forward": {},
    "reverse": {},
    "day_routines": {},
    "stage1": {},
    "last_run_at": None,
    # Coach-feedback ingestion (see feedback_tips.py for the data layer).
    "feedback_pending": [],         # [{tc_workout_id, date, added_at}]
    "feedback_processed": {},       # {tc_workout_id: {processed_at, note_hash}}
    "form_tips": {},                # {normalised_tc_title: {tip, captured_at, source_tc_id}}
    "feedback_bootstrap_done": False,
}

DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday", "Sunday")
HEVY_FOLDER_ID = 2355979  # Mark's coaching folder


def _normalise(cache: dict) -> dict:
    """Ensure required top-level keys exist."""
    for k, default in _EMPTY_CACHE.items():
        if k not in cache:
            cache[k] = json.loads(json.dumps(default))  # deep-ish copy
    return cache


def load_cache() -> dict:
    if not CACHE_PATH.exists():
        return json.loads(json.dumps(_EMPTY_CACHE))
    try:
        return _normalise(json.loads(CACHE_PATH.read_text()))
    except Exception:
        CACHE_PATH.rename(CACHE_PATH.with_suffix(".corrupt.json"))
        return json.loads(json.dumps(_EMPTY_CACHE))


def save_cache(c: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(c, indent=2, sort_keys=True))


# ---------- hashing ---------------------------------------------------------

def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def sha(obj: Any) -> str:
    return hashlib.sha256(_canon(obj).encode()).hexdigest()


# ---------- post-PUT verification ------------------------------------------

def validate_put_response(payload_body: dict, response_obj: dict) -> dict:
    """Compare a routine PUT (or POST) request body against Hevy's response
    so the runner can detect silent exercise drops.

    Hevy occasionally accepts a routine write with HTTP 200 but quietly
    omits exercises from what it stores — historically observed when an
    exercise's `sets` array is empty. The response body always echoes the
    saved routine, so we can compare by position and template id.

    Args:
        payload_body: the request body we sent. Either the inner routine
            object ({"title", "exercises", ...}) or the wrapped form
            ({"routine": {...}}). Both shapes accepted for convenience.
        response_obj: the parsed JSON response. Hevy returns either
            {"routine": [<routine>]} (PUT) or {"routine": <routine>}
            depending on the endpoint; both shapes accepted.

    Returns:
        {
          "ok":               True iff every payload exercise is present,
          "payload_count":    int,
          "response_count":   int,
          "dropped":          [{"index": i, "template": tid,
                                "title": <tc title or None>}, ...],
          "extra":            [{"index": i, "template": tid}, ...],
        }

        `dropped` lists exercises in the request that don't appear in the
        response. `extra` lists exercises in the response with no
        counterpart in the request (rare; surfaces if Hevy auto-adds or
        reorders unexpectedly).
    """
    body = payload_body.get("routine", payload_body) if isinstance(payload_body, dict) else {}
    resp = response_obj.get("routine") if isinstance(response_obj, dict) else None
    if isinstance(resp, list):
        resp_routine = resp[0] if resp else {}
    else:
        resp_routine = resp or {}

    sent = list(body.get("exercises") or [])
    got = list(resp_routine.get("exercises") or [])

    got_templates = [e.get("exercise_template_id") for e in got]
    got_counts: dict = {}
    for tid in got_templates:
        got_counts[tid] = got_counts.get(tid, 0) + 1

    dropped = []
    for i, ex in enumerate(sent):
        tid = ex.get("exercise_template_id")
        if got_counts.get(tid, 0) > 0:
            got_counts[tid] -= 1
        else:
            dropped.append({
                "index": i,
                "template": tid,
                # tc_title is a preview-only field; if the runner stripped
                # it before sending, fall back to None.
                "title": ex.get("tc_title") or ex.get("title"),
            })

    sent_counts: dict = {}
    for ex in sent:
        tid = ex.get("exercise_template_id")
        sent_counts[tid] = sent_counts.get(tid, 0) + 1
    extra = []
    for i, ex in enumerate(got):
        tid = ex.get("exercise_template_id")
        if sent_counts.get(tid, 0) > 0:
            sent_counts[tid] -= 1
        else:
            extra.append({"index": i, "template": tid})

    return {
        "ok": not dropped and not extra,
        "payload_count": len(sent),
        "response_count": len(got),
        "dropped": dropped,
        "extra": extra,
    }


def fingerprint_tc_list(pairs) -> str:
    """Hash a list of (tc_id, date) tuples — order-insensitive."""
    normalised = sorted((str(a), str(b)) for a, b in pairs)
    return sha(normalised)


def tc_content_hash(cache: dict, tc_workout: dict, now=None) -> str:
    """Content hash for a TC workout, folding in the active form-tip
    signature for its exercises.

    Folding tips into the hash makes the planner emit a reverse item when
    a tip changes (new tip, expiry, or clear), even if the TC plan text
    itself is unchanged. Without this the planner short-circuits and a
    fresh tip never reaches Hevy until the coach edits the plan.

    `now` should be a tz-aware datetime; defaults to UTC now via
    feedback_tips' TTL helpers. Lazy import to avoid circular load.
    """
    from feedback_tips import tips_signature
    rc = tc_workout.get("raw_content") or {}
    titles = [ex.get("title", "")
              for ex in (rc.get("exercises") or [])]
    return sha({
        "content": rc,
        "tips": tips_signature(cache, titles, now=now),
    })


# ---------- stage 0 ---------------------------------------------------------

def cooldown_active(cache: dict, minutes: int = COOLDOWN_MINUTES) -> bool:
    """True if the last successful run was within the cooldown window."""
    last = cache.get("last_run_at")
    if not last:
        return False
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return False
    now = datetime.now(timezone.utc)
    return (now - last_dt).total_seconds() < minutes * 60


# ---------- planning --------------------------------------------------------

def _parse_iso_date(s: str):
    from datetime import date as _date
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def select_active_week(
    tc_upcoming: dict,
    today=None,
    completed_dates=None,
    completed_tc_ids=None,
) -> dict:
    """Choose the active sync window (current calendar week, or next).

    Returns {"start": date, "end": date, "promoted": bool, "workouts": [...]}.

    Promotion fires when every TC workout in the current week is already
    "handled" — either:
      - dated strictly before today (i.e. in the past), OR
      - logged in Hevy already (its date appears in ``completed_dates``,
        sourced from ``hevy_snapshot``), OR
      - forward-synced previously (its tc_id appears in ``completed_tc_ids``,
        sourced from ``cache.forward``).
    Vacuously true if the current week has zero TC workouts. The "completed"
    arms let us promote on a Wednesday afternoon once Wednesday's session is
    done, instead of waiting for Sunday — and populate Hevy with next week's
    routines immediately rather than leaving the folder full of tombstones.
    """
    from datetime import timedelta
    if today is None:
        today = datetime.now(timezone.utc).date()
    completed_dates = set(completed_dates or [])
    completed_tc_ids = set(completed_tc_ids or [])

    # Monday of current calendar week
    cur_start = today - timedelta(days=today.weekday())
    cur_end = cur_start + timedelta(days=6)

    workouts = tc_upcoming.get("workouts") or []

    def in_range(tc, lo, hi):
        d = _parse_iso_date(tc.get("date"))
        return d is not None and lo <= d <= hi

    cur_week = [tc for tc in workouts if in_range(tc, cur_start, cur_end)]

    def already_handled(tc):
        d = _parse_iso_date(tc.get("date"))
        if d is None:
            return False
        if d < today:
            return True
        if tc.get("date") in completed_dates:
            return True
        if tc.get("tc_id") in completed_tc_ids:
            return True
        return False

    # Promote when every current-week workout is already handled
    # (vacuously true when the current week has none).
    needs_promotion = (
        all(already_handled(tc) for tc in cur_week) if cur_week else True
    )

    if not needs_promotion:
        return {"start": cur_start, "end": cur_end,
                "promoted": False, "workouts": cur_week}

    next_start = cur_start + timedelta(days=7)
    next_end = next_start + timedelta(days=6)
    next_week = [tc for tc in workouts if in_range(tc, next_start, next_end)]
    return {"start": next_start, "end": next_end,
            "promoted": True, "workouts": next_week}


def plan(
    cache: dict,
    hevy_snapshot: dict,
    tc_upcoming: dict,
    tc_recent: dict,
    hevy_folder: dict | None = None,
    today=None,
) -> dict:
    """Build a plan for this run.

    Forward scope: at most the first workout in hevy_snapshot (most recent).
    Forward "no empty TC slot on date" → forward_auto_synced (not an error).
    Reverse: skip when tc_content_hash matches cache, else emit drill item.
    `tc_content_hash` folds in the active form-tip signature for the
    workout's exercises, so a tip change (new tip, expiry, or clear)
    triggers a reverse push even when the TC plan text is unchanged.
    """
    _normalise(cache)

    forward_items: list[dict] = []
    forward_auto_synced: list[dict] = []

    workouts = hevy_snapshot.get("workouts") or []
    if workouts:
        w = workouts[0]  # most recent only
        wid = w.get("id")
        date = w.get("date")
        if wid and wid not in cache["forward"]:
            slots = tc_recent.get("results_by_date", {}).get(date, [])
            if not slots:
                # No TC workout exists on this date — legitimate auto-sync.
                forward_auto_synced.append({
                    "hevy_workout_id": wid,
                    "hevy_workout_date": date,
                    "tc_workout_id": None,
                    "reason": "no_tc_slot_on_date",
                })
            else:
                # A slot exists. Always push — the runner is append-aware
                # and skips already-Completed exercises, so it's safe even
                # if the slot has prior content (whether from an earlier
                # sync or from Mark logging manually).
                forward_items.append({
                    "hevy_workout_id": wid,
                    "hevy_workout_date": date,
                    "tc_slot_id": slots[0].get("tc_id"),
                    "hevy_raw": w.get("raw"),
                })

    # Reverse — calendar-week model.
    # Days whose workout is already logged in Hevy — their routine is
    # stale and should be tombstoned, not kept. Derived from every
    # workout in hevy_snapshot (the runbook fetches enough recent
    # workouts to cover the active week, not just the most recent).
    completed_dates = {
        w.get("date") for w in (hevy_snapshot.get("workouts") or [])
        if w.get("date")
    }
    # TC slots that have already been forward-synced (any cache.forward entry
    # links a Hevy workout id → tc_workout_id). Used by select_active_week
    # so we can promote to next week mid-week once the current week is done,
    # without waiting for hevy_snapshot to grow.
    completed_tc_ids = {
        e.get("tc_workout_id") for e in cache.get("forward", {}).values()
        if e.get("tc_workout_id")
    }
    active = select_active_week(
        tc_upcoming, today=today,
        completed_dates=completed_dates,
        completed_tc_ids=completed_tc_ids,
    )
    active_workouts = active["workouts"]

    # Group by day_name. On collision: earliest wins; rest are deferred.
    # Days whose date is already completed in Hevy are also deferred —
    # they fall through to stale_days and get tombstoned.
    by_day: dict = {}
    deferred: list = []
    for tc in sorted(active_workouts, key=lambda t: t.get("date", "")):
        day = tc.get("day_name")
        if not day or day not in DAY_NAMES:
            deferred.append({"tc_id": tc.get("tc_id"),
                             "date": tc.get("date"),
                             "day_name": day,
                             "reason": "unrecognised_day_name"})
            continue
        if tc.get("date") in completed_dates:
            deferred.append({"tc_id": tc.get("tc_id"),
                             "date": tc.get("date"),
                             "day_name": day,
                             "reason": "completed_in_hevy"})
            continue
        if day in by_day:
            deferred.append({"tc_id": tc.get("tc_id"),
                             "date": tc.get("date"),
                             "day_name": day,
                             "reason": "same_day_collision"})
        else:
            by_day[day] = tc

    # Anything outside the active week is deferred too.
    active_ids = {tc.get("tc_id") for tc in active_workouts}
    for tc in (tc_upcoming.get("workouts") or []):
        if tc.get("tc_id") in active_ids:
            continue
        deferred.append({"tc_id": tc.get("tc_id"),
                         "date": tc.get("date"),
                         "day_name": tc.get("day_name"),
                         "reason": "outside_active_week"})

    folder_routines = (hevy_folder or {}).get("routines") or []
    # Bare-day-name routines currently in the folder, keyed by day name.
    in_hevy_day_ids: dict = {}
    for r in folder_routines:
        title = r.get("title") or ""
        rid = r.get("id")
        if rid and title in DAY_NAMES:
            in_hevy_day_ids[title] = rid

    needed_days = set(by_day.keys())
    hevy_day_set = set(in_hevy_day_ids.keys())
    keep_days  = needed_days & hevy_day_set
    new_days   = needed_days - hevy_day_set
    stale_days = hevy_day_set - needed_days

    # Pair new days with available routine slots. Slot pool, in priority:
    #   1. Stale day-named slots (this run's "no-longer-needed" days).
    #   2. Existing tombstones (title "_") still in the folder from past
    #      runs — recycle them instead of leaving as clutter.
    # Pairing reuses the existing routine_id and rewrites title + content
    # via PUT; saves a POST and avoids piling up tombstones.
    nd_sorted = sorted(new_days, key=lambda d: DAY_NAMES.index(d))
    slot_pool = [
        {"former_label": d, "routine_id": in_hevy_day_ids[d], "kind": "day"}
        for d in sorted(stale_days, key=lambda d: DAY_NAMES.index(d))
    ] + [
        {"former_label": None, "routine_id": r["id"], "kind": "tombstone"}
        for r in folder_routines
        if (r.get("title") == "_" and r.get("id"))
    ]
    repurpose: dict = {}  # new_day -> slot dict
    for nd in nd_sorted:
        if not slot_pool:
            break
        repurpose[nd] = slot_pool.pop(0)
    leftover_new = [d for d in nd_sorted if d not in repurpose]
    # Stale day-name slots that weren't reused need tombstoning.
    # Unpaired tombstone slots stay as-is (already tombstoned).
    leftover_stale = [s["former_label"] for s in slot_pool
                      if s["kind"] == "day"]

    # `today` is a calendar date; promote to a tz-aware datetime at noon
    # for the form-tip TTL check (mid-day is unambiguous wrt day boundaries).
    if today is not None:
        from datetime import datetime as _dt, time as _time, timezone as _tz
        sig_now = _dt.combine(today, _time(12, 0), tzinfo=_tz.utc)
    else:
        sig_now = None

    reverse_items: list[dict] = []
    for day, tc in by_day.items():
        h = tc_content_hash(cache, tc, now=sig_now)
        if day in keep_days:
            existing_id = in_hevy_day_ids[day]
            cached = cache["reverse"].get(day, {})
            if cached.get("tc_content_hash") == h:
                continue
            reverse_items.append({
                "day_name": day,
                "tc_workout_id": tc.get("tc_id"),
                "date": tc.get("date"),
                "tc_content_hash": h,
                "raw_content": tc.get("raw_content"),
                "prev_payload_hash": cached.get("payload_hash"),
                "existing_routine_id": existing_id,
                "repurposed_from": None,
            })
        elif day in repurpose:
            slot = repurpose[day]
            reverse_items.append({
                "day_name": day,
                "tc_workout_id": tc.get("tc_id"),
                "date": tc.get("date"),
                "tc_content_hash": h,
                "raw_content": tc.get("raw_content"),
                # Repurposed slot — old hash isn't comparable, force PUT.
                "prev_payload_hash": None,
                "existing_routine_id": slot["routine_id"],
                "repurposed_from": slot["former_label"],  # None if from a tombstone
            })
        elif day in leftover_new:
            reverse_items.append({
                "day_name": day,
                "tc_workout_id": tc.get("tc_id"),
                "date": tc.get("date"),
                "tc_content_hash": h,
                "raw_content": tc.get("raw_content"),
                "prev_payload_hash": None,
                "existing_routine_id": None,  # POST a fresh routine
                "repurposed_from": None,
            })

    # Reverse tombstones: Hevy's public API doesn't support DELETE on
    # /v1/routines/{id} (only GET, HEAD, PUT — confirmed via OPTIONS) and
    # PUT rejects folder_id, so we can't delete or move routines out of
    # the folder. Instead we PUT a "_" title + placeholder body so they
    # stay in the folder but are visually distinct and inert. Mark
    # deletes them in the Hevy UI when convenient. Only stale day-named
    # slots that weren't repurposed land here (along with legacy extras).
    import re as _re
    legacy_extra_re = _re.compile(
        r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
        r"\s+\d{4}-\d{2}-\d{2}$"
    )
    reverse_tombstones: list[dict] = []
    # Stale bare-day slots that didn't get repurposed (in weekday order).
    for day in leftover_stale:
        reverse_tombstones.append({
            "routine_id": in_hevy_day_ids[day],
            "former_title": day,
            "day_name": day,
        })
    # Plus legacy "<Day> YYYY-MM-DD" extras.
    for r in folder_routines:
        title = r.get("title", "") or ""
        rid = r.get("id")
        if not rid:
            continue
        if title == "_":
            continue
        if title in DAY_NAMES:
            continue  # handled above (kept, repurposed, or already in tombstones)
        if legacy_extra_re.match(title):
            reverse_tombstones.append({
                "routine_id": rid,
                "former_title": title,
                "day_name": None,
            })

    return {
        "forward": forward_items,
        "forward_auto_synced": forward_auto_synced,
        "reverse": reverse_items,
        "reverse_tombstones": reverse_tombstones,
        "deferred": deferred,
        "active_week": {
            "start": active["start"].isoformat(),
            "end": active["end"].isoformat(),
            "promoted": active["promoted"],
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------- commit ----------------------------------------------------------

def commit(cache: dict, results: dict) -> dict:
    """Update cache based on executed results. Errors leave entries alone."""
    _normalise(cache)
    now = datetime.now(timezone.utc).isoformat()

    for r in results.get("forward", []):
        if r.get("status") == "ok":
            cache["forward"][r["hevy_workout_id"]] = {
                "synced_at": now,
                "tc_workout_id": r.get("tc_workout_id"),
                "mode": r.get("mode", "ui"),
            }
            # Queue this TC workout for coach-feedback processing on a
            # later run. Idempotent — feedback_tips.record_pending skips
            # if already pending or processed.
            tc_id = r.get("tc_workout_id")
            if tc_id:
                try:
                    from feedback_tips import record_pending
                    record_pending(cache, tc_id, r.get("date") or "")
                except Exception:
                    # Never fail commit() over a feedback-queue hiccup.
                    pass

    for r in results.get("forward_auto_synced", []):
        cache["forward"][r["hevy_workout_id"]] = {
            "synced_at": now,
            "tc_workout_id": r.get("tc_workout_id"),
            "mode": "auto",
            "reason": r.get("reason"),
        }

    for r in results.get("reverse", []):
        status = r.get("status")
        if status in ("ok", "skipped"):
            day = r.get("day_name")
            if not day:
                continue
            # Slot repurposing: if this routine was renamed from another
            # day, drop the old day's cache entries before writing the
            # new ones. (Same routine_id, new day_name.)
            old_day = r.get("repurposed_from")
            if old_day and old_day != day:
                cache["day_routines"].pop(old_day, None)
                cache["reverse"].pop(old_day, None)
            entry = cache["reverse"].get(day, {})
            entry["tc_content_hash"] = r.get(
                "tc_content_hash", entry.get("tc_content_hash")
            )
            if status == "ok":
                entry["payload_hash"] = r.get(
                    "payload_hash", entry.get("payload_hash")
                )
                entry["last_pushed_at"] = now
            cache["reverse"][day] = entry
            # Capture the routine_id used (whether from PUT existing or
            # POST new). Runner echoes routine_id in every ok result.
            rid = r.get("routine_id")
            if rid:
                cache["day_routines"][day] = rid

    for r in results.get("reverse_tombstones", []):
        if r.get("status") == "ok":
            day = r.get("day_name")
            if day:
                cache["day_routines"].pop(day, None)
                cache["reverse"].pop(day, None)

    # Stage 1 fingerprints are committed via separate update_stage1 helper,
    # not through results.
    cache["last_run_at"] = now
    return cache


def update_stage1(
    cache: dict,
    *,
    tc_upcoming_fingerprint: str | None = None,
    last_hevy_workout_id: str | None = None,
) -> dict:
    """Record stage-1 probe fingerprints. Called by the prompt after probing."""
    _normalise(cache)
    if tc_upcoming_fingerprint is not None:
        cache["stage1"]["tc_upcoming_fingerprint"] = tc_upcoming_fingerprint
    if last_hevy_workout_id is not None:
        cache["stage1"]["last_hevy_workout_id"] = last_hevy_workout_id
    return cache


# ---------- cli -------------------------------------------------------------

def _read_json(path: str) -> dict:
    return json.loads(Path(path).read_text())


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan")
    p_plan.add_argument("--hevy", required=True)
    p_plan.add_argument("--tc-upcoming", required=True)
    p_plan.add_argument("--tc-recent", required=True)
    p_plan.add_argument("--hevy-folder", required=False, default=None,
                        help="path to hevy_folder.json with the Mark folder's "
                             "current routine list")
    p_plan.add_argument("--out", required=True)

    p_commit = sub.add_parser("commit")
    p_commit.add_argument("--results", required=True)

    p_stage0 = sub.add_parser("stage0")
    p_stage0.add_argument("--cooldown-minutes", type=int,
                          default=COOLDOWN_MINUTES)

    p_stage1 = sub.add_parser("stage1")
    p_stage1.add_argument("--tc-fingerprint")
    p_stage1.add_argument("--hevy-workout-id")

    p_fp = sub.add_parser("fingerprint-tc-list")
    p_fp.add_argument("--pairs", required=True,
                      help="JSON array of [tc_id, date] pairs")

    p_sha = sub.add_parser("sha")
    p_sha.add_argument("--file", required=True)

    p_val = sub.add_parser(
        "validate-put",
        help="Compare a PUT request body to the response — detect "
             "silent exercise drops. Exits 0 if ok, 11 if drops found "
             "(suitable for shell-script branching).",
    )
    p_val.add_argument("--payload", required=True,
                       help="JSON file containing the PUT request body "
                            "(wrapped {\"routine\":{...}} or bare).")
    p_val.add_argument("--response", required=True,
                       help="JSON file containing Hevy's PUT response.")

    p_ov = sub.add_parser("add-override",
                          help="Approve a TC title → Hevy template mapping")
    p_ov.add_argument("--tc-title", required=True)
    p_ov.add_argument("--template-id", required=True)
    p_ov.add_argument("--resolved-title", required=True)
    p_ov.add_argument("--notes-prefix", default=None)
    p_ov.add_argument("--per-hand", action="store_true",
                      help="Hevy template loads two implements while the TC "
                           "title is neutral (e.g. Step Up → Dumbbell Step "
                           "Up). Doubles per-hand plan weights on the way "
                           "into Hevy.")

    p_pa = sub.add_parser("record-pending",
                          help="Append/update a pending-approval entry")
    p_pa.add_argument("--tc-title", required=True)
    p_pa.add_argument("--best-guess-id", required=True)
    p_pa.add_argument("--best-guess-title", required=True)
    p_pa.add_argument("--best-guess-conf", required=True)
    p_pa.add_argument("--alternatives", default="[]",
                      help="JSON array of {id,title,conf,source?}")

    sub.add_parser("show")

    args = ap.parse_args(argv)
    cache = load_cache()

    if args.cmd == "plan":
        hevy_folder = (
            _read_json(args.hevy_folder) if args.hevy_folder else None
        )
        out = plan(
            cache,
            _read_json(args.hevy),
            _read_json(args.tc_upcoming),
            _read_json(args.tc_recent),
            hevy_folder=hevy_folder,
        )
        Path(args.out).write_text(json.dumps(out, indent=2))
        aw = out.get("active_week", {})
        print(
            f"plan: active_week={aw.get('start')}–{aw.get('end')}"
            f"{' (promoted)' if aw.get('promoted') else ''} | "
            f"forward={len(out['forward'])} "
            f"auto_synced={len(out['forward_auto_synced'])} "
            f"reverse={len(out['reverse'])} "
            f"tombstones={len(out.get('reverse_tombstones') or [])} "
            f"deferred={len(out.get('deferred') or [])}"
        )
        return 0

    if args.cmd == "commit":
        cache = commit(cache, _read_json(args.results))
        save_cache(cache)
        print("cache updated")
        return 0

    if args.cmd == "stage0":
        skip = cooldown_active(cache, args.cooldown_minutes)
        print(json.dumps({
            "should_run": not skip,
            "last_run_at": cache.get("last_run_at"),
            "cooldown_minutes": args.cooldown_minutes,
            "cache_path": str(CACHE_PATH),
        }))
        return 0 if not skip else 10  # exit 10 = skip

    if args.cmd == "stage1":
        update_stage1(
            cache,
            tc_upcoming_fingerprint=args.tc_fingerprint,
            last_hevy_workout_id=args.hevy_workout_id,
        )
        save_cache(cache)
        print("stage1 fingerprints updated")
        return 0

    if args.cmd == "fingerprint-tc-list":
        pairs = json.loads(args.pairs)
        print(fingerprint_tc_list(pairs))
        return 0

    if args.cmd == "sha":
        obj = _read_json(args.file)
        print(sha(obj))
        return 0

    if args.cmd == "validate-put":
        report = validate_put_response(
            _read_json(args.payload), _read_json(args.response),
        )
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 11

    if args.cmd == "add-override":
        entry = add_override(
            args.tc_title, args.template_id, args.resolved_title,
            notes_prefix=args.notes_prefix,
            per_hand=args.per_hand,
        )
        print(json.dumps(entry, indent=2))
        return 0

    if args.cmd == "record-pending":
        record_pending_approval(
            args.tc_title,
            best_guess={"id": args.best_guess_id,
                        "title": args.best_guess_title,
                        "conf": args.best_guess_conf},
            alternatives=json.loads(args.alternatives),
        )
        print("pending approval recorded")
        return 0

    if args.cmd == "show":
        print(json.dumps(cache, indent=2, sort_keys=True))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
