"""Tests for plan-vs-actual reconciliation."""

from __future__ import annotations

import pytest
from app.influx import Activity
from app.matching import (
    PlannedSession,
    normalise_garmin_type,
    normalise_plan_type,
    reconcile,
    summarise,
    week_start,
)


def act(
    activity_id="1",
    day="2026-03-02",
    activity_type="running",
    duration_min=45.0,
    distance_km=8.0,
    start_utc=None,
) -> Activity:
    return Activity(
        activity_id=activity_id,
        day=day,
        start_utc=start_utc or f"{day}T18:00:00+00:00",
        name="Morning Run",
        activity_type=activity_type,
        distance_km=distance_km,
        duration_min=duration_min,
        moving_duration_min=duration_min,
        avg_hr=145.0,
        max_hr=170.0,
        calories=500.0,
        elevation_gain_m=50.0,
        aerobic_te=3.0,
        anaerobic_te=0.5,
        training_load=80.0,
    )


def plan(
    id=1,
    day="2026-03-02",
    session_type="Easy run",
    duration=45.0,
    distance=8.0,
) -> PlannedSession:
    return PlannedSession(
        id=id,
        day=day,
        session_type=session_type,
        title=session_type,
        planned_duration_min=duration,
        planned_distance_km=distance,
    )


class TestTypeNormalisation:
    @pytest.mark.parametrize(
        ("garmin", "expected"),
        [
            ("running", "run"),
            ("treadmill_running", "run"),
            ("trail_running", "run"),
            ("indoor_cycling", "bike"),
            ("lap_swimming", "swim"),
            ("strength_training", "strength"),
            ("walking", "walk"),
            ("indoor_rowing", "row"),
            ("something_new", "other"),
            (None, "other"),
        ],
    )
    def test_garmin_types(self, garmin, expected):
        assert normalise_garmin_type(garmin) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Easy run", "run"),
            ("Long run", "run"),
            ("Intervals", "run"),
            ("Tempo", "run"),
            ("Turbo session", "bike"),
            ("Pool swim", "swim"),
            ("Gym - upper body", "strength"),
            ("Rest day", "rest"),
            ("Yoga", "yoga"),
            ("Unclassifiable nonsense", "other"),
        ],
    )
    def test_plan_text(self, text, expected):
        assert normalise_plan_type(text) == expected


class TestReconcile:
    def test_obvious_match(self):
        results = reconcile([plan()], [act()])
        assert len(results) == 1
        assert results[0].status == "completed"
        assert results[0].confidence > 0.9

    def test_type_mismatch_is_never_matched(self):
        # A planned swim must not absorb a recorded run, however close in time.
        results = reconcile(
            [plan(session_type="Pool swim")], [act(activity_type="running")]
        )
        statuses = sorted(r.status for r in results)
        assert statuses == ["missed", "unplanned"]

    def test_missed_session(self):
        results = reconcile([plan()], [])
        assert results[0].status == "missed"

    def test_unplanned_session(self):
        results = reconcile([], [act()])
        assert results[0].status == "unplanned"

    def test_matches_one_day_late(self):
        results = reconcile([plan(day="2026-03-02")], [act(day="2026-03-03")])
        assert results[0].status == "completed"
        assert results[0].day_offset == 1

    def test_does_not_match_outside_tolerance(self):
        results = reconcile(
            [plan(day="2026-03-02")], [act(day="2026-03-06")], day_tolerance=1
        )
        assert sorted(r.status for r in results) == ["missed", "unplanned"]

    def test_deltas_are_computed(self):
        results = reconcile(
            [plan(duration=45.0, distance=8.0)],
            [act(duration_min=50.0, distance_km=8.5)],
        )
        r = results[0]
        assert r.duration_delta_min == pytest.approx(5.0)
        assert r.distance_delta_km == pytest.approx(0.5)
        assert r.duration_pct == pytest.approx(111.1, abs=0.2)

    def test_greedy_prefers_the_better_pairing(self):
        # Two runs planned, two recorded. The closer duration pairing should win
        # rather than a naive first-come assignment.
        planned = [
            plan(id=1, day="2026-03-02", duration=30.0, distance=5.0),
            plan(id=2, day="2026-03-02", duration=90.0, distance=18.0),
        ]
        actuals = [
            act(activity_id="A", day="2026-03-02", duration_min=92.0, distance_km=18.2),
            act(activity_id="B", day="2026-03-02", duration_min=31.0, distance_km=5.1),
        ]
        results = reconcile(planned, actuals)
        assert all(r.status == "completed" for r in results)
        pairing = {r.planned["id"]: r.actual["activity_id"] for r in results}
        assert pairing == {1: "B", 2: "A"}

    def test_rest_day_kept(self):
        results = reconcile([plan(session_type="Rest day", duration=None, distance=None)], [])
        assert results[0].status == "completed"
        assert "rest day kept" in results[0].reasons

    def test_training_on_a_rest_day_is_flagged(self):
        results = reconcile(
            [plan(session_type="Rest day", duration=None, distance=None)],
            [act()],
        )
        statuses = {r.status for r in results}
        assert "unplanned" in statuses

    def test_ignored_activity_is_excluded(self):
        results = reconcile([], [act(activity_id="X")], ignored_activity_ids={"X"})
        assert results == []

    def test_manual_override_wins(self):
        # The matcher would normally reject this pairing on type, but an
        # explicit manual link must be honoured.
        planned = [plan(id=7, session_type="Pool swim")]
        actuals = [act(activity_id="Z", activity_type="running")]
        results = reconcile(planned, actuals, overrides={7: "Z"})
        completed = [r for r in results if r.status == "completed"]
        assert len(completed) == 1
        assert completed[0].manual is True
        assert completed[0].actual["activity_id"] == "Z"

    def test_manual_missed_override(self):
        results = reconcile([plan(id=7)], [act(activity_id="Z")], overrides={7: None})
        by_status = {r.status for r in results}
        assert "missed" in by_status
        assert "unplanned" in by_status  # the activity is now orphaned

    def test_override_to_missing_activity_is_reported(self):
        results = reconcile([plan(id=7)], [], overrides={7: "nonexistent"})
        assert results[0].status == "missed"
        assert any("not found" in r for r in results[0].reasons)

    def test_no_duration_still_matches_on_type_and_day(self):
        results = reconcile(
            [plan(duration=None, distance=None)], [act()]
        )
        assert results[0].status == "completed"


class TestSummarise:
    def test_adherence(self):
        planned = [
            plan(id=1, day="2026-03-02"),
            plan(id=2, day="2026-03-03"),
            plan(id=3, day="2026-03-04"),
        ]
        actuals = [act(activity_id="A", day="2026-03-02")]
        s = summarise(reconcile(planned, actuals, day_tolerance=0))
        assert s["sessions_planned"] == 3
        assert s["sessions_completed"] == 1
        assert s["sessions_missed"] == 2
        assert s["adherence_pct"] == pytest.approx(33.3, abs=0.1)

    def test_volume_totals(self):
        s = summarise(
            reconcile(
                [plan(duration=45.0, distance=8.0)],
                [act(duration_min=50.0, distance_km=9.0)],
            )
        )
        assert s["planned_duration_min"] == 45.0
        assert s["actual_duration_min"] == 50.0
        assert s["duration_delta_min"] == 5.0
        assert s["distance_delta_km"] == 1.0

    def test_unplanned_volume_counts_towards_actual(self):
        s = summarise(reconcile([], [act(duration_min=60.0, distance_km=10.0)]))
        assert s["actual_duration_min"] == 60.0
        assert s["sessions_planned"] == 0
        assert s["adherence_pct"] is None

    def test_by_type_breakdown(self):
        results = reconcile(
            [plan(id=1, session_type="Easy run"), plan(id=2, session_type="Gym")],
            [act(activity_id="A", activity_type="running")],
        )
        s = summarise(results)
        assert s["by_type"]["run"]["completed"] == 1
        assert s["by_type"]["strength"]["missed"] == 1

    def test_empty(self):
        s = summarise([])
        assert s["sessions_planned"] == 0
        assert s["adherence_pct"] is None


def test_week_start():
    assert week_start("2026-03-04") == "2026-03-02"   # Wed -> Mon
    assert week_start("2026-03-02") == "2026-03-02"   # Mon -> itself
    assert week_start("2026-03-08") == "2026-03-02"   # Sun -> Mon
