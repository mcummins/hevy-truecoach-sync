"""Coach-feedback ingestion for the Hevy↔TrueCoach sync.

Cillian leaves freeform notes on each TC workout he reviews. A note typically
mixes encouragement and form tips, e.g.:

    Nice gym!
    Bench - Technique getting better here overall. Keep thinking about
            tucking the elbows. Reps 3 and 4 were the hardest because
            your elbows flared out.
    Squat - These are still looking great. Your strength and technique
            has come on a long way here.
    Chins - That's all good. Getting to 10kg is great!

We want the form-tip portion to surface in Hevy alongside the next routine,
so Mark sees it while training. Encouragement is dropped.

This module is the pure data layer: load/save state, decide whether a tip
is still active (≤30 days old), apply a runner-classified note to the
form-tips map. The runner (Claude in the scheduled task) handles the
actual reading and classification of free-form note text.

Cache schema (extends `.sync_cache.json`)::

    {
      ...,
      "feedback_pending": [
        {"tc_workout_id": "...", "date": "YYYY-MM-DD", "added_at": "ISO"}
      ],
      "feedback_processed": {
        "<tc_workout_id>": {"processed_at": "ISO",
                            "exercises_seen": ["Bench Press", "Squat", ...]}
      },
      "form_tips": {
        "<normalised canonical tc title>": {
            "tip": "Keep thinking about tucking the elbows...",
            "captured_at": "ISO",
            "source_tc_id": "..."
        }
      },
      "feedback_bootstrap_done": bool
    }

Notes are immutable once Cillian posts them — we don't re-process a
workout we've already seen.

Tip lifetime: 30 days from `captured_at`, or until a newer note covers the
same exercise — whichever comes first. If a processed note covers an
exercise and gives no form-tip (only encouragement or no mention), the
slot is CLEARED — Cillian's silence is read as "resolved".
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional


TIP_TTL_DAYS = 30
FORM_TIP_PREFIX = "Form Tip: "
FORM_TIP_SEPARATOR = "---"
BOOTSTRAP_WORKOUT_COUNT = 25


# ---------------------------------------------------------------------------
# Normalisation — keys must match _normalise_tc_title in bidirectional_sync
# ---------------------------------------------------------------------------

_PAREN_RE = re.compile(r"\s*\(([^)]*)\)")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_MULTISPACE = re.compile(r"\s+")


def normalise_title(title: str) -> str:
    """Canonical key for form_tips — lowercased, paren-flattened, alnum-only.

    Mirrors bidirectional_sync._normalise_tc_title so the same key is used
    for overrides and tips.
    """
    s = (title or "").lower().strip()
    s = _PAREN_RE.sub(r" \1", s)
    s = _NON_ALNUM.sub(" ", s)
    s = _MULTISPACE.sub(" ", s).strip()
    return s


# ---------------------------------------------------------------------------
# Cache slot accessors — operate on a dict (the loaded .sync_cache.json)
# ---------------------------------------------------------------------------

def _ensure_slots(cache: dict) -> dict:
    cache.setdefault("feedback_pending", [])
    cache.setdefault("feedback_processed", {})
    cache.setdefault("form_tips", {})
    cache.setdefault("feedback_bootstrap_done", False)
    return cache


def record_pending(cache: dict, tc_workout_id: str, date: str,
                   now: Optional[datetime] = None) -> None:
    """Mark a TC workout as awaiting feedback processing. Idempotent."""
    _ensure_slots(cache)
    if any(e.get("tc_workout_id") == tc_workout_id
           for e in cache["feedback_pending"]):
        return
    # Don't re-queue something we already processed (avoid loops if the
    # forward sync runs twice for the same workout).
    if tc_workout_id in cache["feedback_processed"]:
        return
    now = now or datetime.now(timezone.utc)
    cache["feedback_pending"].append({
        "tc_workout_id": tc_workout_id,
        "date": date,
        "added_at": now.isoformat(),
    })


def list_pending(cache: dict) -> list[dict]:
    """All TC workouts queued for feedback processing, oldest first."""
    _ensure_slots(cache)
    return sorted(
        cache["feedback_pending"],
        key=lambda e: e.get("added_at", ""),
    )


def is_already_processed(cache: dict, tc_workout_id: str) -> bool:
    """True if we've already processed this workout. Notes are immutable
    so the presence of the workout id is a sufficient check."""
    _ensure_slots(cache)
    return tc_workout_id in cache["feedback_processed"]


def apply_note(
    cache: dict,
    tc_workout_id: str,
    exercises: Iterable[str],
    classifications: dict[str, Optional[str]],
    now: Optional[datetime] = None,
) -> dict:
    """Update form_tips from a classified note.

    Rules (additive only — silence never clears):

      * Truthy classification ⇒ SET / REPLACE the slot. A newer form tip
        from Cillian overrides any prior tip on the same exercise.
      * Null / empty / whitespace / missing classification ⇒ leave the
        prior tip in place. Encouragement, an "all good" comment, or no
        mention at all are all read as "the prior tip isn't refreshed
        but also isn't refuted". The tip lives out its 30-day TTL
        (`prune_expired`) or is replaced by a future note.

    This is intentionally conservative: Cillian sometimes reviews
    workouts several days after they're logged, so a workout with no
    coach note yet should be left in `feedback_pending` (the runner is
    responsible for that decision — it shouldn't call apply_note at all
    for a workout that has no note). Even when this function IS called
    on a real note, an absent mention for a given exercise is treated
    as "no new information", not "resolved".

    Args:
        tc_workout_id: TC workout the note belongs to.
        exercises: canonical TC exercise titles in the workout's display
                   order. Recorded under feedback_processed so we don't
                   re-process the same workout, but no longer used to
                   gate clearing.
        classifications: { canonical_title: tip_string_or_null }, one
                   entry per exercise. Only truthy values mutate state.
        now: timezone-aware datetime; defaults to UTC now.

    Returns a small summary dict {set: [...], cleared: []}. The
    ``cleared`` list is retained for return-shape compatibility but is
    always empty under the additive-only rule.
    """
    _ensure_slots(cache)
    now = now or datetime.now(timezone.utc)
    exercises = list(exercises)
    set_keys: list[str] = []

    for ex in exercises:
        key = normalise_title(ex)
        if not key:
            continue
        raw_tip = classifications.get(ex)
        tip = (raw_tip or "").strip() if isinstance(raw_tip, str) else ""
        if not tip:
            # No form tip for this exercise in this note — leave any
            # prior tip in place. It will expire on its own (30 days)
            # or be replaced by a later note.
            continue
        cache["form_tips"][key] = {
            "tip": tip,
            "captured_at": now.isoformat(),
            "source_tc_id": tc_workout_id,
            "tc_title": ex,
        }
        set_keys.append(key)

    cache["feedback_processed"][tc_workout_id] = {
        "processed_at": now.isoformat(),
        "exercises_seen": list(exercises),
    }
    cache["feedback_pending"] = [
        e for e in cache["feedback_pending"]
        if e.get("tc_workout_id") != tc_workout_id
    ]
    return {"set": set_keys, "cleared": []}


def get_active_tip(cache: dict, tc_title: str,
                   now: Optional[datetime] = None) -> Optional[str]:
    """Return the active form tip for `tc_title`, or None if expired/missing."""
    _ensure_slots(cache)
    key = normalise_title(tc_title)
    entry = cache["form_tips"].get(key)
    if not entry:
        return None
    captured = entry.get("captured_at")
    if not captured:
        return entry.get("tip") or None
    try:
        captured_dt = datetime.fromisoformat(captured)
    except ValueError:
        return entry.get("tip") or None
    now = now or datetime.now(timezone.utc)
    if (now - captured_dt) > timedelta(days=TIP_TTL_DAYS):
        return None
    return entry.get("tip") or None


def tips_signature(cache: dict, tc_titles: Iterable[str],
                   now: Optional[datetime] = None) -> dict:
    """Active form tips for these titles, keyed by canonical normalised title.

    Returns only entries that are currently active (within TTL), so the
    output is safe to fold into a content hash. Used by the planner so
    a tip-only change still triggers a reverse push — without this, the
    planner short-circuits when the TC plan content hasn't changed and a
    fresh tip never reaches Hevy until the coach also tweaks the plan.
    """
    out: dict = {}
    for t in tc_titles:
        tip = get_active_tip(cache, t, now=now)
        if tip:
            out[normalise_title(t)] = tip
    return out


def prune_expired(cache: dict, now: Optional[datetime] = None) -> list[str]:
    """Drop tips older than TIP_TTL_DAYS. Returns the keys removed."""
    _ensure_slots(cache)
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=TIP_TTL_DAYS)
    removed: list[str] = []
    for key in list(cache["form_tips"].keys()):
        entry = cache["form_tips"][key]
        captured = entry.get("captured_at")
        if not captured:
            continue
        try:
            captured_dt = datetime.fromisoformat(captured)
        except ValueError:
            continue
        if captured_dt < cutoff:
            del cache["form_tips"][key]
            removed.append(key)
    return removed


def needs_bootstrap(cache: dict) -> bool:
    _ensure_slots(cache)
    return not cache["feedback_bootstrap_done"]


def mark_bootstrap_done(cache: dict) -> None:
    _ensure_slots(cache)
    cache["feedback_bootstrap_done"] = True


# ---------------------------------------------------------------------------
# Notes-block helper — used by build_hevy_exercise / the runner
# ---------------------------------------------------------------------------

def append_form_tip(notes: str, tip: Optional[str]) -> str:
    """Append a form-tip block after the existing notes, idempotently.

    Strips any prior "---\\nForm Tip: ..." block from `notes` first so
    re-pushes don't accumulate duplicates when the tip changes or clears.
    When the existing notes are empty, the separator is dropped — no
    point in a "---" line above nothing.
    """
    base = _strip_form_tip_block(notes or "")
    if not tip:
        return base
    tip_line = f"{FORM_TIP_PREFIX}{tip.strip()}"
    if not base:
        return tip_line
    return f"{base}\n{FORM_TIP_SEPARATOR}\n{tip_line}"


def _strip_form_tip_block(notes: str) -> str:
    """Remove any trailing form-tip block.

    Handles both shapes that `append_form_tip` can emit:
      * "...prior notes...\\n---\\nForm Tip: <tip>"   (notes were non-empty)
      * "Form Tip: <tip>"                             (notes were empty)
    """
    if FORM_TIP_PREFIX not in notes:
        return notes.rstrip()
    # Try the "with separator" shape first; fall back to a bare form-tip line.
    with_sep = re.compile(
        r"\n?" + re.escape(FORM_TIP_SEPARATOR) + r"\s*\n\s*"
        + re.escape(FORM_TIP_PREFIX) + r".*\Z",
        re.DOTALL,
    )
    stripped = with_sep.sub("", notes)
    if stripped != notes:
        return stripped.rstrip()
    bare = re.compile(
        r"(?:\A|\n)" + re.escape(FORM_TIP_PREFIX) + r".*\Z",
        re.DOTALL,
    )
    return bare.sub("", notes).rstrip()


# ---------------------------------------------------------------------------
# CLI — the scheduled-task runner shells into these
# ---------------------------------------------------------------------------

def _cli(argv: list[str]) -> int:
    import argparse
    from pathlib import Path
    # Reuse the canonical cache path discovery from bidirectional_sync
    from bidirectional_sync import CACHE_PATH, load_cache, save_cache

    ap = argparse.ArgumentParser(prog="feedback_tips")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-pending",
                   help="JSON dump of feedback_pending (oldest first)")
    sub.add_parser("dump-tips",
                   help="JSON dump of form_tips with TTL-active flag")
    sub.add_parser("prune",
                   help="Drop tips older than %d days; print removed keys"
                        % TIP_TTL_DAYS)
    sub.add_parser("bootstrap-status",
                   help="Print whether the last-25 bootstrap has run")
    sub.add_parser("mark-bootstrap-done")

    p_get = sub.add_parser("get-tip",
                           help="Print active tip for a canonical TC title")
    p_get.add_argument("--tc-title", required=True)

    p_record = sub.add_parser("record-pending",
                              help="Queue a TC workout for feedback processing")
    p_record.add_argument("--tc-id", required=True)
    p_record.add_argument("--date", required=True)

    p_apply = sub.add_parser(
        "apply-note",
        help=("Apply a classified note to form_tips. "
              "Reads {tc_id,exercises,classifications} from a JSON file."),
    )
    p_apply.add_argument("--file", required=True,
                         help="Path to JSON: {tc_id, exercises, "
                              "classifications: {title: tip_or_null}}")

    args = ap.parse_args(argv)
    cache = load_cache()
    _ensure_slots(cache)

    if args.cmd == "list-pending":
        print(json.dumps(list_pending(cache), indent=2))
        return 0

    if args.cmd == "dump-tips":
        now = datetime.now(timezone.utc)
        out = {}
        for key, entry in cache["form_tips"].items():
            captured = entry.get("captured_at")
            active = True
            if captured:
                try:
                    age = now - datetime.fromisoformat(captured)
                    active = age <= timedelta(days=TIP_TTL_DAYS)
                except ValueError:
                    pass
            out[key] = {**entry, "active": active}
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0

    if args.cmd == "prune":
        removed = prune_expired(cache)
        save_cache(cache)
        print(json.dumps({"removed": removed}))
        return 0

    if args.cmd == "bootstrap-status":
        print(json.dumps({
            "needs_bootstrap": needs_bootstrap(cache),
            "workout_count": BOOTSTRAP_WORKOUT_COUNT,
        }))
        return 0

    if args.cmd == "mark-bootstrap-done":
        mark_bootstrap_done(cache)
        save_cache(cache)
        print("ok")
        return 0

    if args.cmd == "get-tip":
        tip = get_active_tip(cache, args.tc_title)
        if tip:
            print(tip)
        return 0

    if args.cmd == "record-pending":
        record_pending(cache, args.tc_id, args.date)
        save_cache(cache)
        print("queued")
        return 0

    if args.cmd == "apply-note":
        payload = json.loads(Path(args.file).read_text())
        summary = apply_note(
            cache,
            tc_workout_id=payload["tc_id"],
            exercises=payload.get("exercises", []),
            classifications=payload.get("classifications", {}),
        )
        save_cache(cache)
        print(json.dumps(summary))
        return 0

    return 2


if __name__ == "__main__":
    import sys
    sys.exit(_cli(sys.argv[1:]))
