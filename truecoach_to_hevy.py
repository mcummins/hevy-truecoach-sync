"""
TrueCoach-to-Hevy translator.

Pure functions. Takes a TrueCoach exercise title + plan description text and
emits a Hevy-routine-ready structure:

    {
        "title": "Bench Press (Barbell)",
        "exercise_template_id": "79D0BB3A",
        "notes": "... cleaned plan text minus set-lines and sub-headings ...",
        "sets": [
            {"type": "warmup", "weight_kg": 20, "reps": 10},
            {"type": "warmup", "weight_kg": 40, "reps": 5},
            {"type": "normal", "weight_kg": 60, "reps": 12},
            ...
        ],
    }

Conventions (confirmed by Mark 2026-04-22):
  - Sets range "4-5 x R":  take HIGH end (5 sets).
  - Reps  range "N x 10-12": take HIGH end (12 reps).
  - Warmup lines become type="warmup" sets.
  - Working lines become type="normal".
  - Weight:
      * explicit in set line (e.g. "35kg × 5", "5 x 5 @ 60kg") → use it
      * "Start at/with N kg" hint nearby for a templated set → use it
      * dumbbell/kettlebell exercise + "each hand" → DOUBLE to get Hevy
        total weight (both implements are logged per-hand in TC, total
        in Hevy)
      * otherwise 0
  - RPE/RIR guidance and "Start at…", "Progress by…", "each arm" qualifiers
    are preserved verbatim in exercise notes — but section headings and
    consumed set-lines are stripped.

Plan patterns handled:
  A. Ascending-weight individual sets:
         Working sets
         50kg ×5
         55 kg × 3
         60 kg × 1+
  B. Template with or without "Start at" hint:
         Start at 45kg
         4-5 x 10-12
  C. Bodyweight template:
         3-5 x 6-12 reps
  D. "Accumulate N total reps":
         Accumulate 20-30 total reps
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

# Headings (prefix-only, matched against a trimmed line)
_WARMUP_HEADING = re.compile(r"^\s*warm[\s\-]?ups?\s*:?\s*$", re.IGNORECASE)
_WORKING_HEADING = re.compile(r"^\s*work(?:ing)?(?:\s+sets?)?\s*:?\s*$", re.IGNORECASE)

# Weight number token.
#
# Cillian half-writes half-kg increments: he types the decimal point but
# drops the digit after it, giving "12 . kg", "12. kg" or "12.kg" where he
# meant 12.5kg. Before this was handled the trailing dot broke the match and
# the whole set line was silently dropped from the parse. We accept the
# dangling dot here and `_parse_weight_token` resolves it to `.5`
# (Mark, 2026-08-21 — confirmed that's always the intent).
#
# The optional spaces are `[ \t]` and NOT `\s`, deliberately: `\s` matches
# newlines, which would let a weight token run off the end of its line and
# swallow the start of the next plan line.
_WEIGHT_NUM = r"\d+(?:[ \t]{0,2}\.[ \t]{0,2}\d*)?"

# Individual-set line prefix:  weight  ×  reps
#   Matches from start of line; caller uses .end() to get trailing text.
#   AMRAP marker `+` may have whitespace before it ("5 +" as well as "5+").
_INDIV_SET_RE = re.compile(
    r"""
    ^\s*
    (?P<weight>bar|""" + _WEIGHT_NUM + r""")[ \t]*(?P<kg>kg)?
    \s*[x×]\s*
    (?P<reps_lo>\d+)(?:\s*-\s*(?P<reps_hi>\d+))?
    \s*(?P<amrap>\+)?
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Template-set line prefix:  <sets> × <reps>  with optional "@ kg"
#   The word "sets" between the count and the "x" is optional, so
#   "4 sets x 8-12 reps / RIR 2" parses the same as "4 x 8-12". Without
#   this, that wording fell through to _SETS_NO_REPS_RE and shipped
#   placeholder sets with blank reps (seen 2026-09-02, Band Assisted Dip).
#
#   Cillian also writes the connector out in words — "3-6 sets of 1-3
#   reps" (Pull-Up, seen 2026-09-14). That has a rep target like any
#   other template line, but with no "x" it used to fall through to
#   _SETS_NO_REPS_RE and ship 6 sets with blank reps. The word form is
#   accepted only WITH the "sets" keyword ("3-6 sets of 1-3"), so a bare
#   "3 of 5" can't match and start eating prose.
_TEMPLATE_SET_RE = re.compile(
    r"""
    ^\s*
    (?P<sets_lo>\d+)(?:\s*-\s*(?P<sets_hi>\d+))?
    (?:
        (?:\s*sets?)?\s*[x×]\s*      # "4 x 8-12" / "4 sets x 8-12"
      |
        \s*sets?\s+of\s+             # "3-6 sets of 1-3"
    )
    (?P<reps_lo>\d+)(?:\s*-\s*(?P<reps_hi>\d+))?
    \s*\+?\s*(?:reps?)?
    (?:\s*@\s*(?P<weight>""" + _WEIGHT_NUM + r""")\s*kg)?
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Inline heading that prefixes content on the same line,
# e.g. "Warm-up bar x 10" or "Working sets 5 x 5 @ 60kg"
_INLINE_HEADING_RE = re.compile(
    r"^\s*(?P<which>warm[\s\-]?ups?|work(?:ing)?(?:\s+sets?)?)\s+(?P<rest>\S.*)$",
    re.IGNORECASE,
)

# "Start at 45kg" / "Start with 22.5kg" / "Start 12.5kg" / "@ 60kg"
# The "at|with" preposition is optional — Cillian sometimes writes the
# starting weight bare ("Start 12.5kg"). Word boundary on `start` keeps
# "Restart" / "starting" from matching.
_WEIGHT_HINT_RE = re.compile(
    r"(?:\bstart(?:\s+(?:at|with))?\s+|@\s*)(" + _WEIGHT_NUM + r")[ \t]*kg",
    re.IGNORECASE,
)

# "Progress by 2.5kg" / "Progress by 2.5-5kg" / "progress by 2-2.5kg per hand"
_PROGRESS_RE = re.compile(
    r"\bprogress\s+by\s+(" + _WEIGHT_NUM + r")"
    r"(?:[ \t]*-[ \t]*(" + _WEIGHT_NUM + r"))?[ \t]*kg",
    re.IGNORECASE,
)

# "Accumulate 20-30 total reps"
_ACCUMULATE_RE = re.compile(
    r"\baccumulate\s+\d+\s*-\s*(\d+)\s*(?:total\s*)?reps?",
    re.IGNORECASE,
)

# "12-20 total bodyweight reps" — Cillian sometimes omits the "Accumulate"
# keyword and just writes the total-reps target as a standalone line.
# Anchored to start-of-line so this doesn't accidentally consume a
# template-set line like "3 x 8-12 total reps" (the leading "3 x" makes
# that fail the anchor). The qualifier word ("bodyweight", "bw", or
# nothing) is optional.
_TOTAL_REPS_LINE_RE = re.compile(
    r"^\s*\d+\s*-\s*(\d+)\s+total(?:\s+(?:bodyweight|bw))?\s+reps?\b",
    re.IGNORECASE,
)

# Timed work: holds, planks, dead hangs, carries. Cillian writes these as
# "4 x 60 seconds" — structurally identical to a "<sets> x <reps>" template
# line, so _TEMPLATE_SET_RE used to claim it first and ship 4 sets of *60
# reps* into Hevy (seen 2026-09-18, Hollow Hold). The unit word is the only
# thing that distinguishes the two, so these patterns must be tried BEFORE
# _INDIV_SET_RE / _TEMPLATE_SET_RE.
#
# This is the mirror of `_is_duration_based` / `_translate_duration` in
# hevy_to_truecoach.py: a timed set carries `duration_seconds` with null
# reps in both directions, so a hold now round-trips losslessly.
_DURATION_UNIT = r"(?P<unit>seconds?|secs?|s|minutes?|mins?)"

# "4 x 60 seconds" / "3 sets x 30-45s" / "4 x 20kg x 45 seconds"
#   Optional loaded prefix ("20kg x") and optional trailing "@ 10kg" mirror
#   the weighted-carry shape the forward translator emits.
_DURATION_TEMPLATE_RE = re.compile(
    r"""
    ^\s*
    (?P<sets_lo>\d+)(?:\s*-\s*(?P<sets_hi>\d+))?
    (?:\s*sets?)?\s*[x×]\s*
    (?:(?P<weight>""" + _WEIGHT_NUM + r""")[ \t]*kg\s*[x×]\s*)?
    (?P<dur_lo>\d+)(?:\s*-\s*(?P<dur_hi>\d+))?
    \s*""" + _DURATION_UNIT + r"""\b
    (?:\s*holds?\b)?
    (?:\s*@\s*(?P<weight2>""" + _WEIGHT_NUM + r""")[ \t]*kg)?
    """,
    re.IGNORECASE | re.VERBOSE,
)

# A standalone timed set on its own line: "60 seconds", "20kg x 45 seconds",
# "90s hold". Anchored to END of line as well as start, so prose that merely
# mentions a duration ("rest 90 seconds between sets", "hold for 60 seconds
# then switch") stays in notes instead of becoming a set.
_DURATION_INDIV_RE = re.compile(
    r"""
    ^\s*
    (?:(?P<weight>""" + _WEIGHT_NUM + r""")[ \t]*kg\s*[x×]\s*)?
    (?P<dur_lo>\d+)(?:\s*-\s*(?P<dur_hi>\d+))?
    \s*""" + _DURATION_UNIT + r"""\b
    (?:\s*holds?)?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _duration_to_seconds(value: str, unit: str) -> int:
    """Normalise a matched duration token to whole seconds."""
    n = int(value)
    return n * 60 if unit.lower().startswith("m") else n


# "3-5 sets x RIR 2-3" / "4 sets, RIR 2" / "3-5 sets" — set count without
# a numeric rep target. Cillian uses this for bodyweight movements where
# the rep target is "as many as you can while keeping the RIR". We emit
# `<sets_hi>` placeholder sets (weight=0, reps=None) so the exercise
# survives the PUT and Mark fills in reps when he logs it. Anchored to
# line-start so it can't eat unrelated text. Matched ONLY AFTER the
# regular `_TEMPLATE_SET_RE` fails — that one handles the common
# "<sets> x <reps>" case.
_SETS_NO_REPS_RE = re.compile(
    r"""
    ^\s*
    (?P<sets_lo>\d+)(?:\s*-\s*(?P<sets_hi>\d+))?
    \s+sets?\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Demo-video markers — the TC plan often ends with a "Demo video" link
# followed by a "Description: ..." block transcribing the video. Mark
# doesn't want either polluting Hevy notes. The marker can appear inline
# (when the plan is one run-on paragraph) or at line-start.
_DEMO_MARKER_RE = re.compile(
    r"\b(?:demo\s+video|description\s*:)",
    re.IGNORECASE,
)


def _strip_demo_section(plan_text: str) -> str:
    """Truncate plan_text from the first Demo-video / Description: marker."""
    if not plan_text:
        return plan_text
    m = _DEMO_MARKER_RE.search(plan_text)
    if not m:
        return plan_text
    return plan_text[:m.start()].rstrip()


# When TC's DOM hands us the plan as a single run-on paragraph rather than
# proper line-broken text, the per-line parser misses sets / hints / headings.
# This preprocessor inserts \n before known boundary patterns so the parser
# sees them as separate lines.
_INLINE_BOUNDARY_RE = re.compile(
    r"\s+(?="
    r"warm[\s\-]?ups?\b"
    # "Work", "Working", "Work sets", "Working sets" — section heading
    r"|work(?:ing)?(?:\s+sets?)?\b"
    # "Start at Nkg" / "Start with Nkg" / "Start Nkg" — preposition optional
    r"|start\s+(?:at\s+|with\s+)?\d+(?:\.\d+)?\s*kg\b"
    r"|progress\s+by\b"
    r"|each\s+(?:hand|arm|leg|side)\b"
    r"|rir\s+\d"
    r"|bar\s*[x×]"
    # A weight immediately following an "x" is the LOAD half of a loaded
    # set, not the start of a new line — "4 x 20kg x 45 seconds" is one
    # prescription. Without the lookbehind the splitter cut it after
    # "4 x", stranding the set count and shipping a single set.
    r"|(?<![x×]\s)\d+(?:\.\d+)?\s*kg\s*[x×]"
    r"|\d+(?:\s*-\s*\d+)?\s*[x×]\s*\d"
    # "3-5 sets" / "5 sets" — set count with no rep target
    r"|\d+(?:\s*-\s*\d+)?\s+sets?\b"
    r")",
    re.IGNORECASE,
)


def _split_inline_to_lines(plan_text: str) -> str:
    """Inject newlines before known boundaries when the input has none."""
    if not plan_text or "\n" in plan_text:
        return plan_text
    return _INLINE_BOUNDARY_RE.sub("\n", plan_text)

# Big compound barbell lifts. Matches both short TC titles ("Bench", "Squat")
# and longer Hevy titles ("Bench Press (Barbell)"), with optional plural-s.
# Used for progression gating and for rest-interval defaults.
_BIG_LIFT_RE = re.compile(
    r"\b(deadlifts?|squats?|bench|overhead\s+press|ohp)\b",
    re.IGNORECASE,
)

_DUMBBELL_MARKERS = (
    "dumbbell", "db)", "(db)", "per hand", "each hand", "per arm", "each arm",
    # Kettlebells follow the same convention: TC plans are per-hand,
    # Hevy logs the total across both hands.
    "kettlebell", "kb)", "(kb)",
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ParsedSet:
    type: str
    weight_kg: float
    # Optional: None means "unspecified" — emitted for plans that prescribe
    # a set count without a rep target (e.g. "3-5 sets x RIR 2-3"). Hevy
    # renders these as blank, so Mark fills in the actual reps when logging.
    reps: Optional[int]
    # Timed work (hold, plank, dead hang, loaded carry). Mutually exclusive
    # with `reps`: a duration set carries `duration_seconds` and leaves reps
    # None, which is exactly the shape hevy_to_truecoach._is_duration_based
    # looks for on the way back out.
    duration_seconds: Optional[int] = None


@dataclass
class ParsedPlan:
    warmup_sets: list
    working_sets: list
    notes: str
    warnings: list = field(default_factory=list)

    @property
    def sets(self):
        return self.warmup_sets + self.working_sets


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _high_end(lo: str, hi: Optional[str]) -> int:
    return int(hi) if hi is not None else int(lo)


def is_dumbbell(title: str, plan_text: str = "") -> bool:
    """Dumbbell marker in title or plan."""
    blob = f"{title} {plan_text}".lower()
    return any(m in blob for m in _DUMBBELL_MARKERS)


def _parse_weight_token(w_raw: str, has_kg: bool = True) -> float:
    """Turn a matched weight token into kilograms.

    Handles Cillian's dangling-decimal typo: "12 ." / "12." / "12 . " all
    mean 12.5kg (he types the point and drops the 5). Anything else parses
    normally. `bar` is the empty 20kg olympic bar.
    """
    if w_raw is None:
        return 0.0
    tok = re.sub(r"[ \t]+", "", w_raw)
    if tok.lower() == "bar":
        return 20.0
    if tok.endswith("."):
        # Dangling decimal point → the dropped digit is always a 5.
        return float(tok[:-1]) + 0.5
    return float(tok)


def _maybe_double(weight_kg: float, title: str, plan_text: str) -> float:
    if weight_kg == 0:
        return 0.0
    if is_dumbbell(title, plan_text):
        return weight_kg * 2
    return weight_kg


# ---------------------------------------------------------------------------
# Plan parser
# ---------------------------------------------------------------------------

def parse_plan(title: str, plan_text: Optional[str]) -> ParsedPlan:
    """Parse a TrueCoach exercise plan into warmup + working sets + notes."""
    if not plan_text:
        return ParsedPlan(warmup_sets=[], working_sets=[], notes="")

    # Strip Demo-video / Description: tail before any other parsing.
    plan_text = _strip_demo_section(plan_text)
    if not plan_text:
        return ParsedPlan(warmup_sets=[], working_sets=[], notes="")
    # If TC delivered a single run-on paragraph, inject newlines before
    # known headings/sets/hints so the line-by-line parser can see them.
    plan_text = _split_inline_to_lines(plan_text)

    lines = [ln.strip() for ln in plan_text.replace("\r", "").split("\n")]

    warmup_sets: list = []
    working_sets: list = []
    notes_parts: list = []
    warnings: list = []

    section = "pre"
    pending_weight_hint = None
    # Once a section contains an unambiguous "<weight> × <reps>" line (Bar,
    # explicit kg, decimal weight, or magnitude ≥11), subsequent ambiguous
    # "<n> × <r>" lines in the SAME section default to indiv-set rather
    # than template. This makes warmup blocks like:
    #     Bar × 10
    #     30 x5         ← unambiguous: 30 ≥ 11
    #     10 x5         ← ambiguous in isolation, but context says indiv
    # parse uniformly. Reset on every section change.
    section_seen_indiv = False

    # Pre-pass: collect the FIRST "Start at/with Nkg" hint anywhere in the
    # plan so templates that precede the hint line can still use it.
    fallback_hint = None
    m_global = _WEIGHT_HINT_RE.search(plan_text)
    if m_global:
        fallback_hint_raw = _parse_weight_token(m_global.group(1))
        # Decide doubling based on the full plan context.
        fallback_hint = fallback_hint_raw * 2 if is_dumbbell(title, plan_text) else fallback_hint_raw

    # Progression step for within-session ramping. Gated on the title being a
    # big barbell lift (squat/bench/deadlift/OHP); for accessories like
    # dumbbell row we stay flat regardless of the hint.
    #   "Progress by 2.5-5kg"  → step = 5  (HIGH end of the range)
    #   "Progress by 2.5kg"    → step = 2.5 (single-value is still applied)
    progress_step = None
    m_prog = _PROGRESS_RE.search(plan_text)
    if m_prog and _BIG_LIFT_RE.search(title):
        step_raw = _parse_weight_token(
            m_prog.group(2) if m_prog.group(2) is not None else m_prog.group(1)
        )
        progress_step = step_raw * 2 if is_dumbbell(title, plan_text) else step_raw

    for line in lines:
        if not line:
            continue

        # Line-only section headings (e.g. "Warm-up", "Working sets")
        if _WARMUP_HEADING.match(line):
            section = "warmup"
            section_seen_indiv = False
            continue
        if _WORKING_HEADING.match(line):
            section = "working"
            section_seen_indiv = False
            continue

        # Inline heading: strip prefix and re-process as content
        m_inline = _INLINE_HEADING_RE.match(line)
        if m_inline:
            which = m_inline.group("which").lower()
            if "warm" in which:
                section = "warmup"
            else:
                section = "working"
            section_seen_indiv = False
            line = m_inline.group("rest").strip()
            if not line:
                continue

        # "Accumulate N-M total reps"
        m_accum = _ACCUMULATE_RE.search(line)
        if m_accum:
            total = int(m_accum.group(1))
            working_sets.append(ParsedSet(type="normal", weight_kg=0, reps=total))
            notes_parts.append(line)
            if section == "pre":
                section = "working"
            continue

        # "N-M total [bodyweight] reps" — same intent as Accumulate, just
        # phrased without the keyword. Take the HIGH end as the rep target
        # and emit a single bodyweight set (weight_kg=0) so Hevy keeps the
        # exercise. Anchored to line-start so it can't eat a template line.
        m_total = _TOTAL_REPS_LINE_RE.match(line)
        if m_total:
            total = int(m_total.group(1))
            working_sets.append(ParsedSet(type="normal", weight_kg=0, reps=total))
            notes_parts.append(line)
            if section == "pre":
                section = "working"
            continue

        # Timed work — MUST be tried before the rep-based set patterns,
        # which would otherwise read "4 x 60 seconds" as 4 sets of 60 reps.
        m_dur_tmpl = _DURATION_TEMPLATE_RE.match(line)
        m_dur_indiv = None if m_dur_tmpl else _DURATION_INDIV_RE.match(line)
        if m_dur_tmpl or m_dur_indiv:
            m_dur = m_dur_tmpl or m_dur_indiv
            # Duration ranges take the HIGH end, matching how working rep
            # ranges are treated — it's the target, not the floor.
            secs = _duration_to_seconds(
                m_dur.group("dur_hi") or m_dur.group("dur_lo"),
                m_dur.group("unit"),
            )
            nsets = (
                _high_end(m_dur_tmpl.group("sets_lo"), m_dur_tmpl.group("sets_hi"))
                if m_dur_tmpl else 1
            )
            w_raw = m_dur.group("weight")
            if m_dur_tmpl and not w_raw:
                w_raw = m_dur_tmpl.group("weight2")
            if w_raw:
                weight = _maybe_double(_parse_weight_token(w_raw), title, plan_text)
            elif pending_weight_hint is not None:
                weight = pending_weight_hint
            elif fallback_hint is not None:
                weight = fallback_hint
            else:
                weight = 0.0
            set_type = "warmup" if section == "warmup" else "normal"
            if section == "pre":
                section = "working"
            target = warmup_sets if set_type == "warmup" else working_sets
            for _ in range(nsets):
                target.append(
                    ParsedSet(set_type, weight, None, duration_seconds=secs)
                )
            # Keep the whole line as a note. Unlike the template path we do
            # NOT split it at the match end — "4 x 60 seconds" reads as one
            # prescription, and splitting it produced the stray "seconds"
            # line seen in Hevy notes on 2026-09-18.
            notes_parts.append(line)
            continue

        # Explicit weight hint that is NOT also a set ("Start at 45kg")
        m_hint = _WEIGHT_HINT_RE.search(line)
        if m_hint and not _INDIV_SET_RE.match(line) and not _TEMPLATE_SET_RE.match(line):
            raw_weight = _parse_weight_token(m_hint.group(1))
            if is_dumbbell(title, line) or is_dumbbell(title, plan_text):
                pending_weight_hint = raw_weight * 2
            else:
                pending_weight_hint = raw_weight
            # "Start at N kg" implies the next "<n> × <r>" line is a
            # template that uses the hint as weight, not another indiv-set
            # warmup. Clear the sticky-indiv flag so the magnitude
            # heuristic kicks back in.
            section_seen_indiv = False
            notes_parts.append(line)

            # Inline template after the hint, e.g. "Start with 50kg 4 x 8-10".
            # Process the trailing portion as a template line so the sets
            # aren't lost. The just-captured hint supplies the weight.
            tail_after_hint = line[m_hint.end():].lstrip(" ,;:-")
            m_tail_tmpl = _TEMPLATE_SET_RE.match(tail_after_hint)
            if m_tail_tmpl:
                if section == "warmup":
                    section = "working"
                    section_seen_indiv = False
                if section == "pre":
                    section = "working"
                nsets = _high_end(
                    m_tail_tmpl.group("sets_lo"),
                    m_tail_tmpl.group("sets_hi"),
                )
                reps = _high_end(
                    m_tail_tmpl.group("reps_lo"),
                    m_tail_tmpl.group("reps_hi"),
                )
                # Weight precedence: explicit `@ Nkg` on the template wins
                # over the hint we just captured.
                inline_weight = m_tail_tmpl.group("weight")
                if inline_weight:
                    base = _maybe_double(float(inline_weight), title, plan_text)
                else:
                    base = pending_weight_hint
                step = progress_step if (base > 0 and progress_step) else 0
                for i in range(nsets):
                    working_sets.append(
                        ParsedSet("normal", base + step * i, reps)
                    )
            continue

        # Individual-set prefix: "<weight>kg × <reps>"
        m_indiv = _INDIV_SET_RE.match(line)
        m_tmpl = _TEMPLATE_SET_RE.match(line)

        # Disambiguate: an individual set is one of:
        #   * "Bar" × reps
        #   * "<n>kg" × reps   (explicit kg suffix)
        #   * "<n>" × reps where n is large enough to be a weight (>=11
        #     integer, or contains a decimal like 22.5) — nobody does that
        #     many sets of one exercise.
        #   * "<n>" × reps in a section that has already produced an
        #     unambiguous indiv-set line — the surrounding format wins
        #     over the magnitude heuristic (so a "10 x5" warmup parses
        #     correctly when it follows "Bar × 10").
        # Otherwise "<n> × <r>" is a working-set template.
        def _is_indiv_set(m, seen_indiv_in_section: bool):
            if m is None:
                return False
            w = (m.group("weight") or "")
            kg = m.group("kg")
            if w.strip().lower() == "bar":
                return True
            if kg is not None:
                return True
            # No kg suffix: decide by magnitude or by section context.
            # `_parse_weight_token` (not bare float) so a dangling-decimal
            # typo like "12 . x 5" is still recognised as a weight.
            try:
                val = _parse_weight_token(w)
            except ValueError:
                return False
            if "." in w:
                return True       # decimal → weight
            if val >= 11:
                return True       # 1..10 is a set count; 11+ is a weight
            # Magnitude is ambiguous (1..10). Inherit from the section:
            # if a prior line in this section already established the
            # weight×reps cadence, this one is almost certainly the same.
            return seen_indiv_in_section

        if _is_indiv_set(m_indiv, section_seen_indiv):
            w_raw = m_indiv.group("weight")
            weight = _parse_weight_token(w_raw, True)
            amrap = m_indiv.group("amrap")
            reps_lo = m_indiv.group("reps_lo")
            reps_hi = m_indiv.group("reps_hi")
            # Working sets with "N+" suffix = max effort: Mark wants that
            # expressed as a 12-rep target so he still gets a sensible UI goal.
            # Warmup rep RANGES take the LOW end (prep, not work).
            # Working rep ranges take the HIGH end (the target).
            if amrap and section != "warmup":
                reps = 12
            elif section == "warmup":
                reps = int(reps_lo)
            else:
                reps = _high_end(reps_lo, reps_hi)
            if section != "warmup":
                weight = _maybe_double(weight, title, plan_text)
            set_type = "warmup" if section == "warmup" else "normal"
            if section == "pre":
                section = "working"
            section_seen_indiv = True
            target = warmup_sets if set_type == "warmup" else working_sets
            target.append(ParsedSet(set_type, weight, reps))
            # Keep trailing tail as a note (e.g. RIR per set)
            tail = line[m_indiv.end():].strip()
            if tail:
                notes_parts.append(tail)
            continue

        if m_tmpl:
            # A template line implicitly ends a warmup section (individual
            # warmup tokens are "<weight> × reps", NOT "<setcount> × reps").
            if section == "warmup":
                section = "working"
                section_seen_indiv = False
            if section == "pre":
                section = "working"

            nsets = _high_end(m_tmpl.group("sets_lo"), m_tmpl.group("sets_hi"))
            reps = _high_end(m_tmpl.group("reps_lo"), m_tmpl.group("reps_hi"))
            weight_raw = m_tmpl.group("weight")
            if weight_raw:
                base = _maybe_double(_parse_weight_token(weight_raw),
                                     title, plan_text)
            elif pending_weight_hint is not None:
                base = pending_weight_hint
            elif fallback_hint is not None:
                base = fallback_hint
            else:
                base = 0.0
            # Progressive weights within the session when we know both a
            # starting weight and a progression step.
            step = progress_step if (base > 0 and progress_step) else 0
            for i in range(nsets):
                working_sets.append(ParsedSet("normal", base + step * i, reps))
            # Preserve the template portion of the line so the Hevy user can
            # still see the prescription like "4-5 x 10-12".
            template_portion = line[:m_tmpl.end()].strip()
            if template_portion:
                notes_parts.append(template_portion)
            # Drop a dangling separator ("4 sets x 8-12 reps / RIR 2" gets
            # split before "RIR", leaving a bare "/" as the tail).
            tail = line[m_tmpl.end():].strip().lstrip("/|,;-").strip()
            if tail:
                notes_parts.append(tail)
            continue

        # "<n>-<m> sets x RIR ..." / "<n> sets" — set count with no
        # numeric rep target. Emit <sets_hi> placeholder sets so the
        # exercise survives the PUT; Mark fills in the actual reps in
        # Hevy. Bodyweight (weight=0) for now; if a weight hint is
        # present we still respect it.
        m_sets_only = _SETS_NO_REPS_RE.match(line)
        if m_sets_only:
            if section == "warmup":
                section = "working"
                section_seen_indiv = False
            if section == "pre":
                section = "working"
            nsets = _high_end(m_sets_only.group("sets_lo"),
                              m_sets_only.group("sets_hi"))
            if pending_weight_hint is not None:
                base = pending_weight_hint
            elif fallback_hint is not None:
                base = fallback_hint
            else:
                base = 0.0
            for _ in range(nsets):
                working_sets.append(ParsedSet("normal", base, None))
            notes_parts.append(line)
            continue

        # Didn't match any set pattern → keep for notes
        notes_parts.append(line)

    # Notes: preserve order, drop blank lines.
    notes = "\n".join(notes_parts).strip()
    # Collapse whitespace runs
    notes = re.sub(r"[ \t]+", " ", notes)
    notes = re.sub(r"\n{3,}", "\n\n", notes)

    if not working_sets and not warmup_sets:
        warnings.append(f"No sets parsed from plan for '{title}'")

    return ParsedPlan(
        warmup_sets=warmup_sets,
        working_sets=working_sets,
        notes=notes,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Exercise resolver (unchanged from previous iteration)
# ---------------------------------------------------------------------------

class ExerciseResolver:
    """Resolve a TrueCoach exercise title → best matching Hevy template_id."""

    _PAREN_RE = re.compile(r"\s*\(([^)]*)\)")
    _NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
    _MULTISPACE = re.compile(r"\s+")

    # Lifting acronyms — expanded post-normalisation so the resolver can
    # match canonical Hevy names. Conservative list; add cautiously.
    _ACRONYMS = (
        (re.compile(r"\brdls?\b"), "romanian deadlift"),
        (re.compile(r"\bohps?\b"), "overhead press"),
        (re.compile(r"\bsl\b"),    "single leg"),
        (re.compile(r"\bdb\b"),    "dumbbell"),
    )

    # Trailing video-file artefacts that leak from TC titles (e.g.
    # "ISSA Exercise Library Machine Seated Row mp4"). Stripped during
    # normalisation so token-set matches still hit.
    _MEDIA_EXT_RE = re.compile(r"\b(?:mp4|mov|mp3|webm|m4a|m4v)\b")

    def __init__(self, history, templates, overrides=None):
        self.history = list(history)
        self.templates = list(templates)
        # Approved manual overrides — keyed by normalised TC title.
        # Each value: {"exercise_template_id", "resolved_title",
        #              "notes_prefix"?, "per_hand"?, "approved_at"?}
        # `per_hand`: the Hevy template loads two implements (dumbbells /
        # kettlebells) but the TC title doesn't say so, so plan weights are
        # per hand and must be doubled — see `_apply_per_hand_override`.
        self.overrides: dict = dict(overrides or {})
        self._hist_by_norm: dict = {}
        self._hist_by_tokens: dict = {}
        for row in self.history:
            n = self._normalize(row["title"])
            prev = self._hist_by_norm.get(n)
            rank = (row["count"], row.get("last_seen") or "")
            if prev is None or rank > (prev["count"], prev.get("last_seen") or ""):
                self._hist_by_norm[n] = row
            tk = frozenset(n.split()) if n else frozenset()
            if tk:
                prev_t = self._hist_by_tokens.get(tk)
                if prev_t is None or rank > (prev_t["count"],
                                             prev_t.get("last_seen") or ""):
                    self._hist_by_tokens[tk] = row
        self._cat_by_norm: dict = {}
        self._cat_by_tokens: dict = {}
        for t in self.templates:
            n = self._normalize(t["title"])
            # Prefer built-in over custom on tie — Mark sometimes accidentally
            # creates custom duplicates of built-in exercises (e.g. "Skull
            # Crusher (Dumbbell)" custom vs built-in "Skullcrusher (Dumbbell)").
            prev = self._cat_by_norm.get(n)
            if prev is None or (
                not t.get("is_custom") and prev.get("is_custom")
            ):
                self._cat_by_norm[n] = t
            tk = frozenset(n.split()) if n else frozenset()
            if tk:
                prev_t = self._cat_by_tokens.get(tk)
                if prev_t is None or (
                    not t.get("is_custom") and prev_t.get("is_custom")
                ):
                    self._cat_by_tokens[tk] = t

    @classmethod
    def _normalize_key(cls, s: str) -> str:
        """Light normalisation — used as the override-file key. Skips the
        synonym/acronym substitutions so override keys match the raw TC
        title (lowercased + paren-stripped). The full _normalize used for
        history/catalog lookup is heavier."""
        s = (s or "").lower().strip()
        s = cls._PAREN_RE.sub(r" \1", s)
        s = cls._NON_ALNUM.sub(" ", s)
        s = cls._MULTISPACE.sub(" ", s).strip()
        return s

    @classmethod
    def _normalize(cls, s: str) -> str:
        s = (s or "").lower().strip()
        # Strip common library-prefix noise from TrueCoach
        s = re.sub(r"\bissa exercise library\b", "", s)
        s = cls._PAREN_RE.sub(r" \1", s)
        s = cls._NON_ALNUM.sub(" ", s)
        s = cls._MULTISPACE.sub(" ", s).strip()
        s = s.replace("tricep ", "triceps ").replace("bicep ", "biceps ")
        # word-boundary regex so we match start-of-string too (e.g. "Stiff Leg
        # Deadlift" — the leading-space-only replace never fired on these).
        s = re.sub(r"\bstiff leg\b", "straight leg", s)
        # Drop video-file-extension artefacts before acronym expansion so
        # they don't leak into the token set.
        s = cls._MEDIA_EXT_RE.sub("", s)
        s = cls._MULTISPACE.sub(" ", s).strip()
        for pat, replacement in cls._ACRONYMS:
            s = pat.sub(replacement, s)
        # Coaching-vocab synonyms (TC name → Hevy catalog name)
        s = re.sub(r"\bback raise\b", "back extension", s)
        s = re.sub(r"\bhyper\b", "hyperextension", s)
        s = re.sub(r"\bflye?s?\b", "fly", s)              # flye/flyes → fly
        s = re.sub(r"\bchin ?ups?\b", "chin up", s)
        s = re.sub(r"\bpush ?ups?\b", "push up", s)
        return s

    def lookup_override(self, tc_title: str):
        """Return the approved override dict for this TC title, or None."""
        return self.overrides.get(self._normalize_key(tc_title))

    def resolve(self, tc_title: str):
        """Returns (template_id, template_title, confidence, alternatives).

        Approved overrides win over everything else.
        """
        ov = self.lookup_override(tc_title)
        if ov:
            return (ov["exercise_template_id"], ov["resolved_title"],
                    "approved", [])
        norm = self._normalize(tc_title)

        if norm in self._hist_by_norm:
            row = self._hist_by_norm[norm]
            return (row["id"], row["title"], "history", [])
        if norm in self._cat_by_norm:
            t = self._cat_by_norm[norm]
            return (t["id"], t["title"], "catalog-exact", [])

        # Token-set match — same tokens, possibly reordered or with
        # parens around different ones (e.g. "Barbell Overhead Press" →
        # "Overhead Press (Barbell)"). Treat as STRONG since the tokens
        # are an exact set match.
        tk = frozenset(norm.split()) if norm else frozenset()
        if tk:
            if tk in self._hist_by_tokens:
                row = self._hist_by_tokens[tk]
                return (row["id"], row["title"], "history", [])
            if tk in self._cat_by_tokens:
                t = self._cat_by_tokens[tk]
                return (t["id"], t["title"], "catalog-exact", [])

        def tokens(s): return set(s.split())
        want = tokens(norm)
        if not want:
            return (None, None, "no-match", [])

        import math
        scored = []
        for n, row in self._hist_by_norm.items():
            have = tokens(n)
            inter = want & have
            if not inter:
                continue
            j = len(inter) / max(len(want | have), 1)
            subset = 0.5 if want.issubset(have) else 0.0
            freq = min(0.15, 0.05 * math.log(row["count"] + 1))
            total = j + subset + freq
            scored.append((total, row["count"], row["id"], row["title"], "history-fuzzy", j))
        for n, t in self._cat_by_norm.items():
            have = tokens(n)
            inter = want & have
            if not inter:
                continue
            j = len(inter) / max(len(want | have), 1)
            subset = 0.2 if want.issubset(have) else 0.0
            # Prefer built-in catalog entries on tie (small nudge).
            builtin_nudge = 0.05 if not t.get("is_custom") else 0.0
            total = j + subset + builtin_nudge
            scored.append((total, 0, t["id"], t["title"], "catalog-fuzzy", j))
        if not scored:
            return (None, None, "no-match", [])
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        top = scored[0]
        alt = [{"id": r[2], "title": r[3], "score": r[5], "source": r[4]} for r in scored[1:6]]
        # Confidence tiers — based on total score (jaccard + subset bonus
        # + frequency bonus), not raw jaccard, so single-token queries
        # like "Bench" or "Squat" can still resolve as strong when their
        # subset/frequency signals are decisive.
        #   <source>-weak    total < 0.4
        #   <source>          0.4 ≤ total < 0.7
        #   <source>-strong   total ≥ 0.7 AND (
        #                       margin ≥ 0.1 over next alt
        #                       OR top is from history with count clearly
        #                          dominant over the next match (≥ 2×)
        #                     )
        top_total = top[0]
        top_count = top[1]
        source = top[4]
        next_total = scored[1][0] if len(scored) > 1 else 0.0
        next_count = scored[1][1] if len(scored) > 1 else 0
        next_source = scored[1][4] if len(scored) > 1 else None
        margin = top_total - next_total

        is_history = source == "history-fuzzy"
        next_is_history = next_source == "history-fuzzy" if next_source else False

        is_strong_by_margin = top_total >= 0.7 and margin >= 0.1
        # "Frequency-dominant": Mark uses the top template ≥ 2× more often
        # than the runner-up. Resolves tie-by-tokens cases like
        # "Squat" → Squat (Barbell) when Zercher Squat ties on tokens.
        is_strong_by_freq = (
            is_history and top_count > 0 and top_total >= 0.7 and (
                not next_is_history
                or next_count == 0
                or top_count >= 2 * next_count
            )
        )

        if top_total < 0.4:
            confidence = f"{source}-weak"
        elif is_strong_by_margin or is_strong_by_freq:
            confidence = f"{source}-strong"
        else:
            confidence = source
        return (top[2], top[3], confidence, alt)


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def _discover_personal_automation() -> str:
    """Find the personal_automation/ mount across sessions."""
    import os
    from pathlib import Path
    sessions = Path("/sessions")
    try:
        if sessions.exists():
            for s in sorted(sessions.iterdir()):
                p = s / "mnt" / "personal_automation"
                try:
                    if p.exists():
                        return str(p)
                except (PermissionError, OSError):
                    continue
    except (PermissionError, OSError):
        pass
    return str(Path(__file__).resolve().parent)


def load_resolver(
    history_path: str = None,
    templates_path: str = None,
    overrides_path: str = None,
) -> ExerciseResolver:
    """Load the resolver with history, catalog, and approved overrides.

    Paths default to the discovered personal_automation/ mount.
    """
    import json
    import os
    base = _discover_personal_automation()
    history_path  = history_path  or os.path.join(base, "hevy_exercise_history.json")
    templates_path = templates_path or os.path.join(base, "hevy_exercise_templates.json")
    overrides_path = overrides_path or os.path.join(base, "tc_mapping_overrides.json")

    with open(history_path) as f:
        hist = json.load(f)
    with open(templates_path) as f:
        tpl = json.load(f)

    overrides = {}
    if os.path.exists(overrides_path):
        try:
            with open(overrides_path) as f:
                overrides = json.load(f) or {}
        except (json.JSONDecodeError, OSError):
            overrides = {}
    return ExerciseResolver(hist, tpl, overrides=overrides)


def _rest_seconds_for(resolved_title: Optional[str]) -> int:
    """Default rest per exercise. Per Mark's preference:
      - Deadlift (barbell)  → 150s
      - Squat (barbell)     → 120s
      - Everything else     → 90s
    """
    if not resolved_title:
        return 90
    t = resolved_title.lower()
    # Exclude variants that aren't the heavy barbell versions.
    if "machine" in t or "smith" in t or "dip" in t or "dumbbell" in t:
        return 90
    if "deadlift" in t:
        return 150
    if "squat" in t:
        return 120
    return 90


# Placeholder set shipped when the parser can't extract any sets from the
# plan text. Hevy's PUT /v1/routines/{id} silently drops exercises whose
# `sets` array is empty (returns 200 with the exercise missing from the
# response), so we always emit at least one set to guarantee the exercise
# survives. weight_kg/reps are null so it reads as "fill me in" in Hevy.
_FALLBACK_SET = {
    "type": "normal", "weight_kg": None, "reps": None,
    "distance_meters": None, "duration_seconds": None, "custom_metric": None,
}


def _sets_payload_with_fallback(parsed) -> list:
    """Convert ParsedPlan.sets to Hevy-shape dicts, inserting a single
    placeholder set when no sets were parsed. Adds a warning so callers
    can surface the fallback (see also bidirectional_sync.validate_put_response).

    Every optional metric key is emitted explicitly (null when unused) so
    callers can PUT the payload as-is. A timed set carries duration_seconds
    with reps null; a counted set is the reverse.
    """
    out = [
        {
            "type": s.type,
            "weight_kg": s.weight_kg,
            "reps": s.reps,
            "distance_meters": None,
            "duration_seconds": s.duration_seconds,
            "custom_metric": None,
        }
        for s in parsed.sets
    ]
    if not out:
        out.append(dict(_FALLBACK_SET))
        parsed.warnings.append(
            "Inserted placeholder set (no sets parsed) — fill in reps/weight in Hevy"
        )
    return out


def _apply_per_hand_override(override: dict, parsed, title: str,
                             plan_text: Optional[str]) -> None:
    """Double working-set weights for an override flagged `per_hand`.

    Why this exists: Hevy records dumbbell/kettlebell work as the TOTAL
    across both hands, while TrueCoach plans are written per hand. The
    normal detection (`is_dumbbell`) reads the TC *title* and plan text, so
    it can't see that an override redirects a neutrally-named TC exercise
    onto a two-implement Hevy template — e.g. TC "Step Up" → Hevy "Dumbbell
    Step Up". Without this, weights round-trip lossily: Hevy→TC halves on
    the way out but TC→Hevy never doubled on the way back in.

    Only working sets are scaled, mirroring `_maybe_double`, which leaves
    warmups alone. No-ops when the plan text already made `is_dumbbell`
    true, so a plan saying "each hand" can't get doubled twice.
    """
    if not override.get("per_hand"):
        return
    if is_dumbbell(title, plan_text or ""):
        return  # already doubled during parsing
    for s in parsed.working_sets:
        if s.weight_kg:
            s.weight_kg *= 2


def build_hevy_exercise(
    title: str,
    plan_text: Optional[str],
    resolver: ExerciseResolver,
    position_code: Optional[str] = None,
    superset_id=None,
    form_tip: Optional[str] = None,
) -> dict:
    """Assemble a preview dict for a single Hevy routine exercise.

    If `form_tip` is provided, a "---\\nCoach Tip: ..." block is appended
    after the parsed notes. The same parsing happens regardless — the tip
    is layered on at the end so cache hashes for the plan stay stable
    when only the tip changes (callers can hash before vs. after).
    """
    parsed = parse_plan(title, plan_text)
    if form_tip:
        # Lazy import — feedback_tips imports nothing from this module so
        # there's no cycle, but keeping the dependency local makes it
        # easy to swap out and avoids surprising consumers.
        from feedback_tips import append_form_tip
        parsed.notes = append_form_tip(parsed.notes, form_tip)

    # 1. Approved override wins everything else.
    override = resolver.lookup_override(title)
    if override:
        tid    = override["exercise_template_id"]
        ttitle = override["resolved_title"]
        conf   = "approved"
        alt    = []
        prefix = override.get("notes_prefix")
        if prefix:
            parsed.notes = (prefix + "\n" + parsed.notes).strip()
        _apply_per_hand_override(override, parsed, title, plan_text)
        sets_payload = _sets_payload_with_fallback(parsed)
        return {
            "tc_title": title,
            "position_code": position_code,
            "superset_id": superset_id,
            "resolved_title": ttitle,
            "exercise_template_id": tid,
            "confidence": conf,
            "alternatives": alt,
            "rest_seconds": _rest_seconds_for(ttitle),
            "notes": parsed.notes,
            "sets": sets_payload,
            "warnings": parsed.warnings,
        }

    # 2. Base resolution.
    tid, ttitle, conf, alt = resolver.resolve(title)

    # Prefer a loaded variant when the plan indicates weights. Two patterns:
    #   1. "weighted" keyword in plan → try "{title} weighted"
    #      (e.g. TC "Chin-Up" + "Weighted / Start at 2.5kg" → "Chin Up (Weighted)")
    #   2. plan indicates dumbbell loading (e.g. "each hand", explicit kg hint
    #      on a non-barbell movement) → try "dumbbell {title}"
    #      (e.g. TC "Step Up" + "12.5kg each hand" → "Dumbbell Step Up")
    # Only accept the alternate if it resolves to a *different* template with
    # strong confidence (exact history or exact catalog hit).
    STRONG = {"history", "catalog-exact"}
    candidates = []
    if plan_text:
        if re.search(r"\bweighted\b", plan_text, re.IGNORECASE):
            candidates.append(f"{title} weighted")
        if is_dumbbell(title, plan_text) and "dumbbell" not in title.lower():
            candidates.append(f"{title} (dumbbell)")  # paren-suffix variant
            candidates.append(f"dumbbell {title}")
    for cand in candidates:
        c_tid, c_ttl, c_conf, c_alt = resolver.resolve(cand)
        if not c_tid:
            continue
        # Switch to a different (strong) template when the candidate
        # promotion finds a more specific variant (e.g. "Chin-Up" +
        # "Weighted" plan → "Chin Up (Weighted)").
        if c_tid != tid and c_conf in STRONG:
            tid, ttitle, conf, alt = c_tid, c_ttl, c_conf, c_alt
            break
        # Or upgrade the same template's confidence when a stronger
        # variant of the title hits catalog-exact (e.g. "Single Leg RDL"
        # base catalog-fuzzy → "Single Leg RDL (dumbbell)" catalog-exact).
        if c_tid == tid and c_conf in STRONG and conf not in STRONG:
            conf = c_conf
            break

    sets_payload = _sets_payload_with_fallback(parsed)
    return {
        "tc_title": title,
        "position_code": position_code,
        "superset_id": superset_id,
        "resolved_title": ttitle,
        "exercise_template_id": tid,
        "confidence": conf,
        "alternatives": alt,
        "rest_seconds": _rest_seconds_for(ttitle),
        "notes": parsed.notes,
        "sets": sets_payload,
        "warnings": parsed.warnings,
    }
