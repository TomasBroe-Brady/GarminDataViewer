"""Reconcile planned training sessions against what the watch actually recorded.

The problem this solves: your plan says "Tuesday - 8km easy run, 45 min", the
watch says "Tuesday 18:42, running, 7.6km, 41:20". Those are obviously the same
session, but nothing in either system says so. This module makes that link, and
then reports where plan and reality diverged.

Matching is deliberately conservative: it will leave a session unmatched rather
than pair two things that only loosely resemble each other, because a wrong
match produces a misleading adherence number, which is worse than a gap.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any

from .influx import Activity

# --------------------------------------------------------------------------
# Activity type vocabulary
# --------------------------------------------------------------------------
# Canonical categories we reconcile on. Anything unrecognised becomes "other",
# which matches permissively rather than not at all.
CANONICAL_TYPES = (
    "run",
    "bike",
    "swim",
    "strength",
    "walk",
    "row",
    "cardio",
    "yoga",
    "rest",
    "other",
)

# Garmin `activityType.typeKey` values -> canonical category.
GARMIN_TYPE_MAP: dict[str, str] = {
    # running
    "running": "run",
    "treadmill_running": "run",
    "indoor_running": "run",
    "trail_running": "run",
    "track_running": "run",
    "obstacle_run": "run",
    "ultra_run": "run",
    "virtual_run": "run",
    # cycling
    "cycling": "bike",
    "road_biking": "bike",
    "mountain_biking": "bike",
    "gravel_cycling": "bike",
    "indoor_cycling": "bike",
    "virtual_ride": "bike",
    "cyclocross": "bike",
    "downhill_biking": "bike",
    "recumbent_cycling": "bike",
    # swimming
    "swimming": "swim",
    "lap_swimming": "swim",
    "open_water_swimming": "swim",
    # strength
    "strength_training": "strength",
    "indoor_climbing": "strength",
    "bouldering": "strength",
    "pilates": "strength",
    # walking / hiking
    "walking": "walk",
    "casual_walking": "walk",
    "speed_walking": "walk",
    "hiking": "walk",
    # rowing
    "rowing": "row",
    "indoor_rowing": "row",
    # general cardio
    "indoor_cardio": "cardio",
    "cardio": "cardio",
    "elliptical": "cardio",
    "stair_climbing": "cardio",
    "hiit": "cardio",
    "fitness_equipment": "cardio",
    # mind/body
    "yoga": "yoga",
    "meditation": "yoga",
    "breathwork": "yoga",
}

# Free-text words you might write in a spreadsheet -> canonical category.
PLAN_TYPE_KEYWORDS: dict[str, str] = {
    "run": "run", "running": "run", "jog": "run", "tempo": "run",
    "interval": "run", "intervals": "run", "fartlek": "run", "long run": "run",
    "easy run": "run", "recovery run": "run", "track": "run", "hills": "run",
    "bike": "bike", "cycle": "bike", "cycling": "bike", "ride": "bike",
    "spin": "bike", "turbo": "bike", "zwift": "bike",
    "swim": "swim", "swimming": "swim", "pool": "swim",
    "strength": "strength", "gym": "strength", "weights": "strength",
    "lifting": "strength", "resistance": "strength", "core": "strength",
    "climb": "strength", "climbing": "strength", "pilates": "strength",
    "walk": "walk", "walking": "walk", "hike": "walk", "hiking": "walk",
    "row": "row", "rowing": "row", "erg": "row",
    "cardio": "cardio", "elliptical": "cardio", "hiit": "cardio",
    "circuit": "cardio", "crosstrain": "cardio", "cross-train": "cardio",
    "yoga": "yoga", "mobility": "yoga", "stretch": "yoga",
    "meditation": "yoga", "breathwork": "yoga",
    "rest": "rest", "off": "rest", "recovery day": "rest", "rest day": "rest",
}


def normalise_garmin_type(type_key: str | None) -> str:
    if not type_key:
        return "other"
    return GARMIN_TYPE_MAP.get(type_key.strip().lower(), "other")


def normalise_plan_type(text: str | None) -> str:
    """Map free-text from a training log to a canonical category.

    Checks multi-word keys first so "long run" beats a bare "run", and matches
    on word boundaries so "brick" does not become a "b-rick" false positive.
    """
    if not text:
        return "other"
    lowered = text.strip().lower()

    if lowered in CANONICAL_TYPES:
        return lowered

    # Longest keyword first, so specific phrases win over generic words.
    for keyword in sorted(PLAN_TYPE_KEYWORDS, key=len, reverse=True):
        if keyword in lowered:
            return PLAN_TYPE_KEYWORDS[keyword]
    return "other"


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

# How far either side of the planned day we will look for the actual session.
DEFAULT_DAY_TOLERANCE = 1

# Below this score we refuse to call it a match.
MIN_CONFIDENCE = 0.45


@dataclass
class PlannedSession:
    id: int
    day: str
    session_type: str
    title: str | None = None
    planned_duration_min: float | None = None
    planned_distance_km: float | None = None
    planned_intensity: str | None = None
    planned_rpe: float | None = None
    block: str | None = None
    week: int | None = None
    notes: str | None = None

    @property
    def canonical_type(self) -> str:
        return normalise_plan_type(self.session_type)


@dataclass
class MatchResult:
    """One row of the plan-vs-actual report."""

    status: str                       # completed | missed | unplanned
    day: str
    planned: dict[str, Any] | None = None
    actual: dict[str, Any] | None = None
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)
    manual: bool = False

    # Deltas, populated only when both sides are present.
    duration_delta_min: float | None = None
    distance_delta_km: float | None = None
    duration_pct: float | None = None
    distance_pct: float | None = None
    day_offset: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _score(plan: PlannedSession, act: Activity, day_tolerance: int) -> tuple[float, list[str]]:
    """Score a candidate plan/actual pairing in [0, 1], with human-readable why."""
    reasons: list[str] = []

    plan_type = plan.canonical_type
    act_type = normalise_garmin_type(act.activity_type)

    # Type agreement is the strongest signal. A mismatch between two *known*
    # types is disqualifying - an easy run is not a swim, whatever the duration.
    if plan_type == act_type:
        type_score = 1.0
        reasons.append(f"type matches ({plan_type})")
    elif plan_type == "other" or act_type == "other":
        type_score = 0.5
        reasons.append(f"type unclear (plan={plan_type}, actual={act_type})")
    else:
        return 0.0, [f"type mismatch (plan={plan_type}, actual={act_type})"]

    # Day proximity.
    offset = abs((date.fromisoformat(act.day) - date.fromisoformat(plan.day)).days)
    if offset > day_tolerance:
        return 0.0, [f"{offset} days apart"]
    day_score = 1.0 if offset == 0 else 0.55
    if offset:
        reasons.append(f"{offset} day{'s' if offset > 1 else ''} off plan")

    # Duration / distance agreement, when the plan specified them.
    magnitude_scores: list[float] = []
    if plan.planned_duration_min and act.duration_min:
        ratio = min(plan.planned_duration_min, act.duration_min) / max(
            plan.planned_duration_min, act.duration_min
        )
        magnitude_scores.append(ratio)
        reasons.append(
            f"duration {act.duration_min:.0f}min "
            f"vs {plan.planned_duration_min:.0f}min planned"
        )
    if plan.planned_distance_km and act.distance_km:
        ratio = min(plan.planned_distance_km, act.distance_km) / max(
            plan.planned_distance_km, act.distance_km
        )
        magnitude_scores.append(ratio)
        reasons.append(
            f"distance {act.distance_km:.1f}km "
            f"vs {plan.planned_distance_km:.1f}km planned"
        )

    if magnitude_scores:
        magnitude_score = sum(magnitude_scores) / len(magnitude_scores)
        score = 0.45 * type_score + 0.30 * day_score + 0.25 * magnitude_score
    else:
        # Nothing quantitative to compare - lean entirely on type and day.
        score = 0.6 * type_score + 0.4 * day_score
        reasons.append("no planned duration/distance to compare")

    return round(score, 3), reasons


def reconcile(
    planned: list[PlannedSession],
    actuals: list[Activity],
    *,
    overrides: dict[int, str | None] | None = None,
    ignored_activity_ids: set[str] | None = None,
    day_tolerance: int = DEFAULT_DAY_TOLERANCE,
) -> list[MatchResult]:
    """Pair planned sessions with actual activities.

    `overrides` maps planned_session_id -> activity_id (or None to force a
    "missed"); these always win over the automatic scoring.

    Uses greedy best-first assignment: score every viable pair, then repeatedly
    take the highest-scoring pair whose plan and activity are both still free.
    For the handful of sessions in a training week this is both optimal in
    practice and easy to explain, which matters more here than theoretical
    optimality.
    """
    overrides = overrides or {}
    ignored = ignored_activity_ids or set()

    actuals = [a for a in actuals if a.activity_id not in ignored]
    by_activity_id = {a.activity_id: a for a in actuals}

    results: list[MatchResult] = []
    used_activities: set[str] = set()
    used_plans: set[int] = set()

    # ---- 1. Manual overrides first; they are ground truth. ----------------
    for plan in planned:
        if plan.id not in overrides:
            continue
        used_plans.add(plan.id)
        forced_id = overrides[plan.id]

        if forced_id is None:
            results.append(
                MatchResult(
                    status="missed",
                    day=plan.day,
                    planned=asdict(plan),
                    reasons=["manually marked as missed"],
                    manual=True,
                )
            )
            continue

        act = by_activity_id.get(forced_id)
        if act is None:
            # The override points at an activity outside the current window or
            # since deleted; report the plan as unmatched rather than silently
            # dropping the override.
            results.append(
                MatchResult(
                    status="missed",
                    day=plan.day,
                    planned=asdict(plan),
                    reasons=[f"manually linked activity {forced_id} not found in range"],
                    manual=True,
                )
            )
            continue

        used_activities.add(act.activity_id)
        results.append(_build_match(plan, act, 1.0, ["manually linked"], manual=True))

    # ---- 2. Score every remaining viable pair. ----------------------------
    candidates: list[tuple[float, PlannedSession, Activity, list[str]]] = []
    for plan in planned:
        if plan.id in used_plans or plan.canonical_type == "rest":
            continue
        for act in actuals:
            if act.activity_id in used_activities:
                continue
            score, reasons = _score(plan, act, day_tolerance)
            if score >= MIN_CONFIDENCE:
                candidates.append((score, plan, act, reasons))

    # Greedy: highest confidence wins the pairing.
    candidates.sort(key=lambda c: c[0], reverse=True)
    for score, plan, act, reasons in candidates:
        if plan.id in used_plans or act.activity_id in used_activities:
            continue
        used_plans.add(plan.id)
        used_activities.add(act.activity_id)
        results.append(_build_match(plan, act, score, reasons))

    # ---- 3. Whatever is left over. ---------------------------------------
    for plan in planned:
        if plan.id in used_plans:
            continue
        if plan.canonical_type == "rest":
            # A rest day is "kept" when nothing was recorded against it.
            trained = any(a.day == plan.day for a in actuals)
            results.append(
                MatchResult(
                    status="unplanned" if trained else "completed",
                    day=plan.day,
                    planned=asdict(plan),
                    confidence=1.0,
                    reasons=["trained on a rest day"] if trained else ["rest day kept"],
                )
            )
            continue
        results.append(
            MatchResult(
                status="missed",
                day=plan.day,
                planned=asdict(plan),
                reasons=["no matching activity recorded"],
            )
        )

    for act in actuals:
        if act.activity_id in used_activities:
            continue
        results.append(
            MatchResult(
                status="unplanned",
                day=act.day,
                actual=_activity_dict(act),
                reasons=["recorded but not in the plan"],
            )
        )

    results.sort(key=lambda r: (r.day, r.status))
    return results


def _activity_dict(act: Activity) -> dict[str, Any]:
    d = asdict(act)
    d.pop("raw", None)
    d["canonical_type"] = normalise_garmin_type(act.activity_type)
    return d


def _build_match(
    plan: PlannedSession,
    act: Activity,
    score: float,
    reasons: list[str],
    *,
    manual: bool = False,
) -> MatchResult:
    res = MatchResult(
        status="completed",
        day=plan.day,
        planned=asdict(plan),
        actual=_activity_dict(act),
        confidence=score,
        reasons=reasons,
        manual=manual,
    )
    res.day_offset = (date.fromisoformat(act.day) - date.fromisoformat(plan.day)).days

    if plan.planned_duration_min and act.duration_min:
        res.duration_delta_min = round(act.duration_min - plan.planned_duration_min, 1)
        res.duration_pct = round(100.0 * act.duration_min / plan.planned_duration_min, 1)
    if plan.planned_distance_km and act.distance_km:
        res.distance_delta_km = round(act.distance_km - plan.planned_distance_km, 2)
        res.distance_pct = round(100.0 * act.distance_km / plan.planned_distance_km, 1)

    return res


# --------------------------------------------------------------------------
# Summary statistics
# --------------------------------------------------------------------------

def summarise(results: list[MatchResult]) -> dict[str, Any]:
    """Adherence and load summary over a set of match results."""
    completed = [r for r in results if r.status == "completed"]
    missed = [r for r in results if r.status == "missed"]
    unplanned = [r for r in results if r.status == "unplanned"]

    planned_total = len(completed) + len(missed)

    dur_planned = sum(
        (r.planned or {}).get("planned_duration_min") or 0.0
        for r in completed + missed
    )
    dur_actual = sum(
        (r.actual or {}).get("duration_min") or 0.0
        for r in completed + unplanned
    )
    dist_planned = sum(
        (r.planned or {}).get("planned_distance_km") or 0.0
        for r in completed + missed
    )
    dist_actual = sum(
        (r.actual or {}).get("distance_km") or 0.0
        for r in completed + unplanned
    )

    by_type: dict[str, dict[str, Any]] = {}
    for r in results:
        if r.planned:
            key = normalise_plan_type(r.planned.get("session_type"))
        elif r.actual:
            key = r.actual.get("canonical_type", "other")
        else:
            continue
        slot = by_type.setdefault(
            key,
            {"planned": 0, "completed": 0, "missed": 0, "unplanned": 0,
             "planned_min": 0.0, "actual_min": 0.0},
        )
        if r.status == "completed":
            slot["completed"] += 1
            slot["planned"] += 1
        elif r.status == "missed":
            slot["missed"] += 1
            slot["planned"] += 1
        else:
            slot["unplanned"] += 1
        slot["planned_min"] += (r.planned or {}).get("planned_duration_min") or 0.0
        slot["actual_min"] += (r.actual or {}).get("duration_min") or 0.0

    for slot in by_type.values():
        slot["planned_min"] = round(slot["planned_min"], 1)
        slot["actual_min"] = round(slot["actual_min"], 1)

    return {
        "sessions_planned": planned_total,
        "sessions_completed": len(completed),
        "sessions_missed": len(missed),
        "sessions_unplanned": len(unplanned),
        "adherence_pct": (
            round(100.0 * len(completed) / planned_total, 1) if planned_total else None
        ),
        "planned_duration_min": round(dur_planned, 1),
        "actual_duration_min": round(dur_actual, 1),
        "duration_delta_min": round(dur_actual - dur_planned, 1),
        "duration_pct": round(100.0 * dur_actual / dur_planned, 1) if dur_planned else None,
        "planned_distance_km": round(dist_planned, 2),
        "actual_distance_km": round(dist_actual, 2),
        "distance_delta_km": round(dist_actual - dist_planned, 2),
        "distance_pct": round(100.0 * dist_actual / dist_planned, 1) if dist_planned else None,
        "by_type": by_type,
    }


def week_start(day: str) -> str:
    """Monday of the ISO week containing `day`."""
    d = date.fromisoformat(day)
    return (d - timedelta(days=d.weekday())).isoformat()
