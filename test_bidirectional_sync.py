"""Tests for bidirectional_sync planning + commit (v3 calendar-week model)."""
from datetime import date, datetime, timedelta, timezone
import json

import bidirectional_sync as bs


# Default empty cache shape used across tests.
def _empty():
    return {"forward": {}, "reverse": {}, "day_routines": {}, "stage1": {},
            "last_run_at": None,
            "feedback_pending": [], "feedback_processed": {},
            "form_tips": {}, "feedback_bootstrap_done": False}


# ---------- plan: forward ----------------------------------------------------

def test_plan_forward_skip_when_cached():
    cache = _empty()
    cache["forward"] = {"w1": {"synced_at": "t", "tc_workout_id": "tc1",
                                "mode": "ui"}}
    hevy = {"workouts": [{"id": "w1", "date": "2026-04-23", "raw": {}}]}
    p = bs.plan(cache, hevy, {"workouts": []},
                {"results_by_date": {"2026-04-23": [{"tc_id": "tcX"}]}},
                today=date(2026, 4, 23))
    assert p["forward"] == []
    assert p["forward_auto_synced"] == []


def test_plan_forward_emits_fill_with_empty_slot():
    cache = _empty()
    hevy = {"workouts": [{"id": "w1", "date": "2026-04-23", "raw": {"x": 1}}]}
    p = bs.plan(cache, hevy, {"workouts": []},
                {"results_by_date": {"2026-04-23": [{"tc_id": "tc-X"}]}},
                today=date(2026, 4, 23))
    assert len(p["forward"]) == 1
    assert p["forward"][0]["tc_slot_id"] == "tc-X"


def test_plan_forward_auto_synced_when_no_slot_on_date():
    cache = _empty()
    hevy = {"workouts": [{"id": "w1", "date": "2026-04-23", "raw": {}}]}
    p = bs.plan(cache, hevy, {"workouts": []}, {"results_by_date": {}},
                today=date(2026, 4, 23))
    assert p["forward"] == []
    assert len(p["forward_auto_synced"]) == 1
    assert p["forward_auto_synced"][0]["reason"] == "no_tc_slot_on_date"


def test_plan_forward_scope_only_most_recent():
    cache = _empty()
    hevy = {"workouts": [
        {"id": "w_newest", "date": "2026-04-23", "raw": {}},
        {"id": "w_older",  "date": "2026-04-22", "raw": {}},
    ]}
    p = bs.plan(cache, hevy, {"workouts": []},
                {"results_by_date": {"2026-04-23": [{"tc_id": "slot"}]}},
                today=date(2026, 4, 23))
    assert len(p["forward"]) == 1
    assert p["forward"][0]["hevy_workout_id"] == "w_newest"


# ---------- select_active_week ----------------------------------------------

def _tc(day, date_iso, content=None, tc_id=None):
    return {
        "tc_id": tc_id or f"tc-{day.lower()}-{date_iso}",
        "date": date_iso,
        "day_name": day,
        "raw_content": content if content is not None else {"v": date_iso},
    }


def test_active_week_uses_current_week_when_future_workouts_present():
    # Tuesday with workouts later this week — current week is active.
    today = date(2026, 5, 5)  # Tuesday
    tc_up = {"workouts": [
        _tc("Wednesday", "2026-05-06"),
        _tc("Friday", "2026-05-08"),
    ]}
    a = bs.select_active_week(tc_up, today=today)
    assert a["promoted"] is False
    assert a["start"] == date(2026, 5, 4)
    assert a["end"] == date(2026, 5, 10)
    assert len(a["workouts"]) == 2


def test_active_week_promotes_when_current_week_all_past():
    # Sunday with all this-week workouts in the past → promote.
    today = date(2026, 5, 10)  # Sunday
    tc_up = {"workouts": [
        _tc("Monday", "2026-05-04"),  # past
        _tc("Wednesday", "2026-05-13"),  # next week
        _tc("Friday", "2026-05-15"),  # next week
    ]}
    a = bs.select_active_week(tc_up, today=today)
    assert a["promoted"] is True
    assert a["start"] == date(2026, 5, 11)
    assert a["end"] == date(2026, 5, 17)
    assert len(a["workouts"]) == 2


def test_active_week_promotes_when_no_workouts_in_current_week():
    # Sunday with TC workouts only next week → vacuous all-past → promote.
    today = date(2026, 5, 3)  # Sunday
    tc_up = {"workouts": [
        _tc("Monday", "2026-05-04"),
        _tc("Wednesday", "2026-05-06"),
        _tc("Thursday", "2026-05-07"),
        _tc("Wednesday", "2026-05-13"),
        _tc("Friday", "2026-05-15"),
    ]}
    a = bs.select_active_week(tc_up, today=today)
    assert a["promoted"] is True
    assert a["start"] == date(2026, 5, 4)
    assert a["end"] == date(2026, 5, 10)
    days = sorted(tc["day_name"] for tc in a["workouts"])
    assert days == ["Monday", "Thursday", "Wednesday"]


def test_active_week_today_workout_keeps_current_week():
    # Today is Wednesday and this week has nothing past today — current wins.
    today = date(2026, 5, 6)  # Wednesday
    tc_up = {"workouts": [
        _tc("Wednesday", "2026-05-06"),  # today
        _tc("Friday", "2026-05-08"),
    ]}
    a = bs.select_active_week(tc_up, today=today)
    assert a["promoted"] is False


def test_active_week_promotes_when_today_done_and_only_workout_in_week():
    # Today is Thursday May 7. This week only has Thursday May 7 and it's
    # already logged in Hevy → promote to next week so its routines get
    # populated instead of leaving the folder full of tombstones.
    today = date(2026, 5, 7)  # Thursday
    tc_up = {"workouts": [
        _tc("Thursday", "2026-05-07", tc_id="tc-thu"),
        _tc("Monday", "2026-05-11"),
        _tc("Wednesday", "2026-05-13"),
        _tc("Friday", "2026-05-15"),
    ]}
    a = bs.select_active_week(
        tc_up, today=today, completed_dates={"2026-05-07"})
    assert a["promoted"] is True
    assert a["start"] == date(2026, 5, 11)
    assert a["end"] == date(2026, 5, 17)
    days = sorted(tc["day_name"] for tc in a["workouts"])
    assert days == ["Friday", "Monday", "Wednesday"]


def test_active_week_promotes_via_completed_tc_ids():
    # Same scenario but the signal comes from cache.forward (tc_id seen in a
    # prior forward sync) rather than today's hevy_snapshot.
    today = date(2026, 5, 7)
    tc_up = {"workouts": [
        _tc("Thursday", "2026-05-07", tc_id="tc-thu"),
        _tc("Monday", "2026-05-11"),
    ]}
    a = bs.select_active_week(
        tc_up, today=today, completed_tc_ids={"tc-thu"})
    assert a["promoted"] is True
    assert a["start"] == date(2026, 5, 11)


def test_active_week_does_not_promote_with_unfinished_later_in_week():
    # Today's done but Saturday isn't — current week stays active.
    today = date(2026, 5, 7)  # Thursday
    tc_up = {"workouts": [
        _tc("Thursday", "2026-05-07", tc_id="tc-thu"),
        _tc("Saturday", "2026-05-09", tc_id="tc-sat"),
    ]}
    a = bs.select_active_week(
        tc_up, today=today,
        completed_dates={"2026-05-07"}, completed_tc_ids={"tc-thu"})
    assert a["promoted"] is False
    assert a["start"] == date(2026, 5, 4)


def test_plan_promotes_when_active_week_complete_and_emits_reverse():
    # End-to-end: cache shows Thursday already forward-synced; plan should
    # auto-promote to next week and emit reverse items for Mon/Wed/Fri so
    # Hevy is populated with the next week's routines.
    cache = _empty()
    cache["forward"] = {"hw-thu": {"synced_at": "t", "tc_workout_id": "tc-thu",
                                     "mode": "ui"}}
    cache["day_routines"] = {"Thursday": "rid_thu_old"}
    today = date(2026, 5, 7)  # Thursday
    hevy_snapshot = {"workouts": [
        {"id": "hw-thu", "date": "2026-05-07", "raw": {"title": "Thursday"}},
    ]}
    tc_up = {"workouts": [
        _tc("Thursday", "2026-05-07", tc_id="tc-thu"),
        _tc("Monday", "2026-05-11", tc_id="tc-mon", content={"a": 1}),
        _tc("Wednesday", "2026-05-13", tc_id="tc-wed", content={"b": 2}),
        _tc("Friday", "2026-05-15", tc_id="tc-fri", content={"c": 3}),
    ]}
    folder = {"routines": [
        {"id": "rid_thu_old", "title": "Thursday"},
    ]}
    p = bs.plan(
        cache, hevy_snapshot, tc_up,
        {"results_by_date": {}}, hevy_folder=folder, today=today,
    )
    assert p["active_week"]["promoted"] is True
    assert p["active_week"]["start"] == "2026-05-11"
    days = sorted(r["day_name"] for r in p["reverse"])
    assert days == ["Friday", "Monday", "Wednesday"]
    # The stale Thursday slot is repurposed into one of the new days
    # (POST count should be 2 instead of 3) — exact pairing is by weekday
    # order, so Friday gets the recycled slot last; Monday is paired with
    # Thursday's old slot.
    monday = next(r for r in p["reverse"] if r["day_name"] == "Monday")
    assert monday["existing_routine_id"] == "rid_thu_old"
    assert monday["repurposed_from"] == "Thursday"
    # Forward should be empty (hw-thu already in cache.forward).
    assert p["forward"] == []


# ---------- plan: reverse ---------------------------------------------------

def test_plan_reverse_emits_for_each_day_in_active_week():
    """With Mon/Wed/Fri in Hevy and Mon/Wed/Thu active, the stale Friday
    slot is REPURPOSED into Thursday rather than POSTing a new routine."""
    cache = _empty()
    cache["day_routines"] = {"Monday": "rid_mon", "Wednesday": "rid_wed",
                              "Friday": "rid_fri"}
    folder = {"routines": [
        {"id": "rid_mon", "title": "Monday"},
        {"id": "rid_wed", "title": "Wednesday"},
        {"id": "rid_fri", "title": "Friday"},
    ]}
    tc_up = {"workouts": [
        _tc("Monday", "2026-05-04", content={"a": 1}),
        _tc("Wednesday", "2026-05-06", content={"b": 2}),
        _tc("Thursday", "2026-05-07", content={"c": 3}),
    ]}
    p = bs.plan(cache, {"workouts": []}, tc_up, {"results_by_date": {}},
                hevy_folder=folder, today=date(2026, 5, 3))
    days = sorted(r["day_name"] for r in p["reverse"])
    assert days == ["Monday", "Thursday", "Wednesday"]
    # Monday/Wednesday — kept in place.
    monday = next(r for r in p["reverse"] if r["day_name"] == "Monday")
    assert monday["existing_routine_id"] == "rid_mon"
    assert monday["repurposed_from"] is None
    # Thursday — repurposed from the Friday slot.
    thursday = next(r for r in p["reverse"] if r["day_name"] == "Thursday")
    assert thursday["existing_routine_id"] == "rid_fri"
    assert thursday["repurposed_from"] == "Friday"
    # No tombstones — Friday got repurposed.
    assert p["reverse_tombstones"] == []


def test_plan_reverse_skips_unchanged_when_routine_exists():
    content = {"v": "same"}
    cache = _empty()
    cache["day_routines"] = {"Monday": "rid_mon"}
    tc_workout = _tc("Monday", "2026-05-04", content=content)
    h = bs.tc_content_hash(cache, tc_workout)
    cache["reverse"] = {"Monday": {"tc_content_hash": h, "payload_hash": "p"}}
    folder = {"routines": [{"id": "rid_mon", "title": "Monday"}]}
    tc_up = {"workouts": [tc_workout]}
    p = bs.plan(cache, {"workouts": []}, tc_up, {"results_by_date": {}},
                hevy_folder=folder, today=date(2026, 5, 3))
    assert p["reverse"] == []


def test_plan_reverse_emits_when_form_tip_changes_but_content_unchanged():
    """A fresh coach tip should trigger a reverse push even if the TC
    plan text is byte-identical to last time."""
    content = {"exercises": [{"position": "A", "title": "Bench Press",
                              "plan": "Start at 50kg\n3 x 5"}]}
    cache = _empty()
    cache["day_routines"] = {"Monday": "rid_mon"}
    tc_workout = _tc("Monday", "2026-05-04", content=content)
    # Cache from a run BEFORE any tip was set — hash reflects no-tip state.
    h_before = bs.tc_content_hash(cache, tc_workout)
    cache["reverse"] = {"Monday": {"tc_content_hash": h_before,
                                    "payload_hash": "p-old"}}
    # Now the coach has left feedback, populating a form tip for Bench Press.
    cache["form_tips"] = {
        "bench press": {
            "tip": "Tuck the elbows.",
            "captured_at": "2026-05-04T12:00:00+00:00",
            "source_tc_id": "tc-prev",
            "tc_title": "Bench Press",
        }
    }
    folder = {"routines": [{"id": "rid_mon", "title": "Monday"}]}
    tc_up = {"workouts": [tc_workout]}
    p = bs.plan(cache, {"workouts": []}, tc_up, {"results_by_date": {}},
                hevy_folder=folder, today=date(2026, 5, 3))
    assert len(p["reverse"]) == 1
    assert p["reverse"][0]["day_name"] == "Monday"


def test_plan_reverse_emits_when_form_tip_expires():
    """When a tip times out the planner should re-push so the routine
    drops the now-stale Form Tip block."""
    content = {"exercises": [{"position": "A", "title": "Bench Press",
                              "plan": "Start at 50kg\n3 x 5"}]}
    cache = _empty()
    cache["day_routines"] = {"Monday": "rid_mon"}
    tc_workout = _tc("Monday", "2026-05-04", content=content)
    # Seed a tip in cache and compute the hash WITH it active.
    cache["form_tips"] = {
        "bench press": {
            "tip": "Tuck the elbows.",
            "captured_at": "2026-04-01T12:00:00+00:00",  # >30 days old by 2026-05-04
            "source_tc_id": "tc-prev",
            "tc_title": "Bench Press",
        }
    }
    # Compute the cached hash AS IF the tip was still active when last pushed,
    # by temporarily faking captured_at to recent then restoring.
    cache["form_tips"]["bench press"]["captured_at"] = "2026-04-25T12:00:00+00:00"
    h_with_tip = bs.tc_content_hash(cache, tc_workout)
    cache["form_tips"]["bench press"]["captured_at"] = "2026-04-01T12:00:00+00:00"
    cache["reverse"] = {"Monday": {"tc_content_hash": h_with_tip,
                                    "payload_hash": "p-old"}}
    folder = {"routines": [{"id": "rid_mon", "title": "Monday"}]}
    tc_up = {"workouts": [tc_workout]}
    p = bs.plan(cache, {"workouts": []}, tc_up, {"results_by_date": {}},
                hevy_folder=folder, today=date(2026, 5, 4))
    assert len(p["reverse"]) == 1


def test_plan_reverse_emits_when_routine_missing_in_folder_even_if_cache_matches():
    """If the cached day_routine ID isn't in the folder anymore (e.g.
    Mark deleted manually), re-emit so we POST a fresh routine."""
    content = {"v": "x"}
    h = bs.sha(content)
    cache = _empty()
    cache["day_routines"] = {"Monday": "rid_old"}
    cache["reverse"] = {"Monday": {"tc_content_hash": h, "payload_hash": "p"}}
    folder = {"routines": []}  # Mark deleted Monday
    tc_up = {"workouts": [_tc("Monday", "2026-05-04", content=content)]}
    p = bs.plan(cache, {"workouts": []}, tc_up, {"results_by_date": {}},
                hevy_folder=folder, today=date(2026, 5, 3))
    assert len(p["reverse"]) == 1
    assert p["reverse"][0]["existing_routine_id"] is None


def test_plan_reverse_collision_takes_earliest_defers_rest():
    cache = _empty()
    folder = {"routines": []}
    tc_up = {"workouts": [
        _tc("Wednesday", "2026-05-13", content={"v": "later"}),
        _tc("Wednesday", "2026-05-06", content={"v": "earlier"}),
    ]}
    # Today must put both workouts in the active window.
    p = bs.plan(cache, {"workouts": []}, tc_up, {"results_by_date": {}},
                hevy_folder=folder, today=date(2026, 5, 6))
    # Only May 6 lands in active window (May 4-10); May 13 is outside →
    # deferred with reason "outside_active_week", not collision.
    assert [r["day_name"] for r in p["reverse"]] == ["Wednesday"]
    deferred_dates = sorted(d["date"] for d in p["deferred"])
    assert "2026-05-13" in deferred_dates


def test_plan_reverse_within_window_collision_keeps_earliest():
    """Two workouts on the same day_name within the same week → earliest
    wins, the later same-week one is deferred as a collision."""
    cache = _empty()
    folder = {"routines": []}
    tc_up = {"workouts": [
        # Both in active week (May 4-10):
        _tc("Wednesday", "2026-05-06", content={"v": "earlier"}, tc_id="early"),
        _tc("Wednesday", "2026-05-06", content={"v": "later"},   tc_id="later"),
    ]}
    p = bs.plan(cache, {"workouts": []}, tc_up, {"results_by_date": {}},
                hevy_folder=folder, today=date(2026, 5, 4))
    assert len(p["reverse"]) == 1


def test_plan_reverse_deferred_outside_active_week():
    """Workouts in this week stay; workouts beyond this week are deferred."""
    cache = _empty()
    folder = {"routines": []}
    tc_up = {"workouts": [
        # In active week (May 4-10):
        _tc("Wednesday", "2026-05-06"),
        # Outside (week of May 11-17):
        _tc("Wednesday", "2026-05-13"),
        _tc("Friday",    "2026-05-15"),
    ]}
    p = bs.plan(cache, {"workouts": []}, tc_up, {"results_by_date": {}},
                hevy_folder=folder, today=date(2026, 5, 4))
    assert [r["day_name"] for r in p["reverse"]] == ["Wednesday"]
    assert [r["date"] for r in p["reverse"]] == ["2026-05-06"]
    deferred_dates = {d["date"] for d in p["deferred"]}
    assert deferred_dates == {"2026-05-13", "2026-05-15"}
    for d in p["deferred"]:
        assert d["reason"] == "outside_active_week"


def test_plan_reverse_completed_day_tombstones_routine():
    """When a workout for an active-week day is logged in Hevy, that day's
    Hevy routine is stale: the day falls out of by_day and the routine
    gets tombstoned via the stale_days path."""
    cache = _empty()
    cache["day_routines"] = {"Wednesday": "rid_wed", "Thursday": "rid_thu"}
    cache["reverse"] = {
        "Wednesday": {"tc_content_hash": "old", "payload_hash": "p"},
        "Thursday":  {"tc_content_hash": "old", "payload_hash": "p"},
    }
    folder = {"routines": [
        {"id": "rid_wed", "title": "Wednesday"},
        {"id": "rid_thu", "title": "Thursday"},
    ]}
    tc_up = {"workouts": [
        _tc("Wednesday", "2026-05-06"),
        _tc("Thursday",  "2026-05-07"),
    ]}
    # Hevy snapshot says Wed May 6 was logged. Thursday hasn't happened yet.
    hevy = {"workouts": [{"id": "w_wed", "date": "2026-05-06", "raw": {}}]}
    p = bs.plan(cache, hevy, tc_up,
                {"results_by_date": {"2026-05-06": [{"tc_id": "tc-wed"}]}},
                hevy_folder=folder, today=date(2026, 5, 6))
    # Wednesday tombstoned, Thursday kept.
    tomb_days = {t["day_name"] for t in p["reverse_tombstones"]}
    assert tomb_days == {"Wednesday"}
    rev_days = {r["day_name"] for r in p["reverse"]}
    assert "Wednesday" not in rev_days
    # Wednesday landed in deferred with the new reason.
    deferred_for_wed = [d for d in p["deferred"]
                        if d.get("day_name") == "Wednesday"]
    assert len(deferred_for_wed) == 1
    assert deferred_for_wed[0]["reason"] == "completed_in_hevy"


def test_plan_reverse_completed_day_with_no_other_active_days_just_tombstones():
    """If the only active-week day is already completed in Hevy, the
    routine is still tombstoned and reverse stays empty."""
    cache = _empty()
    cache["day_routines"] = {"Wednesday": "rid_wed"}
    folder = {"routines": [{"id": "rid_wed", "title": "Wednesday"}]}
    tc_up = {"workouts": [_tc("Wednesday", "2026-05-06")]}
    hevy = {"workouts": [{"id": "w_wed", "date": "2026-05-06", "raw": {}}]}
    p = bs.plan(cache, hevy, tc_up,
                {"results_by_date": {"2026-05-06": [{"tc_id": "tc-wed"}]}},
                hevy_folder=folder, today=date(2026, 5, 6))
    assert p["reverse"] == []
    assert [t["day_name"] for t in p["reverse_tombstones"]] == ["Wednesday"]


# ---------- plan: deletes ---------------------------------------------------

def test_plan_reverse_tombstones_only_legacy_extras_when_stales_repurposed():
    """When a new day exists, prefer to repurpose the stale Friday slot.
    Only legacy extras get tombstoned. Already-tombstoned `_` and manual
    routines stay alone."""
    cache = _empty()
    folder = {"routines": [
        {"id": "rid_mon", "title": "Monday"},
        {"id": "rid_wed", "title": "Wednesday"},
        {"id": "rid_fri", "title": "Friday"},      # repurposed → Thursday
        {"id": "rid_arm", "title": "Arm Snack"},   # manual — keep
        {"id": "rid_old1", "title": "Friday 2026-05-08"},   # legacy extra
        {"id": "rid_old2", "title": "Wednesday 2026-05-13"},  # legacy extra
        {"id": "rid_tomb", "title": "_"},          # already tombstoned
    ]}
    tc_up = {"workouts": [
        _tc("Monday", "2026-05-04"),
        _tc("Wednesday", "2026-05-06"),
        _tc("Thursday", "2026-05-07"),
    ]}
    p = bs.plan(cache, {"workouts": []}, tc_up, {"results_by_date": {}},
                hevy_folder=folder, today=date(2026, 5, 3))
    tombstoned_ids = {d["routine_id"] for d in p["reverse_tombstones"]}
    # Friday got repurposed into Thursday → not tombstoned.
    # Only legacy extras remain.
    assert tombstoned_ids == {"rid_old1", "rid_old2"}
    assert "rid_fri" not in tombstoned_ids
    assert "rid_arm" not in tombstoned_ids
    assert "rid_tomb" not in tombstoned_ids


def test_plan_reverse_repurposes_existing_tombstone_when_no_stale_slots():
    """A new day with no stale slots available reuses an existing `_`
    tombstone rather than POSTing a fresh routine."""
    cache = _empty()
    folder = {"routines": [
        {"id": "rid_mon", "title": "Monday"},
        {"id": "rid_wed", "title": "Wednesday"},
        {"id": "rid_tomb", "title": "_"},   # leftover from a previous week
    ]}
    tc_up = {"workouts": [
        _tc("Monday", "2026-05-04"),
        _tc("Wednesday", "2026-05-06"),
        _tc("Thursday", "2026-05-07"),
    ]}
    p = bs.plan(cache, {"workouts": []}, tc_up, {"results_by_date": {}},
                hevy_folder=folder, today=date(2026, 5, 3))
    thursday = next(r for r in p["reverse"] if r["day_name"] == "Thursday")
    assert thursday["existing_routine_id"] == "rid_tomb"
    # repurposed_from is None when the slot was a tombstone (no day to clear).
    assert thursday["repurposed_from"] is None
    # No new tombstones either.
    assert p["reverse_tombstones"] == []


def test_plan_reverse_prefers_stale_day_over_tombstone():
    """When both a stale day-name slot and a tombstone exist, prefer the
    stale day (so day-name routines get reused before tombstones)."""
    cache = _empty()
    folder = {"routines": [
        {"id": "rid_mon", "title": "Monday"},
        {"id": "rid_fri", "title": "Friday"},     # stale
        {"id": "rid_tomb", "title": "_"},          # also available
    ]}
    tc_up = {"workouts": [
        _tc("Monday", "2026-05-04"),
        _tc("Thursday", "2026-05-07"),
    ]}
    p = bs.plan(cache, {"workouts": []}, tc_up, {"results_by_date": {}},
                hevy_folder=folder, today=date(2026, 5, 3))
    thursday = next(r for r in p["reverse"] if r["day_name"] == "Thursday")
    assert thursday["existing_routine_id"] == "rid_fri"
    assert thursday["repurposed_from"] == "Friday"
    # Tombstone slot stays alone.
    assert p["reverse_tombstones"] == []


def test_plan_reverse_more_new_days_than_slots_posts_the_overflow():
    cache = _empty()
    folder = {"routines": [
        {"id": "rid_mon", "title": "Monday"},  # in active, kept
    ]}
    tc_up = {"workouts": [
        _tc("Monday",    "2026-05-04"),
        _tc("Tuesday",   "2026-05-05"),
        _tc("Wednesday", "2026-05-06"),
        _tc("Thursday",  "2026-05-07"),
    ]}
    p = bs.plan(cache, {"workouts": []}, tc_up, {"results_by_date": {}},
                hevy_folder=folder, today=date(2026, 5, 3))
    # Tue/Wed/Thu have no slots to repurpose → all POST.
    posters = [r for r in p["reverse"] if r["existing_routine_id"] is None]
    assert sorted(r["day_name"] for r in posters) == \
           ["Thursday", "Tuesday", "Wednesday"]


def test_plan_reverse_more_stales_than_new_days_tombstones_overflow():
    """Two stale day slots, only one new day — repurpose one, tombstone one."""
    cache = _empty()
    folder = {"routines": [
        {"id": "rid_mon", "title": "Monday"},
        {"id": "rid_wed", "title": "Wednesday"},
        {"id": "rid_fri", "title": "Friday"},
    ]}
    tc_up = {"workouts": [
        _tc("Monday",   "2026-05-04"),
        _tc("Tuesday",  "2026-05-05"),  # one new day
    ]}
    p = bs.plan(cache, {"workouts": []}, tc_up, {"results_by_date": {}},
                hevy_folder=folder, today=date(2026, 5, 3))
    # Tuesday repurposes the EARLIEST stale (Wednesday).
    tue = next(r for r in p["reverse"] if r["day_name"] == "Tuesday")
    assert tue["existing_routine_id"] == "rid_wed"
    assert tue["repurposed_from"] == "Wednesday"
    # Friday is the leftover stale → tombstoned.
    tombstoned_ids = {t["routine_id"] for t in p["reverse_tombstones"]}
    assert tombstoned_ids == {"rid_fri"}


def test_plan_reverse_tombstones_excludes_days_still_active():
    cache = _empty()
    folder = {"routines": [
        {"id": "rid_mon", "title": "Monday"},
        {"id": "rid_wed", "title": "Wednesday"},
    ]}
    tc_up = {"workouts": [_tc("Monday", "2026-05-04"),
                          _tc("Wednesday", "2026-05-06")]}
    p = bs.plan(cache, {"workouts": []}, tc_up, {"results_by_date": {}},
                hevy_folder=folder, today=date(2026, 5, 4))
    assert p["reverse_tombstones"] == []


# ---------- commit ----------------------------------------------------------

def test_commit_reverse_ok_records_day_keyed_hashes():
    cache = _empty()
    res = {"forward": [], "reverse": [
        {"day_name": "Monday", "status": "ok",
         "tc_content_hash": "h", "payload_hash": "p"},
    ]}
    c = bs.commit(cache, res)
    e = c["reverse"]["Monday"]
    assert e["tc_content_hash"] == "h"
    assert e["payload_hash"] == "p"
    assert e["last_pushed_at"] is not None


def test_commit_reverse_skipped_keeps_payload_hash():
    cache = _empty()
    cache["reverse"] = {"Monday": {"tc_content_hash": "old",
                                    "payload_hash": "pOLD"}}
    res = {"forward": [], "reverse": [
        {"day_name": "Monday", "status": "skipped", "tc_content_hash": "h"},
    ]}
    c = bs.commit(cache, res)
    e = c["reverse"]["Monday"]
    assert e["tc_content_hash"] == "h"
    assert e["payload_hash"] == "pOLD"


def test_commit_reverse_post_records_routine_id_in_day_routines():
    cache = _empty()
    res = {"forward": [], "reverse": [
        {"day_name": "Thursday", "status": "ok",
         "tc_content_hash": "h", "payload_hash": "p",
         "routine_id": "newly-created-id"},
    ]}
    c = bs.commit(cache, res)
    assert c["day_routines"]["Thursday"] == "newly-created-id"
    assert c["reverse"]["Thursday"]["payload_hash"] == "p"


def test_commit_reverse_tombstones_removes_day_routine_and_reverse():
    cache = _empty()
    cache["day_routines"] = {"Friday": "rid_fri"}
    cache["reverse"] = {"Friday": {"tc_content_hash": "h",
                                    "payload_hash": "p"}}
    res = {"forward": [], "reverse": [],
           "reverse_tombstones": [{"day_name": "Friday", "status": "ok"}]}
    c = bs.commit(cache, res)
    assert "Friday" not in c["day_routines"]
    assert "Friday" not in c["reverse"]


def test_commit_reverse_tombstones_error_leaves_cache():
    cache = _empty()
    cache["day_routines"] = {"Friday": "rid_fri"}
    res = {"forward": [], "reverse": [],
           "reverse_tombstones": [{"day_name": "Friday", "status": "error"}]}
    c = bs.commit(cache, res)
    assert c["day_routines"]["Friday"] == "rid_fri"


# ---------- forward commit (unchanged) --------------------------------------

def test_commit_forward_ok_records():
    cache = _empty()
    res = {"forward": [{"hevy_workout_id": "w1", "status": "ok",
                        "tc_workout_id": "tcX", "mode": "ui"}], "reverse": []}
    c = bs.commit(cache, res)
    assert c["forward"]["w1"]["tc_workout_id"] == "tcX"
    assert c["last_run_at"] is not None


def test_commit_forward_auto_synced_records():
    cache = _empty()
    res = {"forward": [],
           "forward_auto_synced": [
               {"hevy_workout_id": "w1", "hevy_workout_date": "2026-04-22",
                "tc_workout_id": "tcX", "reason": "no_tc_slot_on_date"}],
           "reverse": []}
    c = bs.commit(cache, res)
    assert c["forward"]["w1"]["mode"] == "auto"


# ---------- hashing / fingerprint -------------------------------------------

def test_sha_stable_across_key_order():
    a = {"x": 1, "y": [1, 2, 3]}
    b = {"y": [1, 2, 3], "x": 1}
    assert bs.sha(a) == bs.sha(b)


def test_fingerprint_tc_list_order_insensitive():
    a = [("tc1", "2026-04-27"), ("tc2", "2026-04-29")]
    b = [("tc2", "2026-04-29"), ("tc1", "2026-04-27")]
    assert bs.fingerprint_tc_list(a) == bs.fingerprint_tc_list(b)


# ---------- stage 0 / cooldown ----------------------------------------------

def test_cooldown_inactive_when_no_last_run():
    assert bs.cooldown_active({"last_run_at": None}) is False


def test_cooldown_active_when_within_window():
    now = datetime.now(timezone.utc)
    cache = {"last_run_at": (now - timedelta(minutes=5)).isoformat()}
    assert bs.cooldown_active(cache, minutes=30) is True


def test_cooldown_inactive_when_outside_window():
    now = datetime.now(timezone.utc)
    cache = {"last_run_at": (now - timedelta(hours=5)).isoformat()}
    assert bs.cooldown_active(cache, minutes=30) is False


# ---------- stage 1 fingerprints --------------------------------------------

def test_update_stage1_sets_both_fields():
    cache = _empty()
    bs.update_stage1(cache, tc_upcoming_fingerprint="fp1",
                     last_hevy_workout_id="w1")
    assert cache["stage1"]["tc_upcoming_fingerprint"] == "fp1"
    assert cache["stage1"]["last_hevy_workout_id"] == "w1"


# ---------- cache I/O -------------------------------------------------------

def test_cache_roundtrip(tmp_path, monkeypatch):
    p = tmp_path / "c.json"
    monkeypatch.setattr(bs, "CACHE_PATH", p)
    c1 = {"forward": {"w": {"synced_at": "t"}}, "reverse": {},
          "day_routines": {"Monday": "rid_mon"},
          "stage1": {"tc_upcoming_fingerprint": "fp"}, "last_run_at": "t",
          "feedback_pending": [], "feedback_processed": {},
          "form_tips": {}, "feedback_bootstrap_done": False}
    bs.save_cache(c1)
    c2 = bs.load_cache()
    assert c2 == c1


def test_load_cache_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(bs, "CACHE_PATH", tmp_path / "nope.json")
    c = bs.load_cache()
    assert c == _empty()


def test_load_cache_corrupt_is_recovered(tmp_path, monkeypatch):
    p = tmp_path / "bad.json"
    p.write_text("{{{ not json")
    monkeypatch.setattr(bs, "CACHE_PATH", p)
    c = bs.load_cache()
    assert c == _empty()
    assert (tmp_path / "bad.corrupt.json").exists()


def test_load_cache_backfills_missing_keys(tmp_path, monkeypatch):
    """An older cache (pre-v3) without 'day_routines' still loads cleanly."""
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"forward": {}, "reverse": {},
                             "last_run_at": "t"}))
    monkeypatch.setattr(bs, "CACHE_PATH", p)
    c = bs.load_cache()
    assert "day_routines" in c and c["day_routines"] == {}
    assert "stage1" in c


# ---------- override + pending helpers (unchanged) --------------------------

def test_normalise_tc_title_strips_parens_and_punct():
    assert bs._normalise_tc_title("Single Leg RDL (Dumbbell)") == \
           "single leg rdl dumbbell"
    assert bs._normalise_tc_title("Chin-Up") == "chin up"


def test_add_override_writes_and_clears_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(bs, "_OVERRIDES_PATH", tmp_path / "ov.json")
    monkeypatch.setattr(bs, "_PENDING_PATH",   tmp_path / "pa.json")
    bs.record_pending_approval(
        "Stiff Leg Deadlift",
        best_guess={"id": "X", "title": "Stiff Leg DL Dumbbell", "conf": "history-fuzzy"},
        alternatives=[],
    )
    bs.add_override("Stiff Leg Deadlift", "RIGHT_ID", "Straight Leg Deadlift")
    overrides = json.loads((tmp_path / "ov.json").read_text())
    assert overrides["stiff leg deadlift"]["exercise_template_id"] == "RIGHT_ID"
    pending_after = json.loads((tmp_path / "pa.json").read_text())
    assert "stiff leg deadlift" not in pending_after


# ---------- validate_put_response (Fix c) ----------------------------------

def _ex(tid, sets=None, tc_title=None):
    """Helper: minimal exercise dict for the validator."""
    out = {"exercise_template_id": tid, "sets": sets or []}
    if tc_title:
        out["tc_title"] = tc_title
    return out


def test_validate_put_response_clean_match():
    """All exercises echoed back → ok, no drops, no extras."""
    payload = {"routine": {"title": "Monday", "exercises": [
        _ex("A1", [{"reps": 5}]), _ex("B2", [{"reps": 10}]),
    ]}}
    response = {"routine": [{"title": "Monday", "exercises": [
        _ex("A1", [{"reps": 5}]), _ex("B2", [{"reps": 10}]),
    ]}]}
    r = bs.validate_put_response(payload, response)
    assert r["ok"] is True
    assert r["payload_count"] == 2
    assert r["response_count"] == 2
    assert r["dropped"] == []
    assert r["extra"] == []


def test_validate_put_response_detects_silent_drop():
    """The exact failure mode from 2026-05-18: Chin-Up with sets:[]
    silently dropped by Hevy. Validator must flag it."""
    payload = {"routine": {"exercises": [
        _ex("C6272009", [{"reps": 5}], tc_title="Deadlift"),
        _ex("29083183", [], tc_title="Chin-Up"),    # the dropped one
        _ex("B33B526E", [{"reps": 12}], tc_title="Single Arm Curl"),
    ]}}
    # Hevy's response omits Chin-Up entirely.
    response = {"routine": [{"exercises": [
        _ex("C6272009", [{"reps": 5}]),
        _ex("B33B526E", [{"reps": 12}]),
    ]}]}
    r = bs.validate_put_response(payload, response)
    assert r["ok"] is False
    assert r["payload_count"] == 3
    assert r["response_count"] == 2
    assert len(r["dropped"]) == 1
    assert r["dropped"][0]["template"] == "29083183"
    assert r["dropped"][0]["title"] == "Chin-Up"
    assert r["dropped"][0]["index"] == 1


def test_validate_put_response_accepts_bare_routine_payload():
    """Caller may pass the inner routine object directly (no 'routine'
    wrapper) — accept both shapes for ergonomics."""
    bare_payload = {"exercises": [_ex("A1", [{"reps": 5}])]}
    response = {"routine": [{"exercises": [_ex("A1", [{"reps": 5}])]}]}
    r = bs.validate_put_response(bare_payload, response)
    assert r["ok"] is True


def test_validate_put_response_accepts_singleton_routine_response():
    """Some endpoints return {"routine": {...}} instead of a list."""
    payload = {"routine": {"exercises": [_ex("A1", [{"reps": 5}])]}}
    response = {"routine": {"exercises": [_ex("A1", [{"reps": 5}])]}}
    r = bs.validate_put_response(payload, response)
    assert r["ok"] is True


def test_validate_put_response_flags_extra_in_response():
    """If Hevy returns more exercises than we sent (auto-injection,
    weird state), flag it via the 'extra' bucket."""
    payload = {"routine": {"exercises": [_ex("A1", [{"reps": 5}])]}}
    response = {"routine": [{"exercises": [
        _ex("A1", [{"reps": 5}]), _ex("MYSTERY", [{"reps": 1}]),
    ]}]}
    r = bs.validate_put_response(payload, response)
    assert r["ok"] is False
    assert r["extra"] == [{"index": 1, "template": "MYSTERY"}]


def test_validate_put_response_handles_duplicate_templates():
    """Same template_id appearing twice in payload — counts must match.
    If response keeps both, ok; if only one survives, the second
    instance is reported as dropped (FIFO consumption)."""
    payload = {"routine": {"exercises": [
        _ex("A1", [{"reps": 5}], tc_title="First"),
        _ex("A1", [{"reps": 5}], tc_title="Second"),
    ]}}
    response_keeps_both = {"routine": [{"exercises": [
        _ex("A1", [{"reps": 5}]), _ex("A1", [{"reps": 5}]),
    ]}]}
    r = bs.validate_put_response(payload, response_keeps_both)
    assert r["ok"] is True

    response_keeps_one = {"routine": [{"exercises": [_ex("A1", [{"reps": 5}])]}]}
    r = bs.validate_put_response(payload, response_keeps_one)
    assert r["ok"] is False
    assert len(r["dropped"]) == 1
    # The first occurrence consumed the response slot; second was dropped.
    assert r["dropped"][0]["index"] == 1
    assert r["dropped"][0]["title"] == "Second"
