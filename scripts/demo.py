#!/usr/bin/env python3
"""Run the overlay against synthetic Garmin data - no Docker, no Garmin account.

    python scripts/demo.py

Starts a stub InfluxDB that serves eight weeks of realistic Venu 3 data, seeds
a matching training plan with deliberate gaps, and serves the overlay UI. Use
it to see the interface before connecting the real stack, or to work on the UI
without a Garmin sync running.

Nothing here touches your real overlay database: the demo writes to its own
file under overlay_data/.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "overlay"))

# A fixed seed keeps the demo stable between runs, so a screenshot or a UI
# tweak is comparable with the last one.
RNG = random.Random(20260913)

WEEKS = 8

ACTIVITY_COLUMNS = [
    "time", "Activity_ID", "activityName", "activityType", "distance",
    "elapsedDuration", "movingDuration", "averageSpeed", "averageHR", "maxHR",
    "calories", "elevationGain", "aerobicTrainingEffect",
    "anaerobicTrainingEffect", "activityTrainingLoad",
]

# One training week, repeated. Deliberately includes a rest day.
WEEK_TEMPLATE = [
    # (weekday, session, duration_min, distance_km, intensity, notes)
    (0, "Easy run",  45,  8.0,  "easy",     "Keep HR under 145"),
    (1, "Gym",       55,  None, "moderate", "Upper body + core"),
    (2, "Intervals", 50,  9.0,  "hard",     "6 x 800m off 90s"),
    (3, "Rest day",  None, None, None,      ""),
    (4, "Easy run",  40,  7.0,  "easy",     ""),
    (5, "Gym",       50,  None, "moderate", "Lower body"),
    (6, "Long run",  105, 18.0, "easy",     "Fuel every 40 min"),
]

GARMIN_TYPE = {
    "Easy run": "running",
    "Intervals": "running",
    "Long run": "running",
    "Gym": "strength_training",
}


def _ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def build_dataset(end_day: date) -> tuple[list[list], dict[str, list], list[dict]]:
    """Return (activity rows, daily context rows by measurement, plan rows)."""
    start = end_day - timedelta(days=WEEKS * 7 - 1)
    start -= timedelta(days=start.weekday())          # align to a Monday

    activities: list[list] = []
    plan: list[dict] = []
    daily: dict[str, list] = {"DailyStats": [], "SleepSummary": [], "TrainingReadiness": []}
    activity_id = 9000

    for week in range(WEEKS):
        for weekday, session, dur, dist, intensity, notes in WEEK_TEMPLATE:
            day = start + timedelta(days=week * 7 + weekday)
            if day > end_day:
                continue

            plan.append(
                {
                    "day": day.isoformat(),
                    "session_type": session,
                    "title": session,
                    "planned_duration_min": float(dur) if dur else None,
                    "planned_distance_km": float(dist) if dist else None,
                    "planned_intensity": intensity,
                    "block": f"Base {week // 4 + 1}",
                    "week": week + 1,
                    "notes": notes or None,
                }
            )

            if session == "Rest day":
                continue

            # Miss roughly one session in eight, so the UI shows real gaps.
            if RNG.random() < 0.12:
                continue

            activity_id += 1
            # Actuals drift a little either side of the plan.
            actual_dur = dur * RNG.uniform(0.88, 1.12)
            actual_dist = dist * RNG.uniform(0.92, 1.08) if dist else None
            hard = intensity == "hard"
            avg_hr = RNG.uniform(155, 168) if hard else RNG.uniform(132, 148)

            activities.append(
                [
                    _ms(datetime.combine(day, datetime.min.time()) + timedelta(hours=18)),
                    float(activity_id),
                    session,
                    GARMIN_TYPE.get(session, "running"),
                    actual_dist * 1000 if actual_dist else None,
                    actual_dur * 60,
                    actual_dur * 60 * 0.98,
                    (actual_dist * 1000) / (actual_dur * 60) if actual_dist else None,
                    round(avg_hr, 1),
                    round(avg_hr + RNG.uniform(12, 25), 1),
                    round(actual_dur * RNG.uniform(9, 13), 0),
                    round(actual_dist * 8, 0) if actual_dist else None,
                    round(RNG.uniform(2.4, 4.4), 1),
                    round(RNG.uniform(0.2, 1.6), 1),
                    round(actual_dur * RNG.uniform(1.4, 2.2), 0),
                ]
            )
            # The END marker the real writer emits - exercises our filtering.
            activities.append(
                [
                    _ms(
                        datetime.combine(day, datetime.min.time())
                        + timedelta(hours=18, minutes=actual_dur)
                    ),
                    float(activity_id), "END", "No Activity",
                    None, None, None, None, None, None, None, None, None, None, None,
                ]
            )

        # An unplanned walk most weeks.
        if RNG.random() < 0.7:
            day = start + timedelta(days=week * 7 + RNG.randint(0, 6))
            if day <= end_day:
                activity_id += 1
                walk_min = RNG.uniform(25, 50)
                activities.append(
                    [
                        _ms(datetime.combine(day, datetime.min.time()) + timedelta(hours=12)),
                        float(activity_id), "Lunch Walk", "walking",
                        walk_min * 85, walk_min * 60, walk_min * 60 * 0.95, 1.4,
                        round(RNG.uniform(88, 102), 1), round(RNG.uniform(105, 120), 1),
                        round(walk_min * 4, 0), 12.0, 0.6, 0.0, 6.0,
                    ]
                )

    # Daily wellness context across the whole window.
    day = start
    while day <= end_day:
        midnight = datetime.combine(day, datetime.min.time())
        daily["DailyStats"].append(
            [
                _ms(midnight),
                round(RNG.uniform(44, 53), 1),                 # resting HR
                round(RNG.uniform(7500, 15500), 0),            # steps
                round(RNG.uniform(420, 900), 0),               # active kcal
                round(RNG.uniform(10, 45), 0),                 # vigorous min
                round(RNG.uniform(15, 60), 0),                 # moderate min
            ]
        )
        daily["SleepSummary"].append(
            [
                _ms(midnight + timedelta(hours=7)),
                round(RNG.uniform(62, 92), 0),                 # sleep score
                round(RNG.uniform(5.8, 8.4) * 3600, 0),        # sleep seconds
                round(RNG.uniform(48, 78), 0),                 # overnight HRV
                round(RNG.uniform(30, 60), 0),                 # body battery change
                round(RNG.uniform(12, 28), 0),                 # sleep stress
            ]
        )
        daily["TrainingReadiness"].append(
            [
                _ms(midnight + timedelta(hours=7)),
                round(RNG.uniform(45, 92), 0),                 # readiness
                round(RNG.uniform(240, 460), 0),               # acute load
                round(RNG.uniform(4, 30), 0),                  # recovery hours
            ]
        )
        day += timedelta(days=1)

    return activities, daily, plan


def make_handler(activities: list[list], daily: dict[str, list]):
    ctx_columns = {
        "DailyStats": ["time", "rhr", "steps", "active_kcal", "vig_min", "mod_min"],
        "SleepSummary": ["time", "score", "secs", "hrv", "bb_change", "sleep_stress"],
        "TrainingReadiness": ["time", "readiness", "acute_load", "recovery_time"],
    }

    def result(name, columns, values):
        return {
            "results": [
                {"statement_id": 0,
                 "series": [{"name": name, "columns": columns, "values": values}]}
            ]
        }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            q = (parse_qs(urlparse(self.path).query).get("q") or [""])[0]

            if "SHOW MEASUREMENTS" in q:
                body = result("measurements", ["name"],
                              [[m] for m in ["ActivitySummary", *ctx_columns]])
            elif "ActivitySummary" in q:
                body = result("ActivitySummary", ACTIVITY_COLUMNS, activities)
            else:
                for measurement, columns in ctx_columns.items():
                    if measurement in q:
                        body = result(measurement, columns, daily[measurement])
                        break
                else:
                    body = {"results": [{"statement_id": 0}]}

            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8787, help="port for the overlay UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--end", default=date.today().isoformat(), help="last day of demo data (ISO)"
    )
    args = parser.parse_args()

    end_day = date.fromisoformat(args.end)
    activities, daily, plan = build_dataset(end_day)

    # Stub InfluxDB on an ephemeral port.
    influx = HTTPServer(("127.0.0.1", 0), make_handler(activities, daily))
    threading.Thread(target=influx.serve_forever, daemon=True).start()
    influx_host, influx_port = influx.server_address

    demo_db = ROOT / "overlay_data" / "demo.db"
    demo_db.parent.mkdir(parents=True, exist_ok=True)
    demo_db.unlink(missing_ok=True)

    os.environ.update(
        {
            "INFLUXDB_HOST": str(influx_host),
            "INFLUXDB_PORT": str(influx_port),
            "INFLUXDB_USER": "demo",
            "INFLUXDB_PASSWORD": "demo",
            "INFLUXDB_DB": "GarminStats",
            "OVERLAY_DB": str(demo_db),
            "USER_TIMEZONE": "UTC",
        }
    )

    from app import config, db

    config.get_settings.cache_clear()
    db.init_db()
    with db.get_conn() as conn:
        inserted = db.insert_planned(conn, plan)

    real_sessions = sum(1 for a in activities if a[2] != "END")
    print(f"  Demo data : {WEEKS} weeks ending {end_day}")
    print(f"  Plan      : {inserted} planned sessions")
    print(f"  Actuals   : {real_sessions} recorded activities")
    print(f"  Database  : {demo_db}  (separate from your real data)")
    print(f"\n  Open http://{args.host}:{args.port}\n")

    import uvicorn
    from app.main import app

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
