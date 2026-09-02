"""Unit tests for truecoach_to_hevy.

Run: python3 test_truecoach_to_hevy.py
"""
from truecoach_to_hevy import (
    parse_plan,
    is_dumbbell,
    ExerciseResolver,
    _rest_seconds_for,
)


# ---------------------------------------------------------------------------
# Plan parser — templates
# ---------------------------------------------------------------------------

def test_template_simple():
    p = parse_plan("Bench Press", "5 x 5 @ 60kg")
    assert len(p.working_sets) == 5
    assert all(s.type == "normal" and s.weight_kg == 60 and s.reps == 5 for s in p.working_sets)


def test_template_ranges_take_high_end():
    p = parse_plan("Row", "4-5 x 10-12 @ 45kg")
    assert len(p.working_sets) == 5
    assert p.working_sets[0].reps == 12
    assert p.working_sets[0].weight_kg == 45


def test_template_with_start_hint():
    p = parse_plan("Squat", "Start at 50kg\n2-3 x 6-10")
    assert len(p.working_sets) == 3
    assert p.working_sets[0].reps == 10
    assert p.working_sets[0].weight_kg == 50
    assert "Start at 50kg" in p.notes


def test_template_no_weight_no_hint():
    p = parse_plan("Hollow Hold", "3 x 12 RIR 2")
    assert len(p.working_sets) == 3
    assert all(s.weight_kg == 0 and s.reps == 12 for s in p.working_sets)
    assert "RIR 2" in p.notes


def test_template_bodyweight_pushup():
    p = parse_plan("Push-Up", "Bodyweight\n3-5 x 6-12 reps\nRIR 2-3")
    assert len(p.working_sets) == 5
    assert p.working_sets[0].weight_kg == 0 and p.working_sets[0].reps == 12
    assert "Bodyweight" in p.notes
    assert "RIR 2-3" in p.notes


def test_sets_only_no_rep_target():
    """'<n>-<m> sets x RIR ...' — set count with no numeric rep target.
    Should emit <sets_hi> bodyweight sets with reps=None so the exercise
    survives the Hevy PUT and Mark fills in reps when he logs it."""
    p = parse_plan("Push-Up", "Keep hands just outside ribs\n3-5 sets x RIR 2-3")
    assert len(p.working_sets) == 5
    for s in p.working_sets:
        assert s.weight_kg == 0
        assert s.reps is None
    assert "3-5 sets x RIR 2-3" in p.notes
    assert "Keep hands just outside ribs" in p.notes
    # And no "no sets parsed" warning
    assert not any("No sets parsed" in w for w in p.warnings)


def test_sets_only_singular_form():
    """Trailing 's' on 'sets' is optional and the lower bound is also OK
    by itself ('5 sets x RIR 1')."""
    p = parse_plan("Plank Hold", "5 sets x RIR 1")
    assert len(p.working_sets) == 5
    assert all(s.reps is None for s in p.working_sets)


def test_sets_word_before_x_with_rep_range():
    """'4 sets x 8-12 reps / RIR 2' — the word 'sets' between the count
    and the 'x' must not stop the rep range being read. Working sets take
    the HIGH end of the range. Seen 2026-09-02 on Band Assisted Dip, which
    shipped 4 placeholder sets with blank reps."""
    p = parse_plan("Band Assisted Dip", "4 sets x 8-12 reps / RIR 2")
    assert len(p.working_sets) == 4
    for s in p.working_sets:
        assert s.weight_kg == 0
        assert s.reps == 12
    assert "RIR 2" in p.notes
    assert not any("No sets parsed" in w for w in p.warnings)


def test_sets_word_before_x_with_range_and_weight():
    """Same shape, with a set range and a weight hint: '3-4 sets x 6-8 @ 40kg'."""
    p = parse_plan("Zercher Squat", "3-4 sets x 6-8 @ 40kg")
    assert len(p.working_sets) == 4
    assert all(s.reps == 8 and s.weight_kg == 40 for s in p.working_sets)


def test_weight_hint_bare_start():
    """'Start 12.5kg' without 'at' or 'with' — Cillian writes it both ways.
    A dumbbell exercise should still get the doubling."""
    p = parse_plan(
        "Chest Supported Row (Dumbbell)",
        "3-4 x 8-12\nRIR 2\nStart 12.5kg\nProgress by 2.5-5kg",
    )
    assert len(p.working_sets) == 4
    # Dumbbell exercise → weight doubled (12.5 × 2 = 25)
    assert all(s.weight_kg == 25.0 for s in p.working_sets)
    assert all(s.reps == 12 for s in p.working_sets)
    assert "Start 12.5kg" in p.notes


def test_weight_hint_bare_start_does_not_match_restart():
    """`\\bstart` should not match in 'Restart' or 'starting'."""
    p = parse_plan("Squat", "Restart 5kg\n3 x 5")
    # No working sets with weight=5; template line "3 x 5" has no hint.
    assert all(s.weight_kg == 0 for s in p.working_sets)


def test_inline_hint_with_trailing_template():
    """'Start with 50kg 4 x 8-10' — hint and template on the same line.
    Should produce 4 working sets at the hint weight, with HIGH-end reps."""
    p = parse_plan(
        "Stiff Leg Deadlift",
        "Warm-up bar x 10\n30 x 5-10\n"
        "Start with 50kg 4 x 8-10\n"
        "Use straps if needed",
    )
    assert len(p.warmup_sets) == 2
    assert len(p.working_sets) == 4
    # No "Progress by" → flat 50kg across all 4 sets
    assert all(s.weight_kg == 50.0 for s in p.working_sets)
    assert all(s.reps == 10 for s in p.working_sets)


def test_inline_hint_bare_start_with_template():
    """'Start 60kg 3 x 5' — bare 'Start' (no at/with) + inline template."""
    p = parse_plan("Bench", "Start 60kg 3 x 5")
    assert len(p.working_sets) == 3
    assert all(s.weight_kg == 60.0 and s.reps == 5 for s in p.working_sets)


def test_inline_hint_with_template_and_progression():
    """Inline 'Start with Nkg M x reps' combined with a 'Progress by' line
    on a big lift should ramp the working sets across the session."""
    p = parse_plan(
        "Stiff Leg Deadlift",
        "Start with 50kg 4 x 8-10\nProgress by 2.5kg",
    )
    assert len(p.working_sets) == 4
    weights = [s.weight_kg for s in p.working_sets]
    assert weights == [50.0, 52.5, 55.0, 57.5]


def test_inline_hint_dumbbell_doubles():
    """Inline hint + template on a dumbbell exercise should double the
    per-hand starting weight."""
    p = parse_plan(
        "Bicep Curl (Dumbbell)",
        "Start at 10kg 3 x 8-12",
    )
    assert len(p.working_sets) == 3
    # 10kg per hand → 20kg total
    assert all(s.weight_kg == 20.0 for s in p.working_sets)
    assert all(s.reps == 12 for s in p.working_sets)


def test_inline_hint_explicit_at_kg_overrides():
    """If the template carries its own '@ Nkg' it takes precedence over
    the just-captured hint."""
    p = parse_plan(
        "Squat",
        "Start at 50kg 3 x 5 @ 55kg",
    )
    assert len(p.working_sets) == 3
    assert all(s.weight_kg == 55.0 for s in p.working_sets)


# ---------------------------------------------------------------------------
# Plan parser — warmups
# ---------------------------------------------------------------------------

def test_warmup_multiline_plus_template():
    plan = ("Warm-up\nBar × 10\n22.5 kg × 5\n25kg x 3\nWorking sets\n5 x 5 @ 35kg\nRIR 3")
    p = parse_plan("Overhead Press (Barbell)", plan)
    assert len(p.warmup_sets) == 3
    assert p.warmup_sets[0].weight_kg == 20 and p.warmup_sets[0].reps == 10
    assert p.warmup_sets[1].weight_kg == 22.5
    assert p.warmup_sets[2].weight_kg == 25
    assert len(p.working_sets) == 5
    assert p.working_sets[0].weight_kg == 35
    assert "RIR 3" in p.notes
    assert "Warm-up" not in p.notes
    assert "Bar × 10" not in p.notes


def test_warmup_with_rep_ranges():
    # v4: warmup rep ranges take LOW end — "35kg x 5-8" → 5 reps.
    plan = "Warm-up bar x 10\n35kg x 5-8\nStart at 50kg\n2-3 x 6-10"
    p = parse_plan("Squat", plan)
    assert len(p.warmup_sets) == 2
    assert p.warmup_sets[0].weight_kg == 20 and p.warmup_sets[0].reps == 10
    assert p.warmup_sets[1].weight_kg == 35 and p.warmup_sets[1].reps == 5
    assert len(p.working_sets) == 3
    assert p.working_sets[0].weight_kg == 50


# ---------------------------------------------------------------------------
# Plan parser — ascending-weight individual working sets (pattern A)
# ---------------------------------------------------------------------------

def test_warmup_indiv_set_no_kg_no_space_after_x():
    """Regression: a warmup line written as '30 x5' (no kg, no space
    between x and reps) parses as a 30kg × 5 indiv warmup, not as a
    template of 30 sets × 5 reps. The exact plan came from a real TC
    Squat workout that was missing its 30kg warmup in Hevy."""
    plan = (
        "Warmup\n\nBar × 10\n30 x5\n47.5kg × 3\n55 kg × 3\n\n"
        "Work\n60 kg × 3\n67.5kg × 3\n75 kg × 3"
    )
    p = parse_plan("Squat", plan)
    assert len(p.warmup_sets) == 4
    assert p.warmup_sets[1].weight_kg == 30 and p.warmup_sets[1].reps == 5
    assert len(p.working_sets) == 3
    assert [s.weight_kg for s in p.working_sets] == [60.0, 67.5, 75.0]


def test_warmup_sticky_indiv_handles_low_magnitude_weight():
    """Once a warmup line is unambiguously '<weight> × <reps>', a
    subsequent low-magnitude (1..10) line like '10 x5' is read as a
    10kg warmup — not as a 10-set template — because the section's
    cadence is already established."""
    plan = "Warmup\nBar × 10\n10 x5\nWork\n60 kg × 3"
    p = parse_plan("Squat", plan)
    assert len(p.warmup_sets) == 2
    assert p.warmup_sets[1].weight_kg == 10 and p.warmup_sets[1].reps == 5
    assert len(p.working_sets) == 1


def test_start_at_hint_still_makes_following_n_x_r_a_template():
    """The sticky-indiv flag must NOT swallow a real template that
    follows a 'Start at N kg' hint, even when the warmup contains
    indiv sets. Regression for the squat-progression tests."""
    plan = "Warm-up\nbar x 10\nStart at 50kg\n3 x 5\nProgress by 5kg"
    p = parse_plan("Squats", plan)
    # Warmup: just Bar × 10. Working: 3 sets from the template, progressing.
    assert len(p.warmup_sets) == 1
    assert [s.weight_kg for s in p.working_sets] == [50.0, 55.0, 60.0]


def test_ascending_working_sets_bench():
    plan = (
        "Warm-up\nBar × 12\n35 kg × 5\n45 kg × 3\n"
        "Working sets\n50kg ×5\n55 kg × 3\n60 kg × 1+"
    )
    p = parse_plan("Bench", plan)
    assert len(p.warmup_sets) == 3
    assert p.warmup_sets[0].weight_kg == 20 and p.warmup_sets[0].reps == 12
    assert p.warmup_sets[1].weight_kg == 35 and p.warmup_sets[1].reps == 5
    assert p.warmup_sets[2].weight_kg == 45 and p.warmup_sets[2].reps == 3
    assert len(p.working_sets) == 3
    assert p.working_sets[0].weight_kg == 50 and p.working_sets[0].reps == 5
    assert p.working_sets[1].weight_kg == 55 and p.working_sets[1].reps == 3
    # "60 kg × 1+" in a working set is a max-effort call; Mark wants reps=12.
    assert p.working_sets[2].weight_kg == 60 and p.working_sets[2].reps == 12


def test_ascending_working_sets_deadlift_work_heading_shortform():
    # Heading "Work sets" (without "ing") must still be recognized.
    plan = (
        "Warm-ups\n45 kg × 5-10\n60 kg × 5\n70 kg × 3\n"
        "Work sets\n80 kg × 5\n87.5 kg × 5\n95 kg × 5+"
    )
    p = parse_plan("Deadlift", plan)
    assert len(p.warmup_sets) == 3
    # v4: warmup rep ranges take LOW end — "45 kg × 5-10" → 5 reps.
    assert p.warmup_sets[0].weight_kg == 45 and p.warmup_sets[0].reps == 5
    assert len(p.working_sets) == 3
    assert p.working_sets[0].weight_kg == 80 and p.working_sets[0].reps == 5
    assert p.working_sets[1].weight_kg == 87.5 and p.working_sets[1].reps == 5
    # "95 kg × 5+" → max-effort → reps=12
    assert p.working_sets[2].weight_kg == 95 and p.working_sets[2].reps == 12


# ---------------------------------------------------------------------------
# Plan parser — dumbbell handling
# ---------------------------------------------------------------------------

def test_dumbbell_template_each_hand_doubles():
    # "per hand" in plan → double the working weight.
    plan = "4 x 10 @ 20kg per hand RIR 2"
    p = parse_plan("Bench Press (Dumbbell)", plan)
    assert len(p.working_sets) == 4
    assert p.working_sets[0].weight_kg == 40


def test_dumbbell_title_alone_doubles():
    plan = "4 x 10 @ 20kg RIR 2"
    p = parse_plan("Hammer Curl (Dumbbell)", plan)
    assert p.working_sets[0].weight_kg == 40


def test_dumbbell_start_hint_doubles():
    plan = "2-3 x 8-10 each leg\nStart with 12.5kg each hand\nRIR 2-3"
    p = parse_plan("Step Up", plan)
    # 3 sets of 10 reps @ 25kg (12.5 per hand × 2)
    assert len(p.working_sets) == 3
    assert p.working_sets[0].weight_kg == 25
    assert p.working_sets[0].reps == 10
    assert "each leg" in p.notes
    assert "each hand" in p.notes


def test_barbell_doesnt_double():
    p = parse_plan("Squat (Barbell)", "5 x 5 @ 60kg")
    assert p.working_sets[0].weight_kg == 60


# ---------------------------------------------------------------------------
# Plan parser — accumulate pattern
# ---------------------------------------------------------------------------

def test_accumulate_chinup():
    plan = "Accumulate 20-30 total reps\nKeep 2 RIR"
    p = parse_plan("Chin-Up", plan)
    assert len(p.working_sets) == 1
    assert p.working_sets[0].weight_kg == 0
    assert p.working_sets[0].reps == 30
    assert "Accumulate 20-30 total reps" in p.notes
    assert "Keep 2 RIR" in p.notes


# ---------------------------------------------------------------------------
# Plan parser — weighted chin-up
# ---------------------------------------------------------------------------

def test_weighted_chinup_template_with_hint():
    plan = "Weighted\nStart at 2.5kg\n3-5 x 2 reps\nRIR 2\nProgress by 2.5kg"
    p = parse_plan("Chin-Up", plan)
    assert len(p.working_sets) == 5
    assert p.working_sets[0].weight_kg == 2.5
    assert p.working_sets[0].reps == 2
    assert "Weighted" in p.notes
    assert "RIR 2" in p.notes


# ---------------------------------------------------------------------------
# Plan parser — "each arm" cable curl
# ---------------------------------------------------------------------------

def test_each_arm_qualifier_in_template():
    plan = ("2-5 x 8-12 each arm\nRIR 2\nCan do this with two arms if you're tight on time.")
    p = parse_plan("Single Arm Cable Curl", plan)
    assert len(p.working_sets) == 5
    assert p.working_sets[0].reps == 12
    assert p.working_sets[0].weight_kg == 0
    assert "each arm" in p.notes or "RIR 2" in p.notes


# ---------------------------------------------------------------------------
# Plan parser — v2: progression within a session for big barbell lifts
# ---------------------------------------------------------------------------

def test_progression_squat_range_hint():
    # Mark's core example: range "2.5-5kg" + big lift (Squat) → ascend by 5kg.
    plan = (
        "Warm-up bar x 10\n35kg x 5-8\n"
        "Start at 50kg\n2-3 x 6-10\nRIR 2-3\nProgress by 2.5-5kg"
    )
    p = parse_plan("Squat", plan)
    assert len(p.working_sets) == 3
    assert [s.weight_kg for s in p.working_sets] == [50, 55, 60]
    assert all(s.reps == 10 for s in p.working_sets)


def test_progression_single_value_hint_big_lift_ascends():
    # v4: "Progress by 2.5kg" (single value) still ascends for big lifts.
    # Bench Press counts as a big lift — Mark called this out explicitly.
    plan = "Warm-up\nBar x 10\nStart at 45kg\n4-5 x 10-12\nRIR 2-3\nProgress by 2.5kg"
    p = parse_plan("Bench", plan)
    assert len(p.working_sets) == 5
    assert [s.weight_kg for s in p.working_sets] == [45, 47.5, 50, 52.5, 55]


def test_progression_single_value_hint_non_big_lift_flat():
    # Single-value hint on an accessory → stay flat (not a big lift).
    plan = "Start at 10kg\n3 x 10\nRIR 2\nProgress by 2kg"
    p = parse_plan("Lateral Raise (Dumbbell)", plan)
    # Dumbbell doubles: 10 × 2 = 20kg, flat.
    assert all(s.weight_kg == 20 for s in p.working_sets)


def test_progression_not_applied_to_dumbbell_row():
    # Not a big barbell lift → stay flat even if hint is a range.
    plan = "4-5 x 10-15 each arm\nRIR 2\nStart at 10kg\nProgress by 2.5-5kg"
    p = parse_plan("Dumbbell Row", plan)
    # 10kg per hand × 2 for dumbbell = 20kg total, flat across all sets.
    assert len(p.working_sets) == 5
    assert all(s.weight_kg == 20 for s in p.working_sets)


def test_template_line_preserved_in_notes():
    plan = "Start at 50kg\n2-3 x 6-10\nRIR 2-3\nProgress by 2.5-5kg"
    p = parse_plan("Squat", plan)
    # The template prescription should be visible in notes.
    assert "2-3 x 6-10" in p.notes


# ---------------------------------------------------------------------------
# Plan parser — N+ amrap handling
# ---------------------------------------------------------------------------

def test_amrap_warmup_keeps_literal_reps():
    # "+" inside a warmup set is not a max-effort cue.
    plan = "Warm-up\nBar × 10+\nWorking\n60kg × 5"
    p = parse_plan("Bench", plan)
    assert len(p.warmup_sets) == 1
    assert p.warmup_sets[0].reps == 10


def test_amrap_working_becomes_12_reps():
    plan = "Working\n60kg × 5+"
    p = parse_plan("Bench", plan)
    assert p.working_sets[0].reps == 12


# ---------------------------------------------------------------------------
# Rest-seconds heuristic
# ---------------------------------------------------------------------------

def test_rest_seconds_deadlift_150():
    assert _rest_seconds_for("Deadlift (Barbell)") == 150
    assert _rest_seconds_for("Romanian Deadlift (Barbell)") == 150


def test_rest_seconds_squat_120():
    assert _rest_seconds_for("Squat (Barbell)") == 120
    assert _rest_seconds_for("Front Squat (Barbell)") == 120


def test_rest_seconds_other_big_lifts_drop_to_90():
    # v3 rule: only Deadlift and Squat get extended rest.
    assert _rest_seconds_for("Bench Press (Barbell)") == 90
    assert _rest_seconds_for("Overhead Press (Barbell)") == 90


def test_rest_seconds_accessories_and_machines():
    assert _rest_seconds_for("Bicep Curl (Dumbbell)") == 90
    assert _rest_seconds_for("Chest Fly (Machine)") == 90
    assert _rest_seconds_for("Squat (Smith Machine)") == 90  # machine overrides
    assert _rest_seconds_for("Bench Dip") == 90
    assert _rest_seconds_for("Deadlift (Dumbbell)") == 90  # dumbbell overrides
    assert _rest_seconds_for("Chin Up") == 90
    assert _rest_seconds_for(None) == 90


# ---------------------------------------------------------------------------
# Plan parser — empty and misc
# ---------------------------------------------------------------------------

def test_empty_plan():
    p = parse_plan("Rest", "")
    assert p.warmup_sets == []
    assert p.working_sets == []


# ---------------------------------------------------------------------------
# is_dumbbell
# ---------------------------------------------------------------------------

def test_is_dumbbell_by_title():
    assert is_dumbbell("Bench Press (Dumbbell)", "")
    assert is_dumbbell("Hammer Curl (Dumbbell)", "")


def test_is_dumbbell_by_plan_text():
    assert is_dumbbell("Curl", "20kg per hand")
    assert is_dumbbell("Row", "use DBs, each arm")


def test_is_dumbbell_kettlebell_title():
    # Kettlebells share the per-hand convention with dumbbells.
    assert is_dumbbell("One Arm Kettlebell Press", "")
    assert is_dumbbell("Kettlebell Swing", "3 x 10")


def test_is_dumbbell_barbell_false():
    assert not is_dumbbell("Bench Press (Barbell)", "5 x 5 @ 60kg")


# ---------------------------------------------------------------------------
# ExerciseResolver
# ---------------------------------------------------------------------------

def _mk_resolver():
    history = [
        {"id": "79D0BB3A", "title": "Bench Press (Barbell)", "count": 42,
         "last_seen": "2026-04-20T08:01:22+00:00", "is_custom": False},
        {"id": "F1E57334", "title": "Dumbbell Row", "count": 11,
         "last_seen": "2026-04-01T00:00:00+00:00", "is_custom": False},
        {"id": "4fcd1481-ec99-4050-8ef5-3fb5da2d46bb", "title": "Dumbbell Row",
         "count": 1, "last_seen": "2025-05-01T00:00:00+00:00", "is_custom": True},
        {"id": "7B8D84E8", "title": "Overhead Press (Barbell)", "count": 20,
         "last_seen": "2026-04-22T08:25:48+00:00", "is_custom": False},
        {"id": "cf3ed740-7942-4759-90b8-d6af0366a0e8", "title": "Seal Row", "count": 17,
         "last_seen": "2025-11-25T00:00:00+00:00", "is_custom": True},
    ]
    templates = [
        {"id": "79D0BB3A", "title": "Bench Press (Barbell)", "is_custom": False,
         "equipment": "barbell", "muscle": "chest"},
        {"id": "F1E57334", "title": "Dumbbell Row", "is_custom": False,
         "equipment": "dumbbell", "muscle": "upper_back"},
        {"id": "4fcd1481-ec99-4050-8ef5-3fb5da2d46bb", "title": "Dumbbell Row",
         "is_custom": True, "equipment": "none", "muscle": "other"},
        {"id": "7B8D84E8", "title": "Overhead Press (Barbell)", "is_custom": False,
         "equipment": "barbell", "muscle": "shoulders"},
        {"id": "cf3ed740-7942-4759-90b8-d6af0366a0e8", "title": "Seal Row",
         "is_custom": True, "equipment": "barbell", "muscle": "upper_back"},
        {"id": "XYZ999", "title": "Bent Over Row (Barbell)", "is_custom": False,
         "equipment": "barbell", "muscle": "upper_back"},
    ]
    return ExerciseResolver(history, templates)


def test_resolver_exact_history():
    r = _mk_resolver()
    tid, ttitle, conf, _ = r.resolve("Bench Press (Barbell)")
    assert tid == "79D0BB3A"
    assert conf == "history"


def test_resolver_prefers_most_logged_on_ambiguous_title():
    r = _mk_resolver()
    tid, _, _, _ = r.resolve("Dumbbell Row")
    assert tid == "F1E57334"


def test_resolver_short_query_chooses_history():
    r = _mk_resolver()
    # "Bench" should resolve to Bench Press (Barbell), not a random catalog item.
    tid, _, conf, _ = r.resolve("Bench")
    assert tid == "79D0BB3A"
    assert conf.startswith("history")


def test_resolver_issa_prefix_stripped():
    r = _mk_resolver()
    # Real-world: "ISSA Exercise Library Dumbbell Biceps Curl" → should still
    # resolve to a Dumbbell Row entry if that's all that matches, but most
    # importantly the prefix should be normalized away. With our fixture
    # there's no biceps-curl entry, so we expect either Dumbbell Row (token
    # overlap on "dumbbell") or no-match.
    tid, _, _, _ = r.resolve("ISSA Exercise Library Dumbbell Biceps Curl")
    assert tid in {"F1E57334", "4fcd1481-ec99-4050-8ef5-3fb5da2d46bb", None}


def test_resolver_no_match():
    r = _mk_resolver()
    tid, _, conf, _ = r.resolve("Kettlebell Turkish Getup")
    assert tid is None
    assert conf == "no-match"


def test_big_lift_regex_matches_plurals():
    from truecoach_to_hevy import _BIG_LIFT_RE
    assert _BIG_LIFT_RE.search("Squats")
    assert _BIG_LIFT_RE.search("Deadlifts")
    assert _BIG_LIFT_RE.search("Squat")
    assert _BIG_LIFT_RE.search("Deadlift")


def test_inline_preprocessor_splits_squat_run_on():
    from truecoach_to_hevy import _split_inline_to_lines
    inline = ("Warm-up bar x 10 35kg x 5-8 Start at 52.5kg 3 x 6-10 "
              "RIR 2-3 Progress by 2.5-5kg")
    out = _split_inline_to_lines(inline)
    lines = [ln for ln in out.split("\n") if ln.strip()]
    assert "Warm-up" in lines
    assert "bar x 10" in lines
    assert "35kg x 5-8" in lines
    assert "Start at 52.5kg" in lines
    assert "3 x 6-10" in lines
    assert any(ln.startswith("RIR") for ln in lines)
    assert any(ln.startswith("Progress by") for ln in lines)


def test_inline_preprocessor_leaves_already_line_broken_input():
    from truecoach_to_hevy import _split_inline_to_lines
    text = "line1\nline2\nline3"
    assert _split_inline_to_lines(text) == text


def test_squat_progression_applies_with_plural_title():
    """Regression: TC sometimes uses 'Squats' (plural) and progression
    should still trigger via _BIG_LIFT_RE."""
    from truecoach_to_hevy import build_hevy_exercise
    r = _mk_resolver()
    plan = ("Warm-up\nbar x 10\nStart at 50kg\n3 x 5\n"
            "Progress by 5kg")
    out = build_hevy_exercise("Squats", plan, r)
    weights = [s["weight_kg"] for s in out["sets"] if s["type"] == "normal"]
    assert weights == [50.0, 55.0, 60.0]


def test_inline_squat_full_pipeline_applies_progression():
    from truecoach_to_hevy import build_hevy_exercise
    r = _mk_resolver()
    plan = ("Warm-up bar x 10 35kg x 5-8 Start at 52.5kg 3 x 6-10 "
            "RIR 2-3 Progress by 2.5-5kg")
    out = build_hevy_exercise("Squat", plan, r)
    weights = [s["weight_kg"] for s in out["sets"] if s["type"] == "normal"]
    assert weights == [52.5, 57.5, 62.5]


def test_inline_preprocessor_recognises_short_work_heading():
    """Regression: inline 'Work' heading (without 'ing') was missed,
    causing all working sets to be tagged as warmup."""
    from truecoach_to_hevy import _split_inline_to_lines
    inline = "Bar × 10 32.5 kg × 8 42.5 kg × 5 Work 47.5 kg × 5"
    out = _split_inline_to_lines(inline)
    lines = [ln.strip() for ln in out.split("\n") if ln.strip()]
    assert "Work" in lines


def test_inline_bench_with_work_heading_separates_warmup_from_working():
    from truecoach_to_hevy import build_hevy_exercise
    r = _mk_resolver()
    plan = ("Warm-Up Bar × 10 32.5 kg × 8 42.5 kg × 5 Work "
            "47.5 kg × 5 52.5 kg × 5 57.5 kg × 5 +")
    out = build_hevy_exercise("Bench", plan, r)
    warmup = [s for s in out["sets"] if s["type"] == "warmup"]
    working = [s for s in out["sets"] if s["type"] == "normal"]
    assert len(warmup) == 3
    assert len(working) == 3
    assert [s["weight_kg"] for s in working] == [47.5, 52.5, 57.5]


def test_inline_pushup_template_uses_high_end_reps():
    from truecoach_to_hevy import build_hevy_exercise
    r = _mk_resolver()
    plan = "Bodyweight 3-5 x 6-12 reps RIR 2"
    out = build_hevy_exercise("Push Up", plan, r)
    sets = [s for s in out["sets"] if s["type"] == "normal"]
    assert len(sets) == 5
    assert all(s["reps"] == 12 for s in sets)


def test_seated_leg_curl_template_uses_high_end_reps():
    from truecoach_to_hevy import build_hevy_exercise
    r = _mk_resolver()
    out = build_hevy_exercise("Seated Leg Curl", "3-4 x 8-12 RIR 1-2", r)
    sets = [s for s in out["sets"] if s["type"] == "normal"]
    assert len(sets) == 4
    assert all(s["reps"] == 12 for s in sets)


def test_resolver_prefers_builtin_over_custom_in_catalog():
    """Tie-break: when both a built-in and a Mark-custom template share the
    same normalised form, the built-in should win the catalog dict slot."""
    templates = [
        {"id": "CUSTOM",  "title": "Bench Press (Barbell)", "is_custom": True},
        {"id": "BUILTIN", "title": "Bench Press (Barbell)", "is_custom": False},
    ]
    r = ExerciseResolver([], templates)
    tid, _, conf, _ = r.resolve("Bench Press (Barbell)")
    assert tid == "BUILTIN"
    assert conf == "catalog-exact"


def test_resolver_acronym_rdl_expands_to_romanian_deadlift():
    history = [
        {"id": "RD_DB", "title": "Romanian Deadlift (Dumbbell)", "count": 5,
         "last_seen": "2026-04-22T00:00:00+00:00", "is_custom": False},
    ]
    templates = [
        {"id": "RD_DB", "title": "Romanian Deadlift (Dumbbell)",
         "is_custom": False, "equipment": "dumbbell", "muscle": "hamstrings"},
    ]
    r = ExerciseResolver(history, templates)
    tid, ttitle, conf, _ = r.resolve("RDL")
    assert "Romanian Deadlift" in ttitle


def test_resolver_acronym_ohp_expands_to_overhead_press():
    r = _mk_resolver()
    tid, ttitle, _, _ = r.resolve("OHP")
    assert "Overhead Press" in ttitle


def test_resolver_acronym_sl_rdl_expands_compound():
    """SL RDL should expand both acronyms ('single leg' + 'romanian deadlift')."""
    templates = [
        {"id": "SLRDB", "title": "Single Leg Romanian Deadlift (Dumbbell)",
         "is_custom": False, "equipment": "dumbbell", "muscle": "hamstrings"},
    ]
    r = ExerciseResolver([], templates)
    tid, ttitle, _, _ = r.resolve("SL RDL")
    assert ttitle == "Single Leg Romanian Deadlift (Dumbbell)"


def test_resolver_approved_override_wins():
    history = [
        {"id": "WRONG", "title": "Some Other Exercise", "count": 50,
         "last_seen": "2026-04-22T00:00:00+00:00", "is_custom": False},
    ]
    templates = []
    overrides = {
        "stiff leg deadlift": {
            "exercise_template_id": "RIGHT",
            "resolved_title": "Straight Leg Deadlift",
        }
    }
    r = ExerciseResolver(history, templates, overrides=overrides)
    tid, ttitle, conf, _ = r.resolve("Stiff Leg Deadlift")
    assert tid == "RIGHT"
    assert ttitle == "Straight Leg Deadlift"
    assert conf == "approved"


def test_resolver_override_key_uses_raw_title_not_synonyms():
    """Override keys are the raw lowercase TC title (no synonym subs).

    Without this, a key like 'stiff leg deadlift' would never hit because
    _normalize rewrites it to 'straight leg deadlift'."""
    overrides = {
        "stiff leg deadlift": {
            "exercise_template_id": "FROM_OVERRIDE",
            "resolved_title": "Straight Leg Deadlift",
        }
    }
    r = ExerciseResolver([], [], overrides=overrides)
    tid, _, conf, _ = r.resolve("Stiff Leg Deadlift")
    assert tid == "FROM_OVERRIDE"
    assert conf == "approved"


def test_build_hevy_override_applies_notes_prefix():
    from truecoach_to_hevy import build_hevy_exercise
    overrides = {
        "standing single arm db press": {
            "exercise_template_id": "OHPDB",
            "resolved_title": "Overhead Press (Dumbbell)",
            "notes_prefix": "Single arm",
        }
    }
    r = ExerciseResolver([], [], overrides=overrides)
    out = build_hevy_exercise("Standing Single Arm DB Press",
                              "5 x 8\n15kg per hand", r)
    assert out["resolved_title"] == "Overhead Press (Dumbbell)"
    assert out["confidence"] == "approved"
    # notes_prefix prepended; original parsed notes follow
    assert out["notes"].startswith("Single arm")


def test_resolver_normalize_stiff_leg_works_at_start_of_string():
    """Word-boundary regex must fire even with no leading space."""
    norm = ExerciseResolver._normalize("Stiff Leg Deadlift")
    assert "straight leg" in norm
    assert "stiff leg" not in norm


def test_resolver_candidate_promotes_to_weighted_chinup():
    """Regression guard: Chin-Up + 'Weighted' plan still upgrades."""
    history = [
        {"id": "BASE_CHINUP", "title": "Chin Up", "count": 80,
         "last_seen": "2026-04-20T00:00:00+00:00", "is_custom": False},
    ]
    templates = [
        {"id": "BASE_CHINUP",  "title": "Chin Up", "is_custom": False},
        {"id": "WEIGHTED",     "title": "Chin Up (Weighted)", "is_custom": False},
    ]
    r = ExerciseResolver(history, templates)
    from truecoach_to_hevy import build_hevy_exercise
    out = build_hevy_exercise("Chin-Up", "Weighted\nStart at 2.5kg\n3 x 5", r)
    assert out["exercise_template_id"] == "WEIGHTED"


def test_build_hevy_single_leg_rdl_with_dumbbell_plan_routes_correctly():
    from truecoach_to_hevy import build_hevy_exercise
    templates = [
        {"id": "SLRDB", "title": "Single Leg Romanian Deadlift (Dumbbell)",
         "is_custom": False, "equipment": "dumbbell", "muscle": "hamstrings"},
        {"id": "SLRBB", "title": "Single Leg Romanian Deadlift (Barbell)",
         "is_custom": False, "equipment": "barbell", "muscle": "hamstrings"},
    ]
    r = ExerciseResolver([], templates)
    out = build_hevy_exercise("Single Leg RDL", "12.5kg each hand\n3 x 8", r)
    assert out["resolved_title"] == "Single Leg Romanian Deadlift (Dumbbell)"
    assert out["confidence"] == "catalog-exact"
    assert out["exercise_template_id"] == "SLRDB"


def test_build_hevy_weighted_chinup_routes_to_weighted_template():
    # When plan contains "Weighted", the builder should append "Weighted" to
    # the title before resolving — so Chin-Up → Chin Up (Weighted).
    from truecoach_to_hevy import build_hevy_exercise
    history = [
        {"id": "29083183", "title": "Chin Up", "count": 80,
         "last_seen": "2026-04-20T08:01:22+00:00", "is_custom": False},
    ]
    templates = [
        {"id": "29083183", "title": "Chin Up", "is_custom": False,
         "equipment": "none", "muscle": "upper_back"},
        {"id": "023943F1", "title": "Chin Up (Weighted)", "is_custom": False,
         "equipment": "none", "muscle": "upper_back"},
        {"id": "D23C609B", "title": "Chin Up (Assisted)", "is_custom": False,
         "equipment": "none", "muscle": "upper_back"},
    ]
    r = ExerciseResolver(history, templates)
    plan = "Weighted\nStart at 2.5kg\n3-5 x 2 reps\nRIR 2\nProgress by 2.5kg"
    result = build_hevy_exercise("Chin-Up", plan, r)
    assert result["exercise_template_id"] == "023943F1"
    assert result["resolved_title"] == "Chin Up (Weighted)"


def test_build_hevy_unweighted_chinup_stays_plain():
    from truecoach_to_hevy import build_hevy_exercise
    history = [
        {"id": "29083183", "title": "Chin Up", "count": 80,
         "last_seen": "2026-04-20T08:01:22+00:00", "is_custom": False},
    ]
    templates = [
        {"id": "29083183", "title": "Chin Up", "is_custom": False,
         "equipment": "none", "muscle": "upper_back"},
        {"id": "023943F1", "title": "Chin Up (Weighted)", "is_custom": False,
         "equipment": "none", "muscle": "upper_back"},
    ]
    r = ExerciseResolver(history, templates)
    plan = "Accumulate 20-30 total reps\nKeep 2 RIR"
    result = build_hevy_exercise("Chin-Up", plan, r)
    assert result["exercise_template_id"] == "29083183"


def test_build_hevy_step_up_routes_to_dumbbell_variant_when_loaded():
    # v4: Step Up with "each hand" weight hint → prefer "Dumbbell Step Up".
    from truecoach_to_hevy import build_hevy_exercise
    history = [
        {"id": "128A2381", "title": "Step Up", "count": 5,
         "last_seen": "2026-04-10T08:01:22+00:00", "is_custom": False},
        {"id": "BF6ECE89", "title": "Dumbbell Step Up", "count": 3,
         "last_seen": "2026-04-15T08:01:22+00:00", "is_custom": False},
    ]
    templates = [
        {"id": "128A2381", "title": "Step Up", "is_custom": False},
        {"id": "BF6ECE89", "title": "Dumbbell Step Up", "is_custom": False},
    ]
    r = ExerciseResolver(history, templates)
    plan = "2-3 x 8-10 each leg\nStart with 12.5kg each hand\nRIR 2-3"
    result = build_hevy_exercise("Step Up", plan, r)
    assert result["exercise_template_id"] == "BF6ECE89"
    assert result["resolved_title"] == "Dumbbell Step Up"


def test_build_hevy_step_up_stays_plain_when_no_load_hint():
    # No weight hint and no "each hand" → plain Step Up.
    from truecoach_to_hevy import build_hevy_exercise
    history = [
        {"id": "128A2381", "title": "Step Up", "count": 5,
         "last_seen": "2026-04-10T08:01:22+00:00", "is_custom": False},
        {"id": "BF6ECE89", "title": "Dumbbell Step Up", "count": 3,
         "last_seen": "2026-04-15T08:01:22+00:00", "is_custom": False},
    ]
    templates = [
        {"id": "128A2381", "title": "Step Up", "is_custom": False},
        {"id": "BF6ECE89", "title": "Dumbbell Step Up", "is_custom": False},
    ]
    r = ExerciseResolver(history, templates)
    plan = "3 x 10\nRIR 2-3"
    result = build_hevy_exercise("Step Up", plan, r)
    assert result["exercise_template_id"] == "128A2381"


# ---------------------------------------------------------------------------
# Warmup rep-range behavior — v4: take LOW end on warmup ranges
# ---------------------------------------------------------------------------

def test_warmup_rep_range_takes_low_end():
    # "30 x 5-10" in a warmup → 5 reps, not 10.
    plan = "Warm-up\nBar x 10\n30 x 5-10\nStart at 45kg\n4-5 x 10-12"
    p = parse_plan("Bench", plan)
    # Two warmups: bar (10 reps literal) then 30kg (5 reps, low end).
    assert len(p.warmup_sets) == 2
    assert p.warmup_sets[1].weight_kg == 30
    assert p.warmup_sets[1].reps == 5


def test_warmup_rep_range_takes_low_end_deadlift():
    plan = "Warm-ups\n45 kg × 5-10\n60 kg × 5\nWork sets\n80 kg × 5"
    p = parse_plan("Deadlift", plan)
    assert p.warmup_sets[0].weight_kg == 45
    assert p.warmup_sets[0].reps == 5  # low end of 5-10
    assert p.warmup_sets[1].reps == 5


def test_working_rep_range_still_takes_high_end():
    # Regression check: working sets keep HIGH end.
    plan = "Working sets\n50kg x 6-10"
    p = parse_plan("Bench", plan)
    assert p.working_sets[0].reps == 10


# ---------------------------------------------------------------------------
# Plan parser — "total reps" without the "Accumulate" keyword (Fix b)
# ---------------------------------------------------------------------------

def test_total_bodyweight_reps_without_accumulate_keyword():
    """Regression: Cillian sometimes writes the total-reps target as
    "12-20 total bodyweight reps" without the leading "Accumulate".
    The parser must still emit a working set (HIGH end of the range)
    so the exercise lands in Hevy with at least one set."""
    plan = "12-20 total bodyweight reps\nRIR 2-3"
    p = parse_plan("Chin-Up", plan)
    assert len(p.working_sets) == 1
    assert p.working_sets[0].weight_kg == 0
    assert p.working_sets[0].reps == 20
    # The directive line is preserved in notes for the human reader.
    assert "12-20 total bodyweight reps" in p.notes
    assert "RIR 2-3" in p.notes


def test_total_reps_without_bodyweight_qualifier():
    """Plain 'N-M total reps' (no 'bodyweight') should also match."""
    plan = "15-25 total reps"
    p = parse_plan("Chin-Up", plan)
    assert len(p.working_sets) == 1
    assert p.working_sets[0].reps == 25


def test_total_reps_with_bw_abbreviation():
    """'bw' is accepted as a synonym for 'bodyweight'."""
    plan = "10-15 total bw reps"
    p = parse_plan("Push-Up", plan)
    assert len(p.working_sets) == 1
    assert p.working_sets[0].reps == 15


def test_total_reps_line_does_not_eat_template_set_line():
    """'3 x 8-12 total reps' is a template line (3 sets of 8-12 reps,
    annotated 'total reps'). The line-start anchor on the new regex
    must NOT eat this — the existing _TEMPLATE_SET_RE path should fire."""
    plan = "3 x 8-12 total reps"
    p = parse_plan("Push-Up", plan)
    # Template parse: 3 sets at high-end reps (12).
    assert len(p.working_sets) == 3
    assert all(s.reps == 12 for s in p.working_sets)


# ---------------------------------------------------------------------------
# build_hevy_exercise — placeholder set fallback (Fix a)
# ---------------------------------------------------------------------------

def test_build_hevy_empty_sets_gets_placeholder():
    """When no set pattern matches, build_hevy_exercise must still emit
    at least one set — Hevy silently drops exercises with sets:[]."""
    from truecoach_to_hevy import build_hevy_exercise
    r = _mk_resolver()
    # A plan that intentionally matches no set pattern. (Plain prose.)
    out = build_hevy_exercise("Bench", "Focus on form today.", r)
    assert len(out["sets"]) >= 1, "must never ship empty sets"
    s = out["sets"][0]
    assert s["type"] == "normal"
    assert s["weight_kg"] is None
    assert s["reps"] is None
    # Warning surfaces the fallback so callers can flag it.
    assert any("placeholder" in w.lower() for w in out["warnings"])


def test_build_hevy_existing_sets_unchanged_by_fallback():
    """Fallback must NOT activate when the parser produced real sets."""
    from truecoach_to_hevy import build_hevy_exercise
    r = _mk_resolver()
    out = build_hevy_exercise("Bench", "3 x 5 @ 60kg", r)
    assert len(out["sets"]) == 3
    assert all(s["weight_kg"] == 60.0 for s in out["sets"])
    # No placeholder warning when the parser found real sets.
    assert not any("placeholder" in w.lower() for w in out["warnings"])


def test_build_hevy_chinup_bodyweight_total_reps_full_pipeline():
    """End-to-end: the failing Monday Chin-Up plan now produces a real
    set (Fix b path), not a placeholder."""
    from truecoach_to_hevy import build_hevy_exercise
    r = _mk_resolver()
    out = build_hevy_exercise(
        "Chin-Up", "12-20 total bodyweight reps\nRIR 2-3", r,
    )
    assert len(out["sets"]) == 1
    assert out["sets"][0]["weight_kg"] == 0
    assert out["sets"][0]["reps"] == 20
    # No placeholder warning — we actually parsed a real set.
    assert not any("placeholder" in w.lower() for w in out["warnings"])


# ---------------------------------------------------------------------------
# Dangling-decimal typo — Cillian types the point but drops the 5
# ("12 . kg" means 12.5kg). Before this was handled the whole set line
# failed to match and was silently dropped. (Mark, 2026-08-21)
# ---------------------------------------------------------------------------

def test_dangling_decimal_indiv_set_spaced():
    p = parse_plan("Bench Press", "12 . kg x 8")
    assert [s.weight_kg for s in p.working_sets] == [12.5]


def test_dangling_decimal_indiv_set_variants_all_agree():
    for text in ("12. kg x 8", "12.kg x 8", "12 .kg x 8", "12 . kg x 8"):
        p = parse_plan("Bench Press", text)
        assert [s.weight_kg for s in p.working_sets] == [12.5], text


def test_dangling_decimal_in_start_hint():
    p = parse_plan("Row", "Start with 22 . kg\n4 x 8-10")
    assert len(p.working_sets) == 4
    assert all(s.weight_kg == 22.5 for s in p.working_sets)


def test_dangling_decimal_in_template_at_weight():
    p = parse_plan("Press", "4 x 10 @ 17 . kg")
    assert [s.weight_kg for s in p.working_sets] == [17.5] * 4


def test_dangling_decimal_in_progress_by():
    # Progression is gated on big barbell lifts, so use Squat.
    p = parse_plan("Squat", "Start at 60kg\n3 x 5\nProgress by 2 . kg")
    assert [s.weight_kg for s in p.working_sets] == [60.0, 62.5, 65.0]


def test_dangling_decimal_warmup_keeps_warmup_type():
    # NB: a blank line alone does not close a warmup section — it takes a
    # "Working sets" heading (or a template line). Same shape as the real
    # TC deadlift plans.
    p = parse_plan("Bench Press",
                   "Warm-up\n12 . kg x 10\n\nWorking sets\n60kg x 5")
    assert [s.weight_kg for s in p.warmup_sets] == [12.5]
    assert p.warmup_sets[0].type == "warmup"
    assert [s.weight_kg for s in p.working_sets] == [60.0]


def test_ordinary_decimals_unaffected():
    p = parse_plan("Deadlift", "102.5kg x 3")
    assert [s.weight_kg for s in p.working_sets] == [102.5]


def test_integer_weights_unaffected():
    p = parse_plan("Deadlift", "100kg x 3")
    assert [s.weight_kg for s in p.working_sets] == [100.0]


def test_weight_token_cannot_span_a_newline():
    """Regression guard: the optional-space allowance around the decimal
    point is [ \\t], never \\s. With \\s a weight at the end of one line
    could swallow the start of the next."""
    import re
    from truecoach_to_hevy import _WEIGHT_NUM
    m = re.match(_WEIGHT_NUM, "12\n.5")
    assert m is not None and m.group(0) == "12"


def test_dangling_decimal_does_not_eat_following_sentence():
    p = parse_plan("Row", "20kg x 10. Then rest 2 mins")
    assert [s.weight_kg for s in p.working_sets] == [20.0]
    assert "Then rest 2 mins" in p.notes


def test_amrap_plus_still_maps_to_twelve_reps():
    """Deliberate, not a bug: Mark wants a '+' working set to show a
    sensible 12-rep goal in the Hevy UI. Locked in so it doesn't get
    'fixed' by a future reader. (Confirmed 2026-08-21.)"""
    p = parse_plan("Deadlift", "Work sets\n112.5 kg x 1+")
    assert [s.reps for s in p.working_sets] == [12]


# ---------------------------------------------------------------------------
# per_hand overrides — TC plans are per hand, Hevy stores the two-hand total
# ---------------------------------------------------------------------------

def _per_hand_resolver(per_hand=True):
    overrides = {
        "step up": {
            "exercise_template_id": "BF6ECE89",
            "resolved_title": "Dumbbell Step Up",
            "notes_prefix": None,
            "per_hand": per_hand,
        }
    }
    return ExerciseResolver([], [], overrides=overrides)


def test_per_hand_override_doubles_working_weights():
    from truecoach_to_hevy import build_hevy_exercise
    out = build_hevy_exercise("Step Up", "Start with 15kg\n3 x 8-12",
                              _per_hand_resolver())
    assert out["exercise_template_id"] == "BF6ECE89"
    assert [s["weight_kg"] for s in out["sets"]] == [30.0] * 3


def test_per_hand_override_does_not_double_twice():
    """Plan already says 'each hand', so parse_plan doubled it. The
    override must not double it again."""
    from truecoach_to_hevy import build_hevy_exercise
    out = build_hevy_exercise("Step Up",
                              "Start with 15kg each hand\n3 x 8-12",
                              _per_hand_resolver())
    assert [s["weight_kg"] for s in out["sets"]] == [30.0] * 3


def test_per_hand_override_leaves_bodyweight_alone():
    from truecoach_to_hevy import build_hevy_exercise
    out = build_hevy_exercise("Step Up", "3 x 8-12\nRIR 2",
                              _per_hand_resolver())
    assert [s["weight_kg"] for s in out["sets"]] == [0.0] * 3


def test_per_hand_override_leaves_warmups_alone():
    """Mirrors _maybe_double, which only scales working sets."""
    from truecoach_to_hevy import build_hevy_exercise
    out = build_hevy_exercise(
        "Step Up", "Warm-up\n10kg x 10\n\nWorking sets\n3 x 8 @ 15kg",
        _per_hand_resolver())
    weights = [s["weight_kg"] for s in out["sets"]]
    assert weights[0] == 10.0          # warmup untouched
    assert weights[1:] == [30.0] * 3   # working sets doubled


def test_override_without_per_hand_flag_does_not_double():
    """Back-compat: the pre-existing overrides have no per_hand key."""
    from truecoach_to_hevy import build_hevy_exercise
    r = ExerciseResolver([], [], overrides={
        "step up": {
            "exercise_template_id": "BF6ECE89",
            "resolved_title": "Dumbbell Step Up",
        }
    })
    out = build_hevy_exercise("Step Up", "Start with 15kg\n3 x 8-12", r)
    assert [s["weight_kg"] for s in out["sets"]] == [15.0] * 3


def test_per_hand_false_does_not_double():
    from truecoach_to_hevy import build_hevy_exercise
    out = build_hevy_exercise("Step Up", "Start with 15kg\n3 x 8-12",
                              _per_hand_resolver(per_hand=False))
    assert [s["weight_kg"] for s in out["sets"]] == [15.0] * 3


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys, traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    sys.exit(0 if failed == 0 else 1)
