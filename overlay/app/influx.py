"""Read-only client for the InfluxDB 1.x database that garmin-fetch-data fills.

We never write to InfluxDB - garmin-fetch-data owns that database. This module
only reads the measurements needed to reconcile actuals against a training plan.

Measurement and field names mirror garmin-grafana's writer
(https://github.com/arpanghosh8453/garmin-grafana, BSD-3-Clause). They were
read off that project's `garmin_fetch.py` rather than guessed; if it renames a
field, update the constants below.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .config import get_settings

log = logging.getLogger(__name__)

ACTIVITY_FIELDS = (
    "Activity_ID",
    "activityName",
    "activityType",
    "distance",
    "elapsedDuration",
    "movingDuration",
    "averageSpeed",
    "averageHR",
    "maxHR",
    "calories",
    "elevationGain",
    "aerobicTrainingEffect",
    "anaerobicTrainingEffect",
    "activityTrainingLoad",
)


@dataclass
class Activity:
    """One completed activity as recorded by the watch."""

    activity_id: str
    day: str                      # local calendar date, YYYY-MM-DD
    start_utc: str
    name: str | None
    activity_type: str | None
    distance_km: float | None
    duration_min: float | None
    moving_duration_min: float | None
    avg_hr: float | None
    max_hr: float | None
    calories: float | None
    elevation_gain_m: float | None
    aerobic_te: float | None
    anaerobic_te: float | None
    training_load: float | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


class InfluxUnavailable(RuntimeError):
    """Raised when InfluxDB cannot be reached or rejects a query."""


class InfluxReader:
    def __init__(self, timeout: float = 15.0) -> None:
        s = get_settings()
        self._url = f"{s.influx_url}/query"
        self._db = s.influx_db
        self._auth = (s.influx_user, s.influx_password)
        self._timeout = timeout
        self.tz = _resolve_tz()

    # ------------------------------------------------------------------ core
    def _query(self, q: str) -> list[dict[str, Any]]:
        """Run an InfluxQL query and flatten the returned series into dicts."""
        try:
            resp = httpx.get(
                self._url,
                params={"db": self._db, "q": q, "epoch": "ms"},
                auth=self._auth,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise InfluxUnavailable(f"Could not reach InfluxDB at {self._url}: {exc}") from exc

        if resp.status_code != 200:
            raise InfluxUnavailable(f"InfluxDB returned {resp.status_code}: {resp.text[:300]}")

        results = (resp.json().get("results") or [{}])[0]
        if "error" in results:
            raise InfluxUnavailable(f"InfluxDB query error: {results['error']}")

        rows: list[dict[str, Any]] = []
        for series in results.get("series") or []:
            cols = series.get("columns", [])
            for values in series.get("values", []):
                rows.append(dict(zip(cols, values, strict=False)))
        return rows

    def ping(self) -> bool:
        try:
            self._query("SHOW MEASUREMENTS LIMIT 1")
            return True
        except InfluxUnavailable:
            return False

    def measurements(self) -> list[str]:
        return [r.get("name", "") for r in self._query("SHOW MEASUREMENTS")]

    # ------------------------------------------------------------ activities
    def activities(self, start: str, end: str) -> list[Activity]:
        """Completed activities whose *local* date falls in [start, end].

        InfluxDB stores UTC, so we query a window widened by one day on each
        side and then filter on the local date.
        """
        lo, hi = _widen(start, end)
        fields = ", ".join(f'"{f}"' for f in ACTIVITY_FIELDS)
        q = (
            f'SELECT {fields} FROM "ActivitySummary" '
            f"WHERE time >= '{lo}T00:00:00Z' AND time <= '{hi}T23:59:59Z'"
        )

        out: list[Activity] = []
        for r in self._query(q):
            # garmin-fetch-data writes a second "END" marker point per activity
            # so Grafana can close the series; it carries no measurements.
            if r.get("activityName") == "END" or r.get("activityType") == "No Activity":
                continue

            raw_id = r.get("Activity_ID")
            if raw_id is None:
                continue

            local_day = self._to_local_day(r.get("time"))
            if not (start <= local_day <= end):
                continue

            out.append(
                Activity(
                    activity_id=_clean_id(raw_id),
                    day=local_day,
                    start_utc=_ms_to_iso(r.get("time")),
                    name=r.get("activityName"),
                    activity_type=r.get("activityType"),
                    distance_km=_div(r.get("distance"), 1000.0),
                    duration_min=_div(r.get("elapsedDuration"), 60.0),
                    moving_duration_min=_div(r.get("movingDuration"), 60.0),
                    avg_hr=_num(r.get("averageHR")),
                    max_hr=_num(r.get("maxHR")),
                    calories=_num(r.get("calories")),
                    elevation_gain_m=_num(r.get("elevationGain")),
                    aerobic_te=_num(r.get("aerobicTrainingEffect")),
                    anaerobic_te=_num(r.get("anaerobicTrainingEffect")),
                    training_load=_num(r.get("activityTrainingLoad")),
                    raw=r,
                )
            )

        out.sort(key=lambda a: a.start_utc)
        return out

    # --------------------------------------------------------- daily context
    def daily_context(self, start: str, end: str) -> dict[str, dict[str, Any]]:
        """Per-day recovery context keyed by local date.

        This is what lifts the overlay above a tick-box adherence report: it
        lets you ask whether a session was missed on a day you were already
        flat, or nailed on a day you were well recovered.
        """
        lo, hi = _widen(start, end)
        window = f"WHERE time >= '{lo}T00:00:00Z' AND time <= '{hi}T23:59:59Z'"
        context: dict[str, dict[str, Any]] = {}

        # (query, {influx column -> our key}). Each runs independently so a
        # measurement that was never fetched simply contributes nothing.
        probes: list[tuple[str, dict[str, str]]] = [
            (
                (
                    'SELECT mean("restingHeartRate") AS rhr, mean("totalSteps") AS steps, '
                    'mean("activeKilocalories") AS active_kcal, '
                    'mean("vigorousIntensityMinutes") AS vig_min, '
                    'mean("moderateIntensityMinutes") AS mod_min '
                    f'FROM "DailyStats" {window} GROUP BY time(1d) fill(none)'
                ),
                {
                    "rhr": "resting_hr",
                    "steps": "steps",
                    "active_kcal": "active_calories",
                    "vig_min": "vigorous_minutes",
                    "mod_min": "moderate_minutes",
                },
            ),
            (
                (
                    'SELECT mean("sleepScore") AS score, mean("sleepTimeSeconds") AS secs, '
                    'mean("avgOvernightHrv") AS hrv, mean("bodyBatteryChange") AS bb_change, '
                    'mean("avgSleepStress") AS sleep_stress '
                    f'FROM "SleepSummary" {window} GROUP BY time(1d) fill(none)'
                ),
                {
                    "score": "sleep_score",
                    "secs": "_sleep_seconds",
                    "hrv": "hrv_overnight",
                    "bb_change": "body_battery_change",
                    "sleep_stress": "sleep_stress",
                },
            ),
            (
                (
                    'SELECT mean("score") AS readiness, mean("acuteLoad") AS acute_load, '
                    'mean("recoveryTime") AS recovery_time '
                    f'FROM "TrainingReadiness" {window} GROUP BY time(1d) fill(none)'
                ),
                {
                    "readiness": "training_readiness",
                    "acute_load": "acute_load",
                    "recovery_time": "recovery_time_hours",
                },
            ),
        ]

        for q, mapping in probes:
            try:
                rows = self._query(q)
            except InfluxUnavailable as exc:
                log.debug("daily_context probe skipped: %s", exc)
                continue
            for r in rows:
                day = self._to_local_day(r.get("time"))
                if not (start <= day <= end):
                    continue
                slot = context.setdefault(day, {})
                for col, key in mapping.items():
                    val = _num(r.get(col))
                    if val is not None:
                        slot[key] = val

        # Convert the raw sleep seconds into something readable.
        for slot in context.values():
            secs = slot.pop("_sleep_seconds", None)
            if secs is not None:
                slot["sleep_hours"] = round(secs / 3600.0, 2)

        return context

    # ---------------------------------------------------------------- helper
    def _to_local_day(self, epoch_ms: Any) -> str:
        if epoch_ms is None:
            return ""
        dt = datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc)
        return dt.astimezone(self.tz).date().isoformat()


def _resolve_tz() -> ZoneInfo:
    name = (os.getenv("USER_TIMEZONE") or "").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("Unknown USER_TIMEZONE %r, falling back to UTC", name)
        return ZoneInfo("UTC")


def _widen(start: str, end: str, days: int = 1) -> tuple[str, str]:
    """Widen an inclusive ISO date range by `days` on each side."""
    lo = date.fromisoformat(start) - timedelta(days=days)
    hi = date.fromisoformat(end) + timedelta(days=days)
    return lo.isoformat(), hi.isoformat()


def _ms_to_iso(epoch_ms: Any) -> str:
    if epoch_ms is None:
        return ""
    return datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc).isoformat()


def _clean_id(raw: Any) -> str:
    """Activity ids arrive as floats from InfluxDB; render them without '.0'."""
    if isinstance(raw, float) and raw.is_integer():
        return str(int(raw))
    return str(raw)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, str):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _div(value: Any, divisor: float) -> float | None:
    n = _num(value)
    return None if n is None else round(n / divisor, 3)
