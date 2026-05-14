"""Build a preview of the reverse sync from real TC upcoming workouts.

Emits a JSON structure and a human-readable summary so Mark can review
before we hit the Hevy API.

v2 (2026-04-23): superset groups, rest_seconds per exercise, progressive
within-session weights for big barbell lifts, weighted chin-up routing.
"""

import json
import re
from truecoach_to_hevy import load_resolver, build_hevy_exercise


# Scraped from TrueCoach on 2026-04-23.
# position_code is the letter prefix in the TC card ("A", "C1", "E2", etc.).
# Pairs like C1/C2 or E1/E2 form a superset group.
UPCOMING = [
    {
        "tc_workout_id": "595678225",
        "date": "2026-04-24",
        "day_name": "Friday",
        "exercises": [
            {"position": "A", "title": "Bench",
             "plan": "Warm-up\nBar × 12\n35 kg × 5\n45 kg × 3\nWorking sets\n50kg ×5\n55 kg × 3\n60 kg × 1+"},
            {"position": "B", "title": "Squat",
             "plan": "Warm-up bar x 10\n35kg x 5-8\nStart at 50kg\n2-3 x 6-10\nRIR 2-3\nProgress by 2.5-5kg"},
            {"position": "C1", "title": "Chin-Up",
             "plan": "Weighted\nStart at 2.5kg\n3-5 x 2 reps\nRIR 2\nProgress by 2.5kg"},
            {"position": "C2", "title": "Push-Up",
             "plan": "Bodyweight\n3-5 x 6-12 reps\nRIR 2-3"},
            {"position": "D", "title": "Seated Leg Curl",
             "plan": "3-4 x 8-12\nRIR 1-2"},
        ],
    },
    {
        "tc_workout_id": "595828381",
        "date": "2026-04-27",
        "day_name": "Monday",
        "exercises": [
            {"position": "A", "title": "Deadlift",
             "plan": "Warm-ups\n45 kg × 5-10\n60 kg × 5\n70 kg × 3\nWork sets\n80 kg × 5\n87.5 kg × 5\n95 kg × 5+"},
            {"position": "B", "title": "Barbell Overhead Press",
             "plan": "Warm-up\nBar x 12\nStart at 22.5kg\n5x10\nRIR 2-3\nProgress by 2.5kg"},
            {"position": "C", "title": "Dumbbell Row",
             "plan": "4-5 x 10-15 each arm\nRIR 2\nStart at 10kg\nProgress by 2.5-5kg"},
            {"position": "D", "title": "Step Up",
             "plan": "2-3 x 8-10 each leg\nStart with 12.5kg each hand\nRIR 2-3\nprogress by 2.5-5kg per hand"},
            {"position": "E", "title": "Machine Chest Flye",
             "plan": "Can be subbed for dumbbell chest flye if it's busy\n1-4 x 10-12\nRIR 1-2"},
            {"position": "F", "title": "Single Arm Cable Curl",
             "plan": "2-5 x 8-12 each arm\nRIR 2\nCan do this with two arms if you're tight on time using a straight bar attachment. If cable machine is busy, can sub for dumbbell or barbell."},
        ],
    },
    {
        "tc_workout_id": "596092058",
        "date": "2026-04-29",
        "day_name": "Wednesday",
        "exercises": [
            {"position": "A", "title": "Squat",
             "plan": "Warmup\nBar × 10\n35kg × 5\n45 kg × 5\n57.5 kg × 3\nWorking sets\n67.5 kg × 5\n75 kg × 3\n80 kg × 1+"},
            {"position": "B", "title": "Bench",
             "plan": "Warm-up\nBar x 10\n30 x 5-10\nStart at 45kg\n4-5 x 10-12\nRIR 2-3\nProgress by 2.5kg"},
            {"position": "C", "title": "Chin-Up",
             "plan": "Accumulate 20-30 total reps\nKeep 2 RIR"},
            {"position": "D", "title": "Machine Chest Flye",
             "plan": "3-4 x 10-12\nRIR 1-2"},
            {"position": "E1", "title": "ISSA Exercise Library Dumbbell Biceps Curl",
             "plan": "Start at 8kg\n3-4 x 8-12\nRIR 1-2\nProgress by 2-2.5kg"},
            {"position": "E2", "title": "45° Back Raise",
             "plan": "2-3 x 10-15 reps / RIR 2\nNot 100% sure if your gym has one of these. If they don't you could sub for a dumbbell stiff leg deadlift"},
        ],
    },
]


_SUPERSET_RE = re.compile(r"^([A-Z])(\d+)$")


def assign_superset_ids(exercises):
    """Map position codes like A/B/C1/C2/E1/E2 to Hevy superset_ids.

    Exercises that share a letter prefix AND have digit suffixes are in the
    same superset. Lone letters get superset_id=None. IDs are 0-indexed per
    routine.
    """
    groups = {}
    for ex in exercises:
        m = _SUPERSET_RE.match(ex["position"])
        if m:
            groups.setdefault(m.group(1), []).append(ex)
    next_id = 0
    letter_to_sid = {}
    # Iterate in position-order for stable numbering
    seen_letters = []
    for ex in exercises:
        m = _SUPERSET_RE.match(ex["position"])
        if m and len(groups[m.group(1)]) >= 2 and m.group(1) not in seen_letters:
            seen_letters.append(m.group(1))
            letter_to_sid[m.group(1)] = next_id
            next_id += 1
    result = {}
    for ex in exercises:
        m = _SUPERSET_RE.match(ex["position"])
        if m and m.group(1) in letter_to_sid:
            result[ex["position"]] = letter_to_sid[m.group(1)]
        else:
            result[ex["position"]] = None
    return result


def fmt_set(s):
    w = f"{s['weight_kg']:g}kg"
    return f"{s['type'][0].upper()} {w} × {s['reps']}"


def main():
    resolver = load_resolver()

    preview = []
    for wk in UPCOMING:
        super_map = assign_superset_ids(wk["exercises"])
        exs = []
        for ex in wk["exercises"]:
            built = build_hevy_exercise(
                ex["title"],
                ex["plan"],
                resolver,
                position_code=ex["position"],
                superset_id=super_map[ex["position"]],
            )
            exs.append(built)
        preview.append({
            "tc_workout_id": wk["tc_workout_id"],
            "date": wk["date"],
            "day_name": wk["day_name"],
            "routine_name": wk["day_name"],
            "routine_notes": wk["date"],
            "exercises": exs,
        })

    with open("/sessions/nifty-intelligent-knuth/mnt/personal_automation/reverse_sync_preview.json", "w") as f:
        json.dump(preview, f, indent=2)

    for r in preview:
        print(f"\n=== ROUTINE: {r['routine_name']} — notes: {r['routine_notes']} ===")
        for ex in r["exercises"]:
            ss = f" ⟨superset {ex['superset_id']}⟩" if ex["superset_id"] is not None else ""
            print(f"\n  [{ex['position_code']}] {ex['tc_title']!r}{ss}")
            print(f"    → Hevy: {ex['resolved_title']!r}  [{ex['confidence']}]  ({ex['exercise_template_id']})")
            print(f"    Rest: {ex['rest_seconds']}s")
            if ex["confidence"].endswith("weak") or ex["confidence"] == "no-match":
                if ex["alternatives"]:
                    print(f"    Alternatives considered:")
                    for a in ex["alternatives"][:3]:
                        print(f"      - {a['title']!r} ({a['source']}, score={a['score']:.2f})")
            print(f"    Sets ({len(ex['sets'])}):")
            for s in ex["sets"]:
                print(f"      {fmt_set(s)}")
            if ex["notes"]:
                print(f"    Notes: {ex['notes']!r}")
            if ex["warnings"]:
                print(f"    ⚠ {ex['warnings']}")


if __name__ == "__main__":
    main()
