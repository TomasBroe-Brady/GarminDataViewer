"""Tests for the training-log importer - the messy-input parsing matters most."""

from __future__ import annotations

import io

import pandas as pd
import pytest
from app.importer import (
    build_rows,
    detect_columns,
    parse_day,
    parse_distance_km,
    parse_duration_minutes,
    read_table,
)


class TestParseDuration:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (45, 45.0),
            ("45", 45.0),
            ("45 min", 45.0),
            ("45mins", 45.0),
            ("90 minutes", 90.0),
            ("1:30", 90.0),
            ("1:30:00", 90.0),
            ("0:45", 45.0),
            ("1h30", 90.0),
            ("1h 30m", 90.0),
            ("2h", 120.0),
            ("2 hours", 120.0),
        ],
    )
    def test_accepts_common_spellings(self, value, expected):
        assert parse_duration_minutes(value) == expected

    @pytest.mark.parametrize("value", [None, "", "  ", "rest", float("nan")])
    def test_rejects_non_durations(self, value):
        assert parse_duration_minutes(value) is None


class TestParseDistance:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (8, 8.0),
            ("8", 8.0),
            ("8km", 8.0),
            ("8 km", 8.0),
            ("10,5", 10.5),
        ],
    )
    def test_kilometres(self, value, expected):
        assert parse_distance_km(value) == expected

    def test_miles_are_converted(self):
        assert parse_distance_km("5 miles") == pytest.approx(8.047, abs=0.01)
        assert parse_distance_km("5mi") == pytest.approx(8.047, abs=0.01)

    def test_assume_miles_flag(self):
        assert parse_distance_km(5, assume_miles=True) == pytest.approx(8.047, abs=0.01)

    def test_metres_are_converted(self):
        assert parse_distance_km("400 m") == 0.4


class TestParseDay:
    def test_iso(self):
        assert parse_day("2026-03-04") == "2026-03-04"

    def test_day_first_is_assumed(self):
        # 04/03/2026 should read as 4 March, not 3 April.
        assert parse_day("04/03/2026") == "2026-03-04"

    def test_written_dates(self):
        assert parse_day("4 March 2026") == "2026-03-04"

    def test_rejects_rubbish(self):
        assert parse_day("not a date") is None
        assert parse_day("") is None
        assert parse_day(None) is None


class TestDetectColumns:
    def test_obvious_headers(self):
        mapping, unmapped = detect_columns(
            ["Date", "Session", "Duration", "Distance", "Notes"]
        )
        assert mapping["day"] == "Date"
        assert mapping["session_type"] == "Session"
        assert mapping["planned_duration_min"] == "Duration"
        assert mapping["planned_distance_km"] == "Distance"
        assert mapping["notes"] == "Notes"
        assert unmapped == []

    def test_alternative_spellings(self):
        mapping, _ = detect_columns(["When", "Workout Type", "Mins", "KM", "Phase"])
        assert mapping["day"] == "When"
        assert mapping["session_type"] == "Workout Type"
        assert mapping["planned_duration_min"] == "Mins"
        assert mapping["planned_distance_km"] == "KM"
        assert mapping["block"] == "Phase"

    def test_unknown_columns_are_reported_not_guessed(self):
        _, unmapped = detect_columns(["Date", "Session", "Shoe", "Weather"])
        assert "Shoe" in unmapped
        assert "Weather" in unmapped


class TestBuildRows:
    def test_happy_path(self):
        df = pd.DataFrame(
            {
                "Date": ["2026-03-02", "2026-03-04"],
                "Session": ["Easy run", "Intervals"],
                "Duration": ["45 min", "1:00"],
                "Distance": ["8km", "10km"],
            }
        )
        result = build_rows(df, filename="plan.csv")
        assert result.imported == 2
        assert result.skipped == 0
        assert result.rows[0]["day"] == "2026-03-02"
        assert result.rows[0]["planned_duration_min"] == 45.0
        assert result.rows[1]["planned_distance_km"] == 10.0

    def test_missing_date_column_is_reported(self):
        df = pd.DataFrame({"Session": ["Run"], "Duration": [45]})
        result = build_rows(df)
        assert result.imported == 0
        assert any("date column" in w.lower() for w in result.warnings)

    def test_bad_rows_are_skipped_not_guessed(self):
        df = pd.DataFrame(
            {
                "Date": ["2026-03-02", "banana", "2026-03-06"],
                "Session": ["Run", "Run", "Swim"],
            }
        )
        result = build_rows(df)
        assert result.imported == 2
        assert result.skipped == 1
        assert any("Row 3" in w for w in result.warnings)

    def test_title_used_when_type_missing(self):
        df = pd.DataFrame({"Date": ["2026-03-02"], "Name": ["Long run"]})
        result = build_rows(df)
        assert result.imported == 1
        assert result.rows[0]["session_type"] == "Long run"

    def test_row_with_no_session_at_all_is_skipped(self):
        df = pd.DataFrame({"Date": ["2026-03-02"], "Session": [None]})
        result = build_rows(df)
        assert result.imported == 0
        assert result.skipped == 1

    def test_miles_column_detected(self):
        df = pd.DataFrame(
            {"Date": ["2026-03-02"], "Session": ["Run"], "Miles": [5]}
        )
        result = build_rows(df)
        assert result.rows[0]["planned_distance_km"] == pytest.approx(8.047, abs=0.01)


class TestReadTable:
    def test_reads_csv_with_bom(self):
        raw = "﻿Date,Session\n2026-03-02,Run\n".encode()
        df = read_table(raw, "plan.csv")
        assert list(df.columns) == ["Date", "Session"]
        assert len(df) == 1

    def test_reads_excel(self):
        buf = io.BytesIO()
        pd.DataFrame({"Date": ["2026-03-02"], "Session": ["Run"]}).to_excel(buf, index=False)
        df = read_table(buf.getvalue(), "plan.xlsx")
        assert len(df) == 1
