"""End-to-end API tests against a stub InfluxDB.

No Docker here, so we stand up a small HTTP server that speaks the same
InfluxDB 1.x /query JSON as the real thing. That keeps the real httpx request
path, InfluxQL string building and response parsing under test - only the
database engine itself is substituted.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

# --------------------------------------------------------------------------
# Stub InfluxDB
# --------------------------------------------------------------------------

BASE_DAY = "2026-03-02"          # a Monday


def _ms(day: str, hour: int = 18) -> int:
    dt = datetime.fromisoformat(f"{day}T{hour:02d}:00:00").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _activity_rows() -> list[list]:
    """Two runs and a gym session, plus the END markers the real writer emits."""
    return [
        # time, Activity_ID, activityName, activityType, distance,
        # elapsedDuration, movingDuration, averageSpeed, averageHR, maxHR,
        # calories, elevationGain, aerobicTE, anaerobicTE, trainingLoad
        [_ms("2026-03-02"), 1001.0, "Morning Run", "running", 8100.0,
         2760.0, 2700.0, 2.9, 148.0, 172.0, 520.0, 45.0, 3.1, 0.4, 82.0],
        # The END marker for the same activity - must be filtered out.
        [_ms("2026-03-02", 19), 1001.0, "END", "No Activity", None,
         None, None, None, None, None, None, None, None, None, None],
        [_ms("2026-03-04"), 1002.0, "Evening Gym", "strength_training", None,
         3300.0, 3300.0, None, 118.0, 150.0, 300.0, None, 1.8, 1.2, 40.0],
        [_ms("2026-03-07"), 1003.0, "Long Run", "running", 18200.0,
         6300.0, 6200.0, 2.9, 152.0, 178.0, 1200.0, 180.0, 4.2, 0.3, 190.0],
        # An unplanned walk.
        [_ms("2026-03-05"), 1004.0, "Lunch Walk", "walking", 2500.0,
         1800.0, 1750.0, 1.4, 95.0, 110.0, 120.0, 10.0, 0.5, 0.0, 5.0],
    ]


ACTIVITY_COLUMNS = [
    "time", "Activity_ID", "activityName", "activityType", "distance",
    "elapsedDuration", "movingDuration", "averageSpeed", "averageHR", "maxHR",
    "calories", "elevationGain", "aerobicTrainingEffect",
    "anaerobicTrainingEffect", "activityTrainingLoad",
]


class StubInflux(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence the test output
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        q = (params.get("q") or [""])[0]

        if "SHOW MEASUREMENTS" in q:
            body = _result("measurements", ["name"],
                           [["ActivitySummary"], ["DailyStats"], ["SleepSummary"]])
        elif "ActivitySummary" in q:
            body = _result("ActivitySummary", ACTIVITY_COLUMNS, _activity_rows())
        elif "DailyStats" in q:
            body = _result(
                "DailyStats",
                ["time", "rhr", "steps", "active_kcal", "vig_min", "mod_min"],
                [[_ms(BASE_DAY, 0), 48.0, 11200.0, 620.0, 30.0, 20.0]],
            )
        elif "SleepSummary" in q:
            body = _result(
                "SleepSummary",
                ["time", "score", "secs", "hrv", "bb_change", "sleep_stress"],
                [[_ms(BASE_DAY, 7), 82.0, 27000.0, 61.0, 45.0, 18.0]],
            )
        elif "TrainingReadiness" in q:
            body = _result(
                "TrainingReadiness",
                ["time", "readiness", "acute_load", "recovery_time"],
                [[_ms(BASE_DAY, 7), 74.0, 320.0, 12.0]],
            )
        else:
            body = {"results": [{"statement_id": 0}]}

        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _result(name: str, columns: list[str], values: list[list]) -> dict:
    return {
        "results": [
            {"statement_id": 0,
             "series": [{"name": name, "columns": columns, "values": values}]}
        ]
    }


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def influx_server():
    server = HTTPServer(("127.0.0.1", 0), StubInflux)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address
    server.shutdown()


@pytest.fixture()
def client(influx_server, tmp_path, monkeypatch):
    host, port = influx_server
    monkeypatch.setenv("INFLUXDB_HOST", host)
    monkeypatch.setenv("INFLUXDB_PORT", str(port))
    monkeypatch.setenv("INFLUXDB_USER", "u")
    monkeypatch.setenv("INFLUXDB_PASSWORD", "p")
    monkeypatch.setenv("INFLUXDB_DB", "GarminStats")
    monkeypatch.setenv("OVERLAY_DB", str(tmp_path / "overlay.db"))
    monkeypatch.setenv("USER_TIMEZONE", "UTC")

    from app import config, db

    config.get_settings.cache_clear()
    db.init_db()

    from app.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c

    config.get_settings.cache_clear()


PLAN_CSV = """Date,Session,Duration,Distance,Intensity,Block,Week,Notes
2026-03-02,Easy run,45 min,8km,easy,Base 1,1,shakeout
2026-03-04,Gym,55 min,,moderate,Base 1,1,upper body
2026-03-06,Intervals,40 min,7km,hard,Base 1,1,6x800
2026-03-07,Long run,1:45,18km,easy,Base 1,1,
2026-03-08,Rest day,,,,Base 1,1,
"""


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

class TestHealth:
    def test_health_reports_influx_reachable(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["influxdb_reachable"] is True
        assert body["status"] == "ok"
        assert "ActivitySummary" in body["measurements"]


class TestImport:
    def test_dry_run_does_not_save(self, client):
        r = client.post(
            "/api/import?dry_run=true",
            files={"file": ("plan.csv", PLAN_CSV, "text/csv")},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["dry_run"] is True
        assert body["would_import"] == 5
        assert body["column_mapping"]["day"] == "Date"
        assert body["column_mapping"]["block"] == "Block"

        assert client.get("/api/plan").json()["planned"] == []

    def test_import_saves_sessions(self, client):
        r = client.post("/api/import", files={"file": ("plan.csv", PLAN_CSV, "text/csv")})
        assert r.status_code == 200
        assert r.json()["imported"] == 5

        planned = client.get("/api/plan?start=2026-03-01&end=2026-03-10").json()["planned"]
        assert len(planned) == 5
        easy = next(p for p in planned if p["day"] == "2026-03-02")
        assert easy["planned_duration_min"] == 45.0
        assert easy["planned_distance_km"] == 8.0
        assert easy["block"] == "Base 1"

    def test_reimport_does_not_duplicate(self, client):
        client.post("/api/import", files={"file": ("plan.csv", PLAN_CSV, "text/csv")})
        second = client.post("/api/import", files={"file": ("plan.csv", PLAN_CSV, "text/csv")})
        assert second.json()["imported"] == 0
        assert second.json()["duplicates_skipped"] == 5

    def test_empty_upload_rejected(self, client):
        r = client.post("/api/import", files={"file": ("empty.csv", "", "text/csv")})
        assert r.status_code == 400


class TestOverview:
    @pytest.fixture()
    def loaded(self, client):
        client.post("/api/import", files={"file": ("plan.csv", PLAN_CSV, "text/csv")})
        return client

    def test_reconciles_plan_against_actuals(self, loaded):
        r = loaded.get("/api/overview?start=2026-03-01&end=2026-03-10")
        assert r.status_code == 200
        body = r.json()
        assert body["influx_error"] is None

        by_day = {}
        for res in body["results"]:
            by_day.setdefault(res["day"], []).append(res)

        # Planned easy run on the 2nd, ran 8.1km in 46min -> completed.
        easy = by_day["2026-03-02"][0]
        assert easy["status"] == "completed"
        assert easy["actual"]["activity_id"] == "1001"
        assert easy["duration_delta_min"] == pytest.approx(1.0, abs=0.1)

        # Gym on the 4th -> completed against strength_training.
        gym = next(r for r in by_day["2026-03-04"] if r["status"] == "completed")
        assert gym["actual"]["activity_type"] == "strength_training"

        # Intervals on the 6th were never recorded -> missed.
        assert by_day["2026-03-06"][0]["status"] == "missed"

        # The walk on the 5th was never planned -> unplanned.
        walk = next(r for r in by_day["2026-03-05"] if r["status"] == "unplanned")
        assert walk["actual"]["activity_type"] == "walking"

    def test_end_markers_are_filtered_out(self, loaded):
        body = loaded.get("/api/overview?start=2026-03-01&end=2026-03-10").json()
        names = [
            (r.get("actual") or {}).get("name")
            for r in body["results"]
            if r.get("actual")
        ]
        assert "END" not in names

    def test_summary_numbers(self, loaded):
        s = loaded.get("/api/overview?start=2026-03-01&end=2026-03-10").json()["summary"]
        # 5 planned (4 sessions + 1 rest day), 3 of them completed.
        assert s["sessions_planned"] == 5
        assert s["sessions_completed"] == 4      # 3 sessions + rest day kept
        assert s["sessions_missed"] == 1         # the intervals
        assert s["sessions_unplanned"] == 1      # the walk
        assert s["adherence_pct"] == pytest.approx(80.0, abs=0.1)

    def test_weeks_are_grouped_from_monday(self, loaded):
        body = loaded.get("/api/overview?start=2026-03-01&end=2026-03-10").json()
        starts = [w["week_start"] for w in body["weeks"]]
        assert "2026-03-02" in starts
        assert all(
            datetime.fromisoformat(s).weekday() == 0 for s in starts
        )

    def test_daily_context_present(self, loaded):
        ctx = loaded.get("/api/overview?start=2026-03-01&end=2026-03-10").json()["daily_context"]
        assert BASE_DAY in ctx
        assert ctx[BASE_DAY]["resting_hr"] == 48.0
        assert ctx[BASE_DAY]["sleep_hours"] == pytest.approx(7.5, abs=0.01)


class TestManualCorrections:
    @pytest.fixture()
    def loaded(self, client):
        client.post("/api/import", files={"file": ("plan.csv", PLAN_CSV, "text/csv")})
        return client

    def test_force_link(self, loaded):
        planned = loaded.get("/api/plan?start=2026-03-01&end=2026-03-10").json()["planned"]
        intervals = next(p for p in planned if p["day"] == "2026-03-06")

        # Link the missed intervals session to the walk.
        r = loaded.post(f"/api/match/{intervals['id']}/link", json={"activity_id": "1004"})
        assert r.status_code == 200

        body = loaded.get("/api/overview?start=2026-03-01&end=2026-03-10").json()
        match = next(
            r for r in body["results"]
            if r.get("planned") and r["planned"]["id"] == intervals["id"]
        )
        assert match["status"] == "completed"
        assert match["manual"] is True
        assert match["actual"]["activity_id"] == "1004"

    def test_unlink_restores_automatic_matching(self, loaded):
        planned = loaded.get("/api/plan?start=2026-03-01&end=2026-03-10").json()["planned"]
        intervals = next(p for p in planned if p["day"] == "2026-03-06")
        loaded.post(f"/api/match/{intervals['id']}/link", json={"activity_id": "1004"})
        loaded.delete(f"/api/match/{intervals['id']}/link")

        body = loaded.get("/api/overview?start=2026-03-01&end=2026-03-10").json()
        match = next(
            r for r in body["results"]
            if r.get("planned") and r["planned"]["id"] == intervals["id"]
        )
        assert match["status"] == "missed"

    def test_link_unknown_plan_404s(self, loaded):
        assert loaded.post("/api/match/99999/link", json={"activity_id": "1"}).status_code == 404

    def test_ignore_activity_removes_it(self, loaded):
        loaded.post("/api/activities/1004/ignore")
        body = loaded.get("/api/overview?start=2026-03-01&end=2026-03-10").json()
        ids = [(r.get("actual") or {}).get("activity_id") for r in body["results"]]
        assert "1004" not in ids


class TestPlanCrud:
    def test_add_and_delete(self, client):
        r = client.post("/api/plan", json={"day": "2026-04-01", "session_type": "Swim"})
        assert r.json()["inserted"] == 1

        planned = client.get("/api/plan?start=2026-04-01&end=2026-04-02").json()["planned"]
        assert len(planned) == 1

        assert client.delete(f"/api/plan/{planned[0]['id']}").status_code == 200
        assert client.get("/api/plan?start=2026-04-01&end=2026-04-02").json()["planned"] == []

    def test_missing_fields_rejected(self, client):
        assert client.post("/api/plan", json={"day": "2026-04-01"}).status_code == 400

    def test_bad_range_rejected(self, client):
        assert client.get("/api/overview?start=2026-05-01&end=2026-04-01").status_code == 400


class TestUi:
    def test_index_served(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "Training Overlay" in r.text
