"""Tests for the coach-feedback ingestion layer.

These tests cover the pure data layer in feedback_tips.py plus the wiring
into bidirectional_sync.commit() and truecoach_to_hevy.build_hevy_exercise.
The runner (Claude in the scheduled task) is responsible for the actual
note classification — these tests use synthetic classifications that
mimic the runner's output.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import feedback_tips as ft
import truecoach_to_hevy as t2h
import bidirectional_sync as bs


# ---------------------------------------------------------------------------
# normalise_title — must agree with bidirectional_sync._normalise_tc_title
# ---------------------------------------------------------------------------

def test_normalise_title_matches_sync_key():
    cases = [
        "Bench Press",
        "Chin-Up",
        "Romanian Deadlift (Barbell)",
        "  Squat  ",
        "ISSA Exercise Library Push-Up",
    ]
    for c in cases:
        assert ft.normalise_title(c) == bs._normalise_tc_title(c), c


# ---------------------------------------------------------------------------
# record_pending / list_pending
# ---------------------------------------------------------------------------

def test_record_pending_idempotent():
    cache: dict = {}
    ft.record_pending(cache, "tc1", "2026-05-11",
                      now=datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc))
    ft.record_pending(cache, "tc1", "2026-05-11",
                      now=datetime(2026, 5, 11, 12, 5, tzinfo=timezone.utc))
    assert len(cache["feedback_pending"]) == 1


def test_record_pending_skips_already_processed():
    cache: dict = {"feedback_processed": {"tc1": {"processed_at": "2026-05-09"}}}
    ft.record_pending(cache, "tc1", "2026-05-09")
    assert cache["feedback_pending"] == []


def test_list_pending_oldest_first():
    cache: dict = {}
    ft.record_pending(cache, "tc-old", "2026-05-09",
                      now=datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc))
    ft.record_pending(cache, "tc-new", "2026-05-11",
                      now=datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc))
    ids = [e["tc_workout_id"] for e in ft.list_pending(cache)]
    assert ids == ["tc-old", "tc-new"]


# ---------------------------------------------------------------------------
# apply_note — SET, CLEAR, and the "missing exercise = clear" rule
# ---------------------------------------------------------------------------

def _make_pending_cache():
    cache: dict = {}
    ft.record_pending(cache, "tc1", "2026-05-11",
                      now=datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc))
    return cache


def test_apply_note_sets_tips_for_form_relevant_exercises():
    cache = _make_pending_cache()
    summary = ft.apply_note(
        cache,
        tc_workout_id="tc1",
        exercises=["Bench Press", "Squat"],
        classifications={
            "Bench Press": "Keep thinking about tucking the elbows.",
            "Squat": None,  # encouragement only
        },
        now=datetime(2026, 5, 11, 13, 0, tzinfo=timezone.utc),
    )
    bench = cache["form_tips"][ft.normalise_title("Bench Press")]
    assert bench["tip"] == "Keep thinking about tucking the elbows."
    assert bench["source_tc_id"] == "tc1"
    # Squat had no prior tip and a null classification → still absent
    # (not because anything was cleared — there was nothing to clear).
    assert ft.normalise_title("Squat") not in cache["form_tips"]
    assert summary["set"] == [ft.normalise_title("Bench Press")]
    assert summary["cleared"] == []


def test_apply_note_preserves_prior_tip_for_unmentioned_exercises():
    """An exercise in the workout that's absent from classifications keeps
    its prior tip. Cillian's silence on an exercise just means he didn't
    refresh the tip — it lives out its 30-day TTL or waits for a newer
    tip to replace it."""
    cache = _make_pending_cache()
    cache["form_tips"][ft.normalise_title("Chin-Up")] = {
        "tip": "Old tip from last week",
        "captured_at": "2026-05-04T12:00:00+00:00",
        "source_tc_id": "tc-old",
        "tc_title": "Chin-Up",
    }
    summary = ft.apply_note(
        cache,
        tc_workout_id="tc1",
        exercises=["Bench Press", "Chin-Up"],
        classifications={"Bench Press": "Tuck the elbows."},
        # No "Chin-Up" key — prior tip must survive.
    )
    chin = cache["form_tips"][ft.normalise_title("Chin-Up")]
    assert chin["tip"] == "Old tip from last week"
    assert chin["source_tc_id"] == "tc-old"
    assert summary["cleared"] == []


def test_apply_note_preserves_prior_tip_when_classification_is_null():
    """An exercise classified as null (encouragement / no form tip) keeps
    its prior tip — additive-only rule."""
    cache = _make_pending_cache()
    cache["form_tips"][ft.normalise_title("Squat")] = {
        "tip": "Drive through your heels",
        "captured_at": "2026-05-04T12:00:00+00:00",
        "source_tc_id": "tc-old",
        "tc_title": "Squat",
    }
    ft.apply_note(
        cache,
        tc_workout_id="tc1",
        exercises=["Squat"],
        classifications={"Squat": None},
    )
    sq = cache["form_tips"][ft.normalise_title("Squat")]
    assert sq["tip"] == "Drive through your heels"
    assert sq["source_tc_id"] == "tc-old"


def test_apply_note_overwrites_previous_tip():
    cache = _make_pending_cache()
    cache["form_tips"][ft.normalise_title("Bench Press")] = {
        "tip": "Old guidance",
        "captured_at": "2026-05-04T12:00:00+00:00",
        "source_tc_id": "tc-old",
        "tc_title": "Bench Press",
    }
    ft.apply_note(
        cache,
        tc_workout_id="tc1",
        exercises=["Bench Press"],
        classifications={"Bench Press": "New guidance — tuck the elbows."},
        now=datetime(2026, 5, 11, 13, 0, tzinfo=timezone.utc),
    )
    entry = cache["form_tips"][ft.normalise_title("Bench Press")]
    assert entry["tip"] == "New guidance — tuck the elbows."
    assert entry["source_tc_id"] == "tc1"


def test_apply_note_moves_pending_to_processed():
    cache = _make_pending_cache()
    ft.apply_note(
        cache,
        tc_workout_id="tc1",
        exercises=["Bench Press"],
        classifications={"Bench Press": None},
        now=datetime(2026, 5, 11, 13, 0, tzinfo=timezone.utc),
    )
    assert cache["feedback_pending"] == []
    assert "tc1" in cache["feedback_processed"]
    assert cache["feedback_processed"]["tc1"]["exercises_seen"] == ["Bench Press"]


def test_apply_note_no_note_keeps_pending():
    """note_present=False must NOT drop the workout — Cillian comments
    late, so it stays queued for a later run and records nothing."""
    cache = _make_pending_cache()
    summary = ft.apply_note(
        cache,
        tc_workout_id="tc1",
        exercises=["Bench Press"],
        classifications={},
        note_present=False,
        now=datetime(2026, 5, 11, 13, 0, tzinfo=timezone.utc),
    )
    # Still pending, nothing processed, no tips set.
    assert [e["tc_workout_id"] for e in cache["feedback_pending"]] == ["tc1"]
    assert "tc1" not in cache["feedback_processed"]
    assert cache["form_tips"] == {}
    assert summary["set"] == []
    assert summary.get("kept_pending") is True


def test_apply_note_present_with_only_encouragement_clears_pending():
    """A real note that's pure encouragement (all null) is still a
    *reviewed* workout — it leaves the pending queue."""
    cache = _make_pending_cache()
    ft.apply_note(
        cache,
        tc_workout_id="tc1",
        exercises=["Bench Press"],
        classifications={"Bench Press": None},
        note_present=True,
        now=datetime(2026, 5, 11, 13, 0, tzinfo=timezone.utc),
    )
    assert cache["feedback_pending"] == []
    assert "tc1" in cache["feedback_processed"]


def test_prune_stale_pending_drops_old_keeps_fresh():
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    cache: dict = {}
    # 16 days old → dropped; 5 days old → kept.
    ft.record_pending(cache, "tc-stale", "2026-05-04",
                      now=now - timedelta(days=16))
    ft.record_pending(cache, "tc-fresh", "2026-05-15",
                      now=now - timedelta(days=5))
    dropped = ft.prune_stale_pending(cache, now=now)
    assert dropped == ["tc-stale"]
    pending_ids = [e["tc_workout_id"] for e in cache["feedback_pending"]]
    assert pending_ids == ["tc-fresh"]
    # The given-up workout is recorded so it isn't re-queued/re-scanned.
    assert cache["feedback_processed"]["tc-stale"]["gave_up"] is True
    assert ft.is_already_processed(cache, "tc-stale")


def test_prune_stale_pending_keeps_entry_with_missing_added_at():
    cache: dict = {"feedback_pending": [{"tc_workout_id": "tc-x",
                                         "date": "2026-05-01"}]}
    dropped = ft.prune_stale_pending(
        cache, now=datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert dropped == []
    assert len(cache["feedback_pending"]) == 1


def test_apply_note_empty_string_classification_preserves_prior_tip():
    """Whitespace-only classification is treated like null — additive-only,
    so the prior tip survives."""
    cache = _make_pending_cache()
    cache["form_tips"][ft.normalise_title("Bench Press")] = {
        "tip": "old", "captured_at": "2026-05-04T12:00:00+00:00",
        "source_tc_id": "x", "tc_title": "Bench Press",
    }
    ft.apply_note(
        cache, "tc1",
        exercises=["Bench Press"],
        classifications={"Bench Press": "   "},  # whitespace-only
    )
    entry = cache["form_tips"][ft.normalise_title("Bench Press")]
    assert entry["tip"] == "old"
    assert entry["source_tc_id"] == "x"


# ---------------------------------------------------------------------------
# is_already_processed — workout-id based; notes are immutable so we never
# re-process a workout id we've already handled.
# ---------------------------------------------------------------------------

def test_is_already_processed_true_after_apply():
    cache = _make_pending_cache()
    ft.apply_note(cache, "tc1",
                  exercises=["Bench Press"],
                  classifications={"Bench Press": "Tuck elbows."})
    assert ft.is_already_processed(cache, "tc1")


def test_is_already_processed_false_for_new_workout():
    cache: dict = {}
    assert not ft.is_already_processed(cache, "tc-never-seen")


# ---------------------------------------------------------------------------
# get_active_tip / prune_expired — TTL behaviour
# ---------------------------------------------------------------------------

def _seed_tip(cache, title, age_days):
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    captured = now - timedelta(days=age_days)
    cache.setdefault("form_tips", {})[ft.normalise_title(title)] = {
        "tip": f"Tip for {title}",
        "captured_at": captured.isoformat(),
        "source_tc_id": "tc-seed",
        "tc_title": title,
    }
    return now


def test_get_active_tip_within_ttl():
    cache: dict = {}
    now = _seed_tip(cache, "Bench Press", age_days=15)
    assert ft.get_active_tip(cache, "Bench Press", now=now) == "Tip for Bench Press"


def test_get_active_tip_expired_returns_none():
    cache: dict = {}
    now = _seed_tip(cache, "Bench Press", age_days=31)
    assert ft.get_active_tip(cache, "Bench Press", now=now) is None


def test_get_active_tip_unknown_exercise_returns_none():
    cache: dict = {}
    assert ft.get_active_tip(cache, "Bench Press") is None


def test_prune_drops_expired_only():
    cache: dict = {}
    now = _seed_tip(cache, "Bench Press", age_days=31)
    _seed_tip(cache, "Squat", age_days=10)
    removed = ft.prune_expired(cache, now=now)
    assert removed == [ft.normalise_title("Bench Press")]
    assert ft.normalise_title("Squat") in cache["form_tips"]


# ---------------------------------------------------------------------------
# append_form_tip — idempotent appending
# ---------------------------------------------------------------------------

def test_append_form_tip_to_empty_notes_omits_separator():
    """No '---' if there are no prior notes to separate from."""
    out = ft.append_form_tip("", "Tuck elbows.")
    assert out == "Coach Tip: Tuck elbows."


def test_append_form_tip_after_existing_notes_uses_separator():
    out = ft.append_form_tip("Start at 50kg\n3 x 5", "Tuck elbows.")
    assert out == "Start at 50kg\n3 x 5\n---\nCoach Tip: Tuck elbows."


def test_append_form_tip_strips_prior_separator_block_before_adding_new():
    notes = "Start at 50kg\n---\nCoach Tip: Old guidance"
    out = ft.append_form_tip(notes, "New guidance")
    assert out == "Start at 50kg\n---\nCoach Tip: New guidance"


def test_append_form_tip_strips_prior_bare_block_before_adding_new():
    """When notes used to be empty (bare 'Coach Tip: ...' shape), and now
    prior content has been added or the tip itself is being replaced,
    we still need to strip the prior bare line cleanly."""
    notes = "Coach Tip: Old guidance"
    out = ft.append_form_tip(notes, "New guidance")
    assert out == "Coach Tip: New guidance"


def test_append_form_tip_replaces_legacy_form_tip_label():
    """Blocks written under the old 'Form Tip:' label are stripped and
    replaced by a single 'Coach Tip:' block — no duplicate, no remnant."""
    notes = "Start at 50kg\n---\nForm Tip: Old guidance"
    out = ft.append_form_tip(notes, "New guidance")
    assert out == "Start at 50kg\n---\nCoach Tip: New guidance"
    assert "Form Tip" not in out

    bare = ft.append_form_tip("Form Tip: Old guidance", "New guidance")
    assert bare == "Coach Tip: New guidance"


def test_append_form_tip_with_none_strips_prior_separator_block():
    notes = "Start at 50kg\n---\nCoach Tip: Old guidance"
    out = ft.append_form_tip(notes, None)
    assert out == "Start at 50kg"


def test_append_form_tip_with_none_strips_legacy_block():
    """None also clears a legacy 'Form Tip:' block cleanly."""
    assert ft.append_form_tip("Form Tip: Old guidance", None) == ""
    assert ft.append_form_tip(
        "Start at 50kg\n---\nForm Tip: Old guidance", None) == "Start at 50kg"


def test_append_form_tip_with_none_strips_prior_bare_block():
    notes = "Coach Tip: Old guidance"
    out = ft.append_form_tip(notes, None)
    assert out == ""


def test_append_form_tip_none_on_empty_returns_empty():
    assert ft.append_form_tip("", None) == ""


# ---------------------------------------------------------------------------
# build_hevy_exercise integration
# ---------------------------------------------------------------------------

def _mk_resolver():
    return t2h.ExerciseResolver(
        history=[{"id": "T_BENCH", "title": "Bench Press (Barbell)",
                  "count": 50, "last_seen": "2026-05-01"}],
        templates=[],
    )


def test_build_hevy_exercise_appends_form_tip():
    r = _mk_resolver()
    out = t2h.build_hevy_exercise(
        "Bench Press", "Start at 50kg\n3 x 5", r,
        form_tip="Keep thinking about tucking the elbows.",
    )
    assert out["notes"].endswith(
        "\n---\nCoach Tip: Keep thinking about tucking the elbows."
    )


def test_build_hevy_exercise_no_tip_leaves_notes_untouched():
    r = _mk_resolver()
    out = t2h.build_hevy_exercise("Bench Press", "Start at 50kg\n3 x 5", r)
    assert "Coach Tip" not in out["notes"]


# ---------------------------------------------------------------------------
# commit() wiring — forward results queue feedback_pending
# ---------------------------------------------------------------------------

def test_commit_queues_forward_for_feedback():
    cache: dict = {}
    results = {
        "forward": [{
            "status": "ok", "hevy_workout_id": "hw1",
            "tc_workout_id": "tc-forward-1", "mode": "ui",
            "date": "2026-05-11",
        }],
    }
    bs.commit(cache, results)
    pending_ids = [e["tc_workout_id"] for e in cache["feedback_pending"]]
    assert "tc-forward-1" in pending_ids


def test_commit_does_not_queue_auto_synced():
    """forward_auto_synced means the TC slot was empty / no push happened —
    nothing for the coach to react to."""
    cache: dict = {}
    results = {
        "forward_auto_synced": [{
            "hevy_workout_id": "hw1", "tc_workout_id": "tc-auto-1",
            "reason": "no_tc_slot_on_date",
        }],
    }
    bs.commit(cache, results)
    assert cache["feedback_pending"] == []


def test_commit_does_not_requeue_already_processed():
    cache: dict = {
        "feedback_processed": {"tc-forward-1": {"processed_at": "2026-05-10"}},
    }
    results = {
        "forward": [{
            "status": "ok", "hevy_workout_id": "hw1",
            "tc_workout_id": "tc-forward-1", "mode": "ui",
        }],
    }
    bs.commit(cache, results)
    assert cache["feedback_pending"] == []
