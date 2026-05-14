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
      * dumbbell exercise + "each hand" → DOUBLE to get Hevy total weight
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

# Individual-set line prefix:  weight  ×  reps
#   Matches from start of line; caller uses .end() to get trailing text.
#   AMRAP marker `+` may have whitespace before it ("5 +" as well as "5+").
_INDIV_SET_RE = re.compile(
    r"""
    ^\s*
    (?P<weight>bar|\d+(?:\.\d+)?)\s*(?P<kg>kg)?
    \s*[x×]\s*
    (?P<reps_lo>\d+)(?:\s*-\s*(?P<reps_hi>\d+))?
    \s*(?P<amrap>\+)?
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Template-set line prefix:  <sets> × <reps>  with optional "@ kg"
_TEMPLATE_SET_RE = re.compile(
    r"""
    ^\s*
    (?P<sets_lo>\d+)(?:\s*-\s*(?P<sets_hi>\d+))?
    \s*[x×]\s*
    (?P<reps_lo>\d+)(?:\s*-\s*(?P<reps_hi>\d+))?
    \s*\+?\s*(?:reps?)?
    (?:\s*@\s*(?P<weight>\d+(?:\.\d+)?)\s*kg)?
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Inline heading that prefixes content on the same line,
# e.g. "Warm-up bar x 10" or "Working sets 5 x 5 @ 60kg"
_INLINE_HEADING_RE = re.compile(
    r"^\s*(?P<which>warm[\s\-]?ups?|work(?:ing)?(?:\s+sets?)?)\s+(?P<rest>\S.*)$",
    re.IGNORECASE,
)

# "Start at 45kg" / "Start with 22.5kg" / "@ 60kg"
_WEIGHT_HINT_RE = re.compile(
    r"\b(?:start\s+(?:at|with)|@)\s*(\d+(?:\.\d+)?)\s*kg",
    re.IGNORECASE,
)

# "Progress by 2.5kg" / "Progress by 2.5-5kg" / "progress by 2-2.5kg per hand"
_PROGRESS_RE = re.compile(
    r"\bprogress\s+by\s+(\d+(?:\.\d+)?)(?:\s*-\s*(\d+(?:\.\d+)?))?\s*kg",
    re.IGNORECASE,
)

# "Accumulate 20-30 total reps"
_ACCUMULATE_RE = re.compile(
    r"\baccumulate\s+\d+\s*-\s*(\d+)\s*(?:total\s*)?reps?",
    re.IGNORECASE,
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
    r"|start\s+(?:at|with)\b"
    r"|progress\s+by\b"
    r"|each\s+(?:hand|arm|leg|side)\b"
    r"|rir\s+\d"
    r"|bar\s*[x×]"
    r"|\d+(?:\.\d+)?\s*kg\s*[x×]"
    r"|\d+(?:\s*-\s*\d+)?\s*[x×]\s*\d"
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
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ParsedSet:
    type: str
    weight_kg: float
    reps: int


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


def _parse_weight_token(w_raw: str, has_kg: bool) -> float:
    if w_raw is None:
        return 0.0
    if w_raw.lower() == "bar":
        return 20.0
    return float(w_raw)


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
        fallback_hint_raw = float(m_global.group(1))
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
        step_raw = float(m_prog.group(2) if m_prog.group(2) is not None else m_prog.group(1))
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

        # Explicit weight hint that is NOT also a set ("Start at 45kg")
        m_hint = _WEIGHT_HINT_RE.search(line)
        if m_hint and not _INDIV_SET_RE.match(line) and not _TEMPLATE_SET_RE.match(line):
            raw_weight = float(m_hint.group(1))
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
            if w.lower() == "bar":
                return True
            if kg is not None:
                return True
            # No kg suffix: decide by magnitude or by section context.
            try:
                val = float(w)
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
                base = _maybe_double(float(weight_raw), title, plan_text)
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
            tail = line[m_tmpl.end():].strip()
            if tail:
                notes_parts.append(tail)
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
        #              "notes_prefix"?, "approved_at"?}
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


def build_hevy_exercise(
    title: str,
    plan_text: Optional[str],
    resolver: ExerciseResolver,
    position_code: Optional[str] = None,
    superset_id=None,
    form_tip: Optional[str] = None,
) -> dict:
    """Assemble a preview dict for a single Hevy routine exercise.

    If `form_tip` is provided, a "---\\nForm Tip: ..." block is appended
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
        sets_payload = [
            {"type": s.type, "weight_kg": s.weight_kg, "reps": s.reps}
            for s in parsed.sets
        ]
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

    sets_payload = [
        {"type": s.type, "weight_kg": s.weight_kg, "reps": s.reps}
        for s in parsed.sets
    ]
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
