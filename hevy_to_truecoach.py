"""
Hevy-to-TrueCoach translator.

Pure functions. Takes a Hevy workout (as returned by GET /v1/workouts)
and emits per-exercise TrueCoach text blocks.

Rules (from Mark, 2026-04-21, rev3):
  - Log EVERY set (warmups + working). Better to have the data than miss it.
  - Insert a blank line at the warmup→working boundary. Precedence, chosen
    ONCE PER SESSION:
      1. If ANY set in the whole workout has `type == "warmup"`, trust
         Hevy's markers on every exercise — no heuristics.
      2. Otherwise, per exercise: warmup count from the matching TC plan.
      3. Otherwise: fallback heuristic — first set where RPE jumps from <7
         to >=7.
  - RPE → RIR:
      * Integer RPE → single-integer RIR  (RPE 7 → "rir 3")
      * Half-step RPE → integer RANGE     (RPE 6.5 → "rir 3-4")
  - Dumbbell exercises: Hevy logs TOTAL weight (both hands combined);
    TrueCoach wants PER-HAND weight. Halve it.
    Dumbbell detected by equipment == "dumbbell" OR "(Dumbbell)" in title.
  - Format per set:
        "45kg x 10, rir 3-4"          (with weight + rpe)
        "45kg x 10"                   (with weight, no rpe)
        "3, rir 1-2"                  (bodyweight, no weight)
        "3"                           (bodyweight, no rpe)
"""

from dataclasses import dataclass
from typing import Optional, Iterable
import re


DUMBBELL_EQUIPMENT = {"dumbbell", "dumbells", "dumbbells"}
# Fallback only — used when no TrueCoach plan text is supplied.
WORKING_RPE_THRESHOLD = 7.0

# A warmup-set token (no line anchors — used for mid-string matching, because
# the TrueCoach plan text sometimes arrives as a run-on paragraph rather than
# line-delimited).
# Examples that should match:
#   "Bar × 10", "Bar x 10", "35kg × 5", "45 kg × 5", "57.5 kg × 3", "30 x 5-10"
# Examples that should NOT match:
#   "4-5 x 10-12"  (leading "4-" — set-count range, not a weight)
#   "Start at 45kg", "RIR 2-3", "Progress by 2.5kg"  (no [x×] structure)
# The caller further requires that the match end at whitespace or EOS so it
# doesn't partially eat into neighbouring content like "45kg 4-5 x 10-12".
_WARMUP_SET_TOKEN = re.compile(
    r"""
    (?:bar|\d+(?:\.\d+)?\s*(?:kg)?)   # "Bar" or a single weight (decimal ok, "kg" optional)
    \s*[x×]\s*                         # the multiplication mark
    \d+(?:-\d+)?\+?                    # reps: single, range, or "1+" (AMRAP)
    """,
    re.IGNORECASE | re.VERBOSE,
)
_WARMUP_HEADING = re.compile(r"\bwarm[\s\-]?up\b", re.IGNORECASE)
_WORKING_HEADING = re.compile(r"working(?:\s+sets?)?\b", re.IGNORECASE)


@dataclass
class ExerciseBlock:
    title: str
    lines: list  # list[str] — one per working set

    def render(self, separator: str = "\n") -> str:
        """Render as a TrueCoach-ready text block: header line + sets."""
        body = separator.join(self.lines)
        return f"{self.title}\n{body}" if body else self.title


def _is_dumbbell(exercise: dict) -> bool:
    eq = (exercise.get("equipment") or "").strip().lower()
    if eq in DUMBBELL_EQUIPMENT:
        return True
    title = (exercise.get("title") or "").lower()
    # Hevy's naming convention: "Bicep Curl (Dumbbell)", "Dumbbell Row", etc.
    return "dumbbell" in title


def _format_weight(kg: float) -> str:
    """45.0 -> '45kg', 47.5 -> '47.5kg'."""
    if kg is None:
        return ""
    # Show .0 as integer, otherwise keep one decimal (2.5, 47.5, etc.)
    if abs(kg - round(kg)) < 1e-6:
        return f"{int(round(kg))}kg"
    # Strip trailing zeros but keep one significant decimal
    s = f"{kg:.2f}".rstrip("0").rstrip(".")
    return f"{s}kg"


def _format_rir(rpe: Optional[float]) -> Optional[str]:
    """
    Integer RPE -> 'rir N'.
    Half-step RPE (x.5) -> 'rir (N-1)-N' (lower first).
    None -> None (omit).
    """
    if rpe is None:
        return None
    # Integer RPE
    if abs(rpe - round(rpe)) < 1e-6:
        rir = int(round(10 - rpe))
        if rir < 0:
            rir = 0
        return f"rir {rir}"
    # Half-step: rpe of x.5 -> rir of (10-x-1) to (10-x)
    # e.g. rpe 6.5 -> rir 3-4, rpe 9.5 -> rir 0-1
    high = int(10 - (rpe - 0.5))   # upper bound
    low = high - 1                  # lower bound
    if low < 0:
        low = 0
    return f"rir {low}-{high}"


def _format_set(set_data: dict, is_dumbbell: bool) -> str:
    """
    Weighted-path formatter: always renders the weight (including "0kg")
    so mixed-weight exercises stay consistent. The fully-bodyweight case
    (all sets 0/null) is handled separately by _translate_bodyweight_accumulate,
    never reaches this function.
    """
    weight = set_data.get("weight_kg")
    reps = set_data.get("reps")
    rpe = set_data.get("rpe")
    if weight is not None and is_dumbbell:
        weight = weight / 2.0
    # Missing-weight (null) sets inside a weighted exercise are rare; render as "x reps".
    w = _format_weight(weight) if weight is not None else ""
    r = f"{int(reps)}" if reps is not None else ""
    rir = _format_rir(rpe)
    if w and r:
        core = f"{w} x {r}"
    elif r:
        core = r
    else:
        core = w
    return f"{core}, {rir}" if rir else core


def _has_warmup_markers(sets: list) -> bool:
    """True iff any set in this exercise is tagged `type == "warmup"`."""
    return any((s.get("type") or "").lower() == "warmup" for s in sets)


def _workout_has_warmup_markers(workout: dict) -> bool:
    """True if any exercise in the workout has at least one warmup-typed set.

    Session-wide check: if Mark tagged even one warmup in the Hevy app for
    this session, we treat the markers as authoritative across every
    exercise — we don't mix heuristics with markers.
    """
    for ex in (workout.get("exercises") or []):
        if _has_warmup_markers(ex.get("sets") or []):
            return True
    return False


def _warmup_boundary_from_types(sets: list) -> Optional[int]:
    """Boundary from Hevy's per-set `type` field.

    Returns the index of the first non-warmup set when there are both
    warmup-tagged and non-warmup-tagged sets. Returns None if this exercise
    has no warmup-tagged sets, or all sets are warmups, or the first set is
    already non-warmup — in every None case no separator should be drawn.
    """
    if not _has_warmup_markers(sets):
        return None
    for i, s in enumerate(sets):
        if (s.get("type") or "").lower() != "warmup":
            return i if i > 0 else None
    return None


def _warmup_boundary(sets: list) -> Optional[int]:
    """
    Return the index of the first "working" set (RPE >= 7) if it's > 0 AND
    there are preceding sets that look like warmups. Otherwise None.
    None means: don't insert a blank line.

    Fallback heuristic used only when the TrueCoach plan text wasn't supplied
    (or didn't specify warmups). Prefer parse_warmup_count() when possible.
    """
    for i, s in enumerate(sets):
        rpe = s.get("rpe")
        if rpe is not None and rpe >= WORKING_RPE_THRESHOLD:
            return i if i > 0 else None
    return None  # no working set found at all — no separator


def parse_warmup_count(plan_text: Optional[str]) -> Optional[int]:
    """
    Parse a TrueCoach exercise plan description for an explicit warmup count.

    Works on both line-delimited plan text (what the DOM gives us sometimes)
    and inline/run-on text (what it gives us the rest of the time — e.g.
    "Warm-up Bar x 10 30 x 5-10 Start at 45kg 4-5 x 10-12 RIR 2-3 ...").

    Algorithm:
      1. Find a 'Warmup' / 'Warm-up' heading.
      2. From just past the heading, scan forward token-by-token:
         - Skip whitespace.
         - Stop at a 'Working sets' heading.
         - If the next token looks like '<weight> [×|x] <reps>', count it.
         - Otherwise stop (instruction line, set-count template, etc.).

    A warmup-set match only counts if it's followed by whitespace or EOS —
    this prevents a standalone '45kg' inside 'Start at 45kg 4-5 x 10-12'
    from being greedily stitched to the following '4-5 x 10-12'.

    Returns:
        None if plan_text is falsy or has no warmup heading.
        An integer (possibly 0) when a warmup heading is present.
    """
    if not plan_text:
        return None

    heading = _WARMUP_HEADING.search(plan_text)
    if heading is None:
        return None

    pos = heading.end()
    n = len(plan_text)
    count = 0

    while pos < n:
        # Skip inter-token whitespace.
        while pos < n and plan_text[pos].isspace():
            pos += 1
        if pos >= n:
            break

        # A 'Working sets' heading ends the warmup section.
        if _WORKING_HEADING.match(plan_text, pos):
            break

        # Try to consume a warmup-set token at this position.
        m = _WARMUP_SET_TOKEN.match(plan_text, pos)
        if m is None:
            break
        end = m.end()
        # Only accept the match if it ends at a token boundary (whitespace or EOS).
        if end < n and not plan_text[end].isspace():
            break

        count += 1
        pos = end

    return count


def _is_bodyweight(sets: list) -> bool:
    """True if every set has null-or-zero weight (e.g. chin-up, push-up)."""
    if not sets:
        return False
    return all(
        (s.get("weight_kg") is None or s.get("weight_kg") == 0)
        for s in sets
    )


def _translate_bodyweight_accumulate(exercise: dict, sets: list) -> "ExerciseBlock":
    """
    Special chin-up/push-up format (Mark's 2026-04-21 rule):
        <sum-of-reps> total
        <blank>
        <reps_1>[, rir N]
        <reps_2>[, rir N]
        ...

    Per-set RIR is appended when Hevy has an RPE on that set — same
    format and conversion as the weighted path, so push-ups carry the
    same coaching context as everything else.
    """
    title = exercise.get("title") or "Exercise"
    total = sum(int(s.get("reps") or 0) for s in sets)
    lines = [f"{total} total", ""]
    for s in sets:
        reps = s.get("reps")
        if reps is None:
            lines.append("")
            continue
        rir = _format_rir(s.get("rpe"))
        core = f"{int(reps)}"
        lines.append(f"{core}, {rir}" if rir else core)
    return ExerciseBlock(title=title, lines=lines)


def translate_exercise(
    exercise: dict,
    warmup_count: Optional[int] = None,
    markers_authoritative: bool = False,
) -> ExerciseBlock:
    """
    Translate a Hevy exercise into an ExerciseBlock for TrueCoach.

    Boundary precedence:
      1. If markers_authoritative (the caller has seen at least one
         warmup-typed set somewhere in the session), use this exercise's
         own markers — no heuristics, no plan-text fallback. An exercise
         with zero warmup-typed sets gets no blank line.
      2. Else warmup_count (parsed from the TC plan) if 0 < count < len(sets).
      3. Else RPE-threshold heuristic (_warmup_boundary).
    """
    title = exercise.get("title") or "Exercise"
    sets = exercise.get("sets") or []

    if _is_bodyweight(sets):
        return _translate_bodyweight_accumulate(exercise, sets)

    is_db = _is_dumbbell(exercise)
    if markers_authoritative:
        boundary = _warmup_boundary_from_types(sets)
    elif warmup_count is None:
        boundary = _warmup_boundary(sets)
    elif 0 < warmup_count < len(sets):
        boundary = warmup_count
    else:
        boundary = None  # explicit "no warmup section" from the plan

    lines = []
    for i, s in enumerate(sets):
        if boundary is not None and i == boundary:
            lines.append("")    # blank line between warmups and working sets
        lines.append(_format_set(s, is_db))
    return ExerciseBlock(title=title, lines=lines)


def translate_workout(
    workout: dict,
    set_separator: str = "\n",
    warmup_counts: Optional[list] = None,
) -> list:
    """
    Returns a list of {title, text, notes, is_dumbbell} per exercise,
    ordered as in Hevy.

    If any set in the workout is marked `type == "warmup"`, Hevy's markers
    are trusted as authoritative across every exercise; warmup_counts and
    the RPE heuristic are both ignored for that session.

    warmup_counts (optional, only consulted when no markers are present):
    list aligned with workout['exercises']. Each entry is either an int
    (explicit count parsed from the TC plan) or None (fall back to RPE).
    """
    out = []
    exercises = workout.get("exercises") or []
    markers_authoritative = _workout_has_warmup_markers(workout)
    for idx, ex in enumerate(exercises):
        wc = None
        if warmup_counts is not None and idx < len(warmup_counts):
            wc = warmup_counts[idx]
        block = translate_exercise(
            ex,
            warmup_count=wc,
            markers_authoritative=markers_authoritative,
        )
        out.append(
            {
                "title": block.title,
                "text": set_separator.join(block.lines),
                "notes": ex.get("notes") or "",
                "is_dumbbell": _is_dumbbell(ex),
            }
        )
    return out
