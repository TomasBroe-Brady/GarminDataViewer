"""Natural-language questions over the combined plan + wellness data.

Deliberately not LLM-backed: a fixed, deterministic set of question templates,
in keeping with matching.py's own philosophy that an honest "I didn't
understand that" beats a plausible-sounding wrong answer. Each recognised
question names a wellness metric (optionally scoped to weeks you missed a
session, or weeks you completed everything) or asks about missed/unplanned
counts and overall adherence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .matching import MatchResult, summarise, week_start

# Wellness metric vocabulary: phrase -> (daily_context key, human unit).
# Longest phrase wins so "resting heart rate" beats a bare "heart rate".
METRICS: dict[str, tuple[str, str]] = {
    "resting heart rate": ("resting_hr", "bpm"),
    "resting hr": ("resting_hr", "bpm"),
    "rhr": ("resting_hr", "bpm"),
    "overnight hrv": ("hrv_overnight", "ms"),
    "hrv": ("hrv_overnight", "ms"),
    "sleep score": ("sleep_score", ""),
    "sleep": ("sleep_hours", "h"),
    "body battery": ("body_battery_change", "pts"),
    "training readiness": ("training_readiness", ""),
    "readiness": ("training_readiness", ""),
    "recovery time": ("recovery_time_hours", "h"),
    "steps": ("steps", "steps"),
    "active calories": ("active_calories", "kcal"),
    "stress": ("sleep_stress", ""),
}

# Week-status vocabulary: phrase -> status filter ("missed" | "perfect" | "unplanned").
STATUSES: dict[str, str] = {
    "missed a session": "missed",
    "missed sessions": "missed",
    "missed something": "missed",
    "i missed": "missed",
    "completed everything": "perfect",
    "completed all": "perfect",
    "perfect weeks": "perfect",
    "hit every session": "perfect",
    "unplanned": "unplanned",
    "extra workouts": "unplanned",
}

SUGGESTIONS = [
    "How did my sleep look in weeks I missed a session?",
    "What was my resting heart rate in weeks I completed everything?",
    "How many sessions did I miss?",
    "What's my overnight HRV like overall?",
]

_COUNT_MISSED = re.compile(r"how many (sessions|workouts).*(miss|skip)", re.I)
_COUNT_UNPLANNED = re.compile(r"how many (unplanned|extra) (sessions|workouts)", re.I)
_ADHERENCE = re.compile(r"adherence|on track|how.*(am i doing|did i do)", re.I)


@dataclass
class Answer:
    question: str
    understood: bool
    text: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "understood": self.understood,
            "text": self.text,
            "data": self.data,
        }


def _find_metric(q: str) -> tuple[str, str, str] | None:
    lowered = q.lower()
    for phrase in sorted(METRICS, key=len, reverse=True):
        if phrase in lowered:
            key, unit = METRICS[phrase]
            return phrase, key, unit
    return None


def _find_status(q: str) -> str | None:
    lowered = q.lower()
    for phrase in sorted(STATUSES, key=len, reverse=True):
        if phrase in lowered:
            return STATUSES[phrase]
    return None


def _weeks_by_status(results: list[MatchResult]) -> dict[str, set[str]]:
    """Map each ISO week-start to the set of result statuses present that week."""
    weeks: dict[str, set[str]] = {}
    for r in results:
        if not r.day:
            continue
        weeks.setdefault(week_start(r.day), set()).add(r.status)
    return weeks


def _days_for_status(results: list[MatchResult], status_filter: str | None) -> set[str]:
    """Days falling in a week matching `status_filter` (None = every week)."""
    weeks = _weeks_by_status(results)
    if status_filter is None:
        wanted = set(weeks)
    elif status_filter == "perfect":
        wanted = {
            wk for wk, statuses in weeks.items()
            if "missed" not in statuses and "unplanned" not in statuses
        }
    else:
        wanted = {wk for wk, statuses in weeks.items() if status_filter in statuses}

    return {r.day for r in results if r.day and week_start(r.day) in wanted}


def _average(
    days: set[str], daily_context: dict[str, dict[str, Any]], key: str
) -> tuple[float | None, int]:
    vals = [daily_context[d][key] for d in days if d in daily_context and key in daily_context[d]]
    if not vals:
        return None, 0
    return round(sum(vals) / len(vals), 2), len(vals)


def answer_question(
    question: str,
    results: list[MatchResult],
    daily_context: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    q = question.strip()
    if not q:
        return Answer(
            q, False, f"Ask a question, e.g. {SUGGESTIONS[0]!r}", {"suggestions": SUGGESTIONS}
        ).to_dict()

    metric = _find_metric(q)
    if metric:
        phrase, key, unit = metric
        status = _find_status(q)
        days = _days_for_status(results, status)
        avg, n = _average(days, daily_context, key)
        scope = {
            "missed": "in weeks with a missed session",
            "perfect": "in weeks you completed everything",
            "unplanned": "in weeks with an unplanned session",
            None: "overall",
        }[status]
        if avg is None:
            text = f"No {phrase} data recorded {scope} in this range."
        else:
            unit_suffix = f" {unit}" if unit else ""
            text = (
                f"Average {phrase} {scope}: {avg}{unit_suffix}, "
                f"across {n} day{'s' if n != 1 else ''}."
            )
        return Answer(
            q, True, text,
            {"metric": key, "status_filter": status, "value": avg, "unit": unit, "n_days": n},
        ).to_dict()

    if _COUNT_MISSED.search(q):
        n = sum(1 for r in results if r.status == "missed")
        return Answer(
            q, True, f"You missed {n} planned session{'s' if n != 1 else ''} in this range.",
            {"count": n, "status": "missed"},
        ).to_dict()

    if _COUNT_UNPLANNED.search(q):
        n = sum(1 for r in results if r.status == "unplanned")
        return Answer(
            q, True, f"{n} unplanned session{'s' if n != 1 else ''} recorded in this range.",
            {"count": n, "status": "unplanned"},
        ).to_dict()

    if _ADHERENCE.search(q):
        s = summarise(results)
        pct = s["adherence_pct"]
        if pct is None:
            text = "No planned sessions in this range to measure adherence against."
        else:
            text = (
                f"Adherence is {pct}% "
                f"({s['sessions_completed']} of {s['sessions_planned']} planned sessions)."
            )
        return Answer(q, True, text, {"summary": s}).to_dict()

    return Answer(
        q, False,
        "I didn't recognise that question. Try asking about a wellness metric "
        "(sleep, HRV, resting heart rate, readiness, steps) - optionally scoped to "
        "weeks you missed a session or completed everything - or ask about "
        "missed/unplanned counts or adherence.",
        {"suggestions": SUGGESTIONS},
    ).to_dict()
