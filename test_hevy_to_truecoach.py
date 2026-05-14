"""
Tests for hevy_to_truecoach (rev2: log everything, blank-line separator).
Run: python3 test_hevy_to_truecoach.py
"""

from hevy_to_truecoach import (
    translate_workout,
    translate_exercise,
    parse_warmup_count,
    _format_rir,
    _format_weight,
    _format_set,
    _is_dumbbell,
    _warmup_boundary,
    _warmup_boundary_from_types,
    _workout_has_warmup_markers,
)


# -------- small helpers --------

def test_format_weight_integer():
    assert _format_weight(45.0) == "45kg"
    assert _format_weight(100) == "100kg"

def test_format_weight_half():
    assert _format_weight(47.5) == "47.5kg"
    assert _format_weight(2.5) == "2.5kg"

def test_rir_integer_rpe():
    assert _format_rir(7) == "rir 3"
    assert _format_rir(10) == "rir 0"
    assert _format_rir(8.0) == "rir 2"

def test_rir_half_rpe():
    assert _format_rir(6.5) == "rir 3-4"
    assert _format_rir(7.5) == "rir 2-3"
    assert _format_rir(8.5) == "rir 1-2"
    assert _format_rir(9.5) == "rir 0-1"

def test_rir_none():
    assert _format_rir(None) is None


# -------- verification against real 2026-04-20 data --------
# Pulled from Hevy API for workout "PT Session Squat & Press"

BENCH_2026_04_20 = {
    "title": "Bench Press (Barbell)",
    "equipment": "barbell",
    "sets": [
        {"type": "normal", "weight_kg": 20,   "reps": 10, "rpe": 6},
        {"type": "normal", "weight_kg": 30,   "reps": 5,  "rpe": 6},
        {"type": "normal", "weight_kg": 45,   "reps": 10, "rpe": 7},
        {"type": "normal", "weight_kg": 47.5, "reps": 10, "rpe": 7.5},
        {"type": "normal", "weight_kg": 50,   "reps": 10, "rpe": 8.5},
        {"type": "normal", "weight_kg": 52.5, "reps": 5,  "rpe": 9},
        {"type": "normal", "weight_kg": 52.5, "reps": 7,  "rpe": 9.5},
    ],
}

EXPECTED_BENCH = (
    "Bench Press (Barbell)\n"
    "20kg x 10, rir 4\n"
    "30kg x 5, rir 4\n"
    "\n"
    "45kg x 10, rir 3\n"
    "47.5kg x 10, rir 2-3\n"
    "50kg x 10, rir 1-2\n"
    "52.5kg x 5, rir 1\n"
    "52.5kg x 7, rir 0-1"
)

def test_bench_full_output():
    assert translate_exercise(BENCH_2026_04_20).render() == EXPECTED_BENCH


SQUAT_2026_04_20 = {
    "title": "Squat (Barbell)",
    "equipment": "barbell",
    "sets": [
        {"type": "normal", "weight_kg": 20,   "reps": 10, "rpe": 6},
        {"type": "normal", "weight_kg": 35,   "reps": 5,  "rpe": 6},
        {"type": "normal", "weight_kg": 45,   "reps": 5,  "rpe": 6},
        {"type": "normal", "weight_kg": 57.5, "reps": 3,  "rpe": 6},
        {"type": "normal", "weight_kg": 67.5, "reps": 5,  "rpe": 7},
        {"type": "normal", "weight_kg": 75,   "reps": 3,  "rpe": 7},
        {"type": "normal", "weight_kg": 80,   "reps": 3,  "rpe": 9},
    ],
}
EXPECTED_SQUAT = (
    "Squat (Barbell)\n"
    "20kg x 10, rir 4\n"
    "35kg x 5, rir 4\n"
    "45kg x 5, rir 4\n"
    "57.5kg x 3, rir 4\n"
    "\n"
    "67.5kg x 5, rir 3\n"
    "75kg x 3, rir 3\n"
    "80kg x 3, rir 1"
)

def test_squat_full_output():
    assert translate_exercise(SQUAT_2026_04_20).render() == EXPECTED_SQUAT


# -------- bodyweight (chin-up) --------

def test_chinup_bodyweight_accumulate_format():
    # 2026-04-20 Chin-Up: 8 sets of bodyweight, 3/3/3/2/1/2/1/1 reps, total 16.
    # RIR is appended per set, same conversion as weighted exercises:
    #   RPE 8.5 → rir 1-2 (half-step → range)
    #   RPE 9   → rir 1
    #   RPE 8   → rir 2
    ex = {
        "title": "Chin Up",
        "sets": [
            {"type": "normal", "weight_kg": None, "reps": 3, "rpe": 8.5},
            {"type": "normal", "weight_kg": None, "reps": 3, "rpe": 8.5},
            {"type": "normal", "weight_kg": None, "reps": 3, "rpe": 8.5},
            {"type": "normal", "weight_kg": None, "reps": 2, "rpe": 9},
            {"type": "normal", "weight_kg": None, "reps": 1, "rpe": 8},
            {"type": "normal", "weight_kg": None, "reps": 2, "rpe": 9},
            {"type": "normal", "weight_kg": None, "reps": 1, "rpe": 9},
            {"type": "normal", "weight_kg": None, "reps": 1, "rpe": 9},
        ],
    }
    expected = (
        "Chin Up\n"
        "16 total\n"
        "\n"
        "3, rir 1-2\n3, rir 1-2\n3, rir 1-2\n"
        "2, rir 1\n1, rir 2\n2, rir 1\n1, rir 1\n1, rir 1"
    )
    assert translate_exercise(ex).render() == expected


def test_pushup_bodyweight_accumulate_format():
    # Push-up with explicit RPEs — every set carries an rir suffix.
    ex = {
        "title": "Push-Up",
        "sets": [
            {"weight_kg": 0, "reps": 10, "rpe": 7},
            {"weight_kg": 0, "reps": 8,  "rpe": 8},
            {"weight_kg": 0, "reps": 6,  "rpe": 9},
        ],
    }
    # weight_kg: 0 is also treated as bodyweight.
    assert translate_exercise(ex).render() == (
        "Push-Up\n24 total\n\n10, rir 3\n8, rir 2\n6, rir 1"
    )


def test_bodyweight_omits_rir_when_no_rpe():
    """An exercise logged without RPE shouldn't get a stray ', rir' suffix —
    rir is conditional on RPE being present."""
    ex = {
        "title": "Push-Up",
        "sets": [
            {"weight_kg": 0, "reps": 12},
            {"weight_kg": 0, "reps": 12},
            {"weight_kg": 0, "reps": 12},
        ],
    }
    assert translate_exercise(ex).render() == (
        "Push-Up\n36 total\n\n12\n12\n12"
    )


def test_bodyweight_mixed_rpe_per_set():
    """Some sets with RPE, others without — the rir suffix appears only
    on the sets that have it."""
    ex = {
        "title": "Push-Up",
        "sets": [
            {"weight_kg": 0, "reps": 12, "rpe": 7},
            {"weight_kg": 0, "reps": 12},
            {"weight_kg": 0, "reps": 10, "rpe": 9},
        ],
    }
    assert translate_exercise(ex).render() == (
        "Push-Up\n34 total\n\n12, rir 3\n12\n10, rir 1"
    )

def test_back_raise_mixed_weight_NOT_bodyweight():
    # 2026-04-20 Back Raise: 0kg then 10kg x 2. The 0kg bodyweight set is
    # among weighted sets -> normal format, NOT bodyweight-accumulate.
    ex = {
        "title": "Back Extension (Weighted Hyperextension)",
        "sets": [
            {"weight_kg": 0,  "reps": 15, "rpe": 6},
            {"weight_kg": 10, "reps": 15, "rpe": 7},
            {"weight_kg": 10, "reps": 15, "rpe": 8},
        ],
    }
    out = translate_exercise(ex).render()
    # Mixed-weight: the 0kg set stays rendered as "0kg x 15" (not stripped to "15").
    assert out == (
        "Back Extension (Weighted Hyperextension)\n"
        "0kg x 15, rir 4\n"
        "\n"
        "10kg x 15, rir 3\n"
        "10kg x 15, rir 2"
    )


# -------- dumbbell halving + warmup separator --------

def test_dumbbell_curl_full_output():
    # 2026-04-20 Curl: 16/20/25 kg total, rpe 6/8/9 -> 8/10/12.5 per hand
    ex = {
        "title": "Bicep Curl (Dumbbell)",
        "sets": [
            {"type": "normal", "weight_kg": 16, "reps": 12, "rpe": 6},
            {"type": "normal", "weight_kg": 20, "reps": 12, "rpe": 8},
            {"type": "normal", "weight_kg": 25, "reps": 6,  "rpe": 9},
        ],
    }
    expected = (
        "Bicep Curl (Dumbbell)\n"
        "8kg x 12, rir 4\n"
        "\n"
        "10kg x 12, rir 2\n"
        "12.5kg x 6, rir 1"
    )
    assert translate_exercise(ex).render() == expected


# -------- warmup boundary edge cases --------

def test_boundary_no_warmups():
    sets = [
        {"weight_kg": 60, "reps": 5, "rpe": 8},
        {"weight_kg": 60, "reps": 5, "rpe": 9},
    ]
    assert _warmup_boundary(sets) is None

def test_boundary_all_warmups():
    sets = [
        {"weight_kg": 20, "reps": 10, "rpe": 6},
        {"weight_kg": 30, "reps": 10, "rpe": 6},
    ]
    assert _warmup_boundary(sets) is None   # no blank line if no working set

def test_boundary_normal_mix():
    sets = [
        {"weight_kg": 20, "reps": 10, "rpe": 6},
        {"weight_kg": 40, "reps": 10, "rpe": 7},
        {"weight_kg": 60, "reps": 5,  "rpe": 9},
    ]
    assert _warmup_boundary(sets) == 1

def test_no_rpe_values_means_no_boundary():
    sets = [
        {"weight_kg": 60, "reps": 5, "rpe": None},
        {"weight_kg": 60, "reps": 5, "rpe": None},
    ]
    assert _warmup_boundary(sets) is None


# -------- workout-level --------

def test_translate_workout_preserves_order():
    workout = {
        "title": "Pull Day",
        "exercises": [
            {"title": "Lat Pulldown", "sets": [{"weight_kg": 60, "reps": 10, "rpe": 8}]},
            {"title": "Bicep Curl (Dumbbell)", "sets": [{"weight_kg": 20, "reps": 10, "rpe": 9}]},
        ],
    }
    out = translate_workout(workout)
    assert [e["title"] for e in out] == ["Lat Pulldown", "Bicep Curl (Dumbbell)"]
    assert "60kg x 10, rir 2" in out[0]["text"]
    assert "10kg x 10, rir 1" in out[1]["text"]


# -------- dumbbell detection still works --------

def test_dumbbell_by_equipment_field():
    ex = {
        "title": "Bicep Curl",
        "equipment": "dumbbell",
        "sets": [{"weight_kg": 20.0, "reps": 10, "rpe": 8}],
    }
    assert _is_dumbbell(ex) is True
    assert "10kg x 10, rir 2" in translate_exercise(ex).render()

def test_barbell_not_halved():
    ex = {
        "title": "Barbell Row",
        "equipment": "barbell",
        "sets": [{"weight_kg": 60.0, "reps": 8, "rpe": 8}],
    }
    assert _is_dumbbell(ex) is False
    assert "60kg x 8, rir 2" in translate_exercise(ex).render()


# -------- TrueCoach plan parser (warmup count) --------

# Plan text copied from TrueCoach for 2026-04-20 "PT Session Squat & Press".
# Each exercise's plan description as it appears in the textarea placeholder
# block (to the left of the "Enter results" textarea).

SQUAT_PLAN_TEXT = (
    "Warmup\n"
    "Bar × 10\n"
    "35kg × 5\n"
    "45 kg × 5\n"
    "57.5 kg × 3\n"
    "\n"
    "Working sets\n"
    "5 x 5\n"
    "RIR 3\n"
    "Progress by 2.5kg"
)

BENCH_PLAN_TEXT = (
    "Warm-up\n"
    "Bar x 10\n"
    "30 x 5-10\n"
    "Start at 45kg\n"
    "4-5 x 10-12\n"
    "RIR 2-3\n"
    "Progress by 2.5kg"
)

# Exercises that don't have a warmup section in the plan.
CURL_PLAN_TEXT = (
    "3-4 x 8-12\n"
    "RIR 1-2\n"
    "Drop-set the last one"
)
FLYE_PLAN_TEXT = (
    "4 x 12\n"
    "RIR 1-2\n"
    "Slow eccentrics"
)
CHINUP_PLAN_TEXT = (
    "Accumulate 16 reps\n"
    "In as few sets as possible"
)
BACKRAISE_PLAN_TEXT = (
    "3 x 15\n"
    "Add weight when RIR hits 3"
)


def test_parse_warmup_count_squat():
    assert parse_warmup_count(SQUAT_PLAN_TEXT) == 4

def test_parse_warmup_count_bench_stops_at_instruction():
    # Two concrete warmup sets, then 'Start at 45kg' (instruction) aborts the count.
    assert parse_warmup_count(BENCH_PLAN_TEXT) == 2

def test_parse_warmup_count_no_warmup_heading():
    assert parse_warmup_count(CURL_PLAN_TEXT) is None
    assert parse_warmup_count(FLYE_PLAN_TEXT) is None
    assert parse_warmup_count(CHINUP_PLAN_TEXT) is None
    assert parse_warmup_count(BACKRAISE_PLAN_TEXT) is None

def test_parse_warmup_count_empty_or_none():
    assert parse_warmup_count("") is None
    assert parse_warmup_count(None) is None

def test_parse_warmup_count_heading_only_returns_zero():
    assert parse_warmup_count("Warm-up\n\nWorking sets\n5 x 5") == 0

def test_parse_warmup_count_stops_at_working_heading_without_blank_line():
    plan = "Warmup\nBar x 10\n35kg x 5\nWorking sets\n5 x 5"
    assert parse_warmup_count(plan) == 2

def test_parse_warmup_count_range_reps_ok():
    # "30 x 5-10" is a concrete weight with a rep range — count it.
    plan = "Warmup\n30 x 5-10\n\nWorking sets"
    assert parse_warmup_count(plan) == 1

def test_parse_warmup_count_set_count_range_is_not_a_warmup():
    # "4-5 x 10-12" is a set-count × rep-range template (working set), not a warmup.
    plan = "Warmup\n4-5 x 10-12\n\nWorking sets"
    assert parse_warmup_count(plan) == 0

def test_parse_warmup_count_bar_variants():
    plan = "Warm-up\nBar × 10\nBar x 10\n\nWorking sets"
    assert parse_warmup_count(plan) == 2


# -------- inline / flattened plan text (DOM sometimes strips newlines) --------

def test_parse_warmup_count_inline_bench():
    # Exactly what Mark saw in TrueCoach: single-line, whitespace-separated.
    plan = (
        "Warm-up Bar x 10 30 x 5-10 Start at 45kg 4-5 x 10-12 "
        "RIR 2-3 Progress by 2.5kg"
    )
    assert parse_warmup_count(plan) == 2

def test_parse_warmup_count_inline_squat():
    plan = (
        "Warmup Bar × 10 35kg × 5 45 kg × 5 57.5 kg × 3 "
        "Working sets 5 x 5 RIR 3 Progress by 2.5kg"
    )
    assert parse_warmup_count(plan) == 4

def test_parse_warmup_count_inline_no_warmup():
    # "3-4 x 8-12" is a set-count × rep-range template, not a warmup set,
    # and there's no warmup heading → None.
    plan = "3-4 x 8-12 RIR 1-2 Drop-set the last one"
    assert parse_warmup_count(plan) is None

def test_parse_warmup_count_inline_no_partial_stitching():
    # Regression guard: '45kg' followed by '4-5 x 10-12' must NOT be stitched
    # into a phantom '45kg × 4-5' warmup set. The parser already stopped at
    # 'Start' — but belt-and-braces.
    plan = "Warm-up Bar x 10 Start at 45kg 4-5 x 10-12 RIR 2-3"
    assert parse_warmup_count(plan) == 1

def test_parse_warmup_count_mixed_line_and_inline():
    # Some plans mix: heading on its own line, sets inline, then instructions.
    plan = "Warm-up\nBar x 10 30 x 5-10\nStart at 45kg 4-5 x 10-12"
    assert parse_warmup_count(plan) == 2


# -------- translate_exercise: explicit warmup_count overrides RPE heuristic --------

def test_translate_exercise_explicit_warmup_count_overrides_rpe_heuristic():
    # All sets at RPE 8 — RPE heuristic would say "no warmups" (boundary at 0).
    # With explicit warmup_count=1, we should insert a blank line after set 1.
    ex = {
        "title": "Some Lift",
        "equipment": "barbell",
        "sets": [
            {"weight_kg": 20, "reps": 10, "rpe": 8},
            {"weight_kg": 30, "reps": 10, "rpe": 8},
            {"weight_kg": 40, "reps": 10, "rpe": 8},
        ],
    }
    assert translate_exercise(ex, warmup_count=1).render() == (
        "Some Lift\n"
        "20kg x 10, rir 2\n"
        "\n"
        "30kg x 10, rir 2\n"
        "40kg x 10, rir 2"
    )

def test_translate_exercise_warmup_count_zero_suppresses_separator():
    # RPE heuristic would insert a blank line; explicit warmup_count=0 suppresses it
    # (the plan had no warmup section).
    ex = {
        "title": "Curl",
        "equipment": "dumbbell",
        "sets": [
            {"weight_kg": 16, "reps": 12, "rpe": 6},
            {"weight_kg": 20, "reps": 12, "rpe": 8},
            {"weight_kg": 25, "reps": 6,  "rpe": 9},
        ],
    }
    # Dumbbell halving applies; no blank line between the "rir 4" and the rest.
    assert translate_exercise(ex, warmup_count=0).render() == (
        "Curl\n"
        "8kg x 12, rir 4\n"
        "10kg x 12, rir 2\n"
        "12.5kg x 6, rir 1"
    )

def test_translate_exercise_warmup_count_none_falls_back_to_heuristic():
    # Same exercise as above, no explicit count — heuristic inserts blank after rpe<7 set.
    ex = {
        "title": "Curl",
        "equipment": "dumbbell",
        "sets": [
            {"weight_kg": 16, "reps": 12, "rpe": 6},
            {"weight_kg": 20, "reps": 12, "rpe": 8},
        ],
    }
    assert translate_exercise(ex, warmup_count=None).render() == (
        "Curl\n"
        "8kg x 12, rir 4\n"
        "\n"
        "10kg x 12, rir 2"
    )

def test_translate_exercise_warmup_count_exceeds_sets():
    # Defensive: plan claims 5 warmups but only 2 sets were logged.
    # Treat as "no separator" rather than placing blank after the last set.
    ex = {
        "title": "Lift",
        "equipment": "barbell",
        "sets": [
            {"weight_kg": 40, "reps": 5, "rpe": 8},
            {"weight_kg": 50, "reps": 5, "rpe": 9},
        ],
    }
    out = translate_exercise(ex, warmup_count=5).render()
    assert out == "Lift\n40kg x 5, rir 2\n50kg x 5, rir 1"


def test_translate_workout_accepts_warmup_counts():
    workout = {
        "exercises": [
            {
                "title": "Squat",
                "equipment": "barbell",
                "sets": [
                    {"weight_kg": 20, "reps": 10, "rpe": 6},
                    {"weight_kg": 40, "reps": 5,  "rpe": 6},
                    {"weight_kg": 60, "reps": 5,  "rpe": 8},
                ],
            },
            {
                "title": "Curl",
                "equipment": "dumbbell",
                "sets": [
                    {"weight_kg": 20, "reps": 10, "rpe": 6},
                    {"weight_kg": 30, "reps": 8,  "rpe": 9},
                ],
            },
        ],
    }
    out = translate_workout(workout, warmup_counts=[2, 0])
    # Squat: blank after 2nd set (plan says 2 warmups).
    assert out[0]["title"] == "Squat"
    assert out[0]["text"] == (
        "20kg x 10, rir 4\n40kg x 5, rir 4\n\n60kg x 5, rir 2"
    )
    # Curl: no blank line — plan has no warmup section, overriding the RPE
    # heuristic which would have inserted one after the rpe-6 set.
    assert out[1]["title"] == "Curl"
    assert out[1]["text"] == (
        "10kg x 10, rir 4\n15kg x 8, rir 1"
    )


# -------- rev3: Hevy set-type markers (authoritative when present) --------

def test_warmup_boundary_from_types_returns_count_of_leading_warmups():
    sets = [
        {"type": "warmup", "weight_kg": 20, "reps": 10},
        {"type": "warmup", "weight_kg": 40, "reps": 5},
        {"type": "normal", "weight_kg": 60, "reps": 5},
        {"type": "normal", "weight_kg": 60, "reps": 5},
    ]
    assert _warmup_boundary_from_types(sets) == 2


def test_warmup_boundary_from_types_none_when_no_markers():
    sets = [
        {"type": "normal", "weight_kg": 60, "reps": 5},
        {"type": "normal", "weight_kg": 60, "reps": 5},
    ]
    assert _warmup_boundary_from_types(sets) is None


def test_warmup_boundary_from_types_none_when_all_warmups():
    sets = [
        {"type": "warmup", "weight_kg": 20, "reps": 10},
        {"type": "warmup", "weight_kg": 40, "reps": 5},
    ]
    assert _warmup_boundary_from_types(sets) is None


def test_warmup_boundary_from_types_none_when_first_set_is_normal():
    sets = [
        {"type": "normal", "weight_kg": 60, "reps": 5},
        {"type": "warmup", "weight_kg": 20, "reps": 10},  # odd placement
    ]
    assert _warmup_boundary_from_types(sets) is None


def test_workout_has_warmup_markers_detects_any_exercise():
    workout = {"exercises": [
        {"title": "A", "sets": [{"type": "normal", "weight_kg": 60, "reps": 5}]},
        {"title": "B", "sets": [
            {"type": "warmup", "weight_kg": 20, "reps": 10},
            {"type": "normal", "weight_kg": 60, "reps": 5},
        ]},
    ]}
    assert _workout_has_warmup_markers(workout) is True


def test_workout_has_warmup_markers_false_when_none_tagged():
    workout = {"exercises": [
        {"title": "A", "sets": [{"type": "normal", "weight_kg": 60, "reps": 5}]},
        {"title": "B", "sets": [{"type": "normal", "weight_kg": 60, "reps": 5}]},
    ]}
    assert _workout_has_warmup_markers(workout) is False


def test_markers_session_wide_every_exercise_uses_markers():
    """If any exercise has markers, ALL exercises in translate_workout use them."""
    workout = {"exercises": [
        {
            "title": "Squat",
            "sets": [
                {"type": "warmup", "weight_kg": 20, "reps": 10, "rpe": 4},
                {"type": "warmup", "weight_kg": 40, "reps": 5,  "rpe": 4},
                {"type": "normal", "weight_kg": 60, "reps": 5,  "rpe": 8},
            ],
        },
        {
            # No markers on this exercise. Under rev3 the session-wide flag
            # says "trust markers" → no warmups here → no blank line,
            # EVEN THOUGH the RPE heuristic would have drawn one after set 0.
            "title": "Curl",
            "sets": [
                {"weight_kg": 10, "reps": 10, "rpe": 6},
                {"weight_kg": 15, "reps": 8,  "rpe": 9},
            ],
        },
    ]}
    out = translate_workout(workout)
    assert out[0]["text"] == (
        "20kg x 10, rir 6\n40kg x 5, rir 6\n\n60kg x 5, rir 2"
    )
    assert out[1]["text"] == "10kg x 10, rir 4\n15kg x 8, rir 1"


def test_markers_override_provided_warmup_count():
    """Markers beat a warmup_count supplied from TC plan text."""
    workout = {"exercises": [{
        "title": "Bench",
        "sets": [
            {"type": "warmup", "weight_kg": 20, "reps": 10, "rpe": 4},
            {"type": "normal", "weight_kg": 50, "reps": 10, "rpe": 8},
            {"type": "normal", "weight_kg": 50, "reps": 10, "rpe": 8},
        ],
    }]}
    # TC plan text said 2 warmups. Markers say 1. Markers win.
    out = translate_workout(workout, warmup_counts=[2])
    assert out[0]["text"] == (
        "20kg x 10, rir 6\n\n50kg x 10, rir 2\n50kg x 10, rir 2"
    )


def test_no_markers_falls_back_to_warmup_count():
    workout = {"exercises": [{
        "title": "Bench",
        "sets": [
            {"weight_kg": 20, "reps": 10, "rpe": 4},
            {"weight_kg": 40, "reps": 5,  "rpe": 4},
            {"weight_kg": 50, "reps": 10, "rpe": 8},
        ],
    }]}
    out = translate_workout(workout, warmup_counts=[2])
    assert out[0]["text"] == (
        "20kg x 10, rir 6\n40kg x 5, rir 6\n\n50kg x 10, rir 2"
    )


def test_no_markers_no_count_falls_back_to_rpe_heuristic():
    workout = {"exercises": [{
        "title": "Bench",
        "sets": [
            {"weight_kg": 20, "reps": 10, "rpe": 4},
            {"weight_kg": 50, "reps": 10, "rpe": 8},
        ],
    }]}
    out = translate_workout(workout)
    assert out[0]["text"] == "20kg x 10, rir 6\n\n50kg x 10, rir 2"


if __name__ == "__main__":
    import sys, traceback
    passed = failed = 0
    names = [n for n in globals() if n.startswith("test_")]
    for name in names:
        try:
            globals()[name]()
            print(f"  ok  {name}")
            passed += 1
        except Exception:
            print(f"  FAIL {name}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
