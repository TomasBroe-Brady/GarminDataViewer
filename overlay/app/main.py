"""FastAPI service: overlay your written training plan on Garmin actuals.

Wellness ingestion and the general dashboards are handled by garmin-grafana.
This service owns only the thing that project cannot do: knowing what you
*meant* to do, and reporting how that compares with what you did.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import get_settings
from .importer import ImportResult, build_rows, read_table
from .influx import InfluxReader, InfluxUnavailable
from .matching import PlannedSession, reconcile, summarise, week_start
from .nlq import SUGGESTIONS, answer_question

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("overlay")

STATIC_DIR = Path(__file__).parent / "static"

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    db.init_db()
    settings = get_settings()
    log.info("Overlay ready. DB=%s Influx=%s", settings.db_path, settings.influx_url)
    yield


app = FastAPI(
    title="Garmin Training Overlay",
    description="Reconciles a written training plan against Garmin Connect actuals.",
    version="0.1.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _default_range(start: str | None, end: str | None) -> tuple[str, str]:
    """Default to the last 8 weeks, which is about one training block."""
    today = date.today()
    hi = date.fromisoformat(end) if end else today
    lo = date.fromisoformat(start) if start else hi - timedelta(days=55)
    if lo > hi:
        raise HTTPException(400, "start date is after end date")
    return lo.isoformat(), hi.isoformat()


def _planned_objects(rows: list[dict[str, Any]]) -> list[PlannedSession]:
    return [
        PlannedSession(
            id=r["id"],
            day=r["day"],
            session_type=r["session_type"],
            title=r.get("title"),
            planned_duration_min=r.get("planned_duration_min"),
            planned_distance_km=r.get("planned_distance_km"),
            planned_intensity=r.get("planned_intensity"),
            planned_rpe=r.get("planned_rpe"),
            block=r.get("block"),
            week=r.get("week"),
            notes=r.get("notes"),
        )
        for r in rows
    ]


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict[str, Any]:
    reader = InfluxReader()
    influx_ok = reader.ping()
    with db.get_conn() as conn:
        lo, hi = db.plan_date_range(conn)
        planned_count = conn.execute("SELECT COUNT(*) AS n FROM planned_sessions").fetchone()["n"]

    payload: dict[str, Any] = {
        "status": "ok" if influx_ok else "degraded",
        "influxdb_reachable": influx_ok,
        "influxdb_url": get_settings().influx_url,
        "timezone": str(reader.tz),
        "planned_sessions": planned_count,
        "plan_range": {"start": lo, "end": hi},
    }
    if influx_ok:
        try:
            payload["measurements"] = sorted(reader.measurements())
        except InfluxUnavailable:
            pass
    else:
        payload["hint"] = (
            "InfluxDB is not reachable. Is the stack up (docker compose up -d) and has "
            "garmin-fetch-data completed a first sync?"
        )
    return payload


# --------------------------------------------------------------------------
# The main view: plan vs actual
# --------------------------------------------------------------------------

@app.get("/api/overview")
def overview(
    start: str | None = Query(None, description="ISO date, inclusive"),
    end: str | None = Query(None, description="ISO date, inclusive"),
    day_tolerance: int = Query(1, ge=0, le=3),
) -> dict[str, Any]:
    lo, hi = _default_range(start, end)

    with db.get_conn() as conn:
        planned_rows = db.fetch_planned(conn, lo, hi)
        overrides = db.fetch_overrides(conn)
        ignored = db.fetch_ignored(conn)

    reader = InfluxReader()
    try:
        actuals = reader.activities(lo, hi)
        context = reader.daily_context(lo, hi)
        influx_error = None
    except InfluxUnavailable as exc:
        # Still return the plan so the UI is useful while the stack warms up.
        actuals, context, influx_error = [], {}, str(exc)

    results = reconcile(
        _planned_objects(planned_rows),
        actuals,
        overrides=overrides,
        ignored_activity_ids=ignored,
        day_tolerance=day_tolerance,
    )

    # Group into ISO weeks for the calendar view.
    weeks: dict[str, dict[str, Any]] = {}
    for r in results:
        if not r.day:
            continue
        wk = week_start(r.day)
        slot = weeks.setdefault(wk, {"week_start": wk, "results": []})
        slot["results"].append(r.to_dict())
    for wk_slot in weeks.values():
        wk_slot["summary"] = summarise(
            [r for r in results if r.day and week_start(r.day) == wk_slot["week_start"]]
        )

    return {
        "range": {"start": lo, "end": hi},
        "summary": summarise(results),
        "results": [r.to_dict() for r in results],
        "weeks": [weeks[k] for k in sorted(weeks)],
        "daily_context": context,
        "influx_error": influx_error,
    }


@app.get("/api/activities")
def activities(
    start: str | None = None, end: str | None = None
) -> dict[str, Any]:
    lo, hi = _default_range(start, end)
    reader = InfluxReader()
    try:
        acts = reader.activities(lo, hi)
    except InfluxUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc

    with db.get_conn() as conn:
        ignored = db.fetch_ignored(conn)

    out = []
    for a in acts:
        d = {k: v for k, v in a.__dict__.items() if k != "raw"}
        d["ignored"] = a.activity_id in ignored
        out.append(d)
    return {"range": {"start": lo, "end": hi}, "activities": out}


# --------------------------------------------------------------------------
# Natural-language questions
# --------------------------------------------------------------------------

@app.get("/api/ask/suggestions")
def ask_suggestions() -> dict[str, Any]:
    return {"suggestions": SUGGESTIONS}


@app.get("/api/ask")
def ask(
    q: str = Query(..., min_length=3, description="A question about your plan vs wellness data"),
    start: str | None = Query(None),
    end: str | None = Query(None),
) -> dict[str, Any]:
    lo, hi = _default_range(start, end)

    with db.get_conn() as conn:
        planned_rows = db.fetch_planned(conn, lo, hi)
        overrides = db.fetch_overrides(conn)
        ignored = db.fetch_ignored(conn)

    reader = InfluxReader()
    try:
        actuals = reader.activities(lo, hi)
        context = reader.daily_context(lo, hi)
    except InfluxUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc

    results = reconcile(
        _planned_objects(planned_rows), actuals, overrides=overrides, ignored_activity_ids=ignored
    )
    answer = answer_question(q, results, context)
    answer["range"] = {"start": lo, "end": hi}
    return answer


# --------------------------------------------------------------------------
# Plan management
# --------------------------------------------------------------------------

@app.get("/api/plan")
def get_plan(start: str | None = None, end: str | None = None) -> dict[str, Any]:
    lo, hi = _default_range(start, end)
    with db.get_conn() as conn:
        return {"range": {"start": lo, "end": hi}, "planned": db.fetch_planned(conn, lo, hi)}


@app.post("/api/plan")
def add_planned(session: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if not session.get("day") or not session.get("session_type"):
        raise HTTPException(400, "day and session_type are required")
    with db.get_conn() as conn:
        n = db.insert_planned(conn, [session])
    return {"inserted": n}


@app.delete("/api/plan/{planned_id}")
def remove_planned(planned_id: int) -> dict[str, Any]:
    with db.get_conn() as conn:
        if not db.delete_planned(conn, planned_id):
            raise HTTPException(404, f"No planned session with id {planned_id}")
    return {"deleted": planned_id}


@app.post("/api/import")
async def import_plan(
    file: UploadFile = File(...),
    distance_in_miles: bool = Query(False),
    dry_run: bool = Query(False, description="Preview the parse without saving"),
) -> JSONResponse:
    """Upload a training-log CSV or Excel file."""
    content = await file.read()
    if not content:
        raise HTTPException(400, "Uploaded file was empty")

    filename = file.filename or "upload.csv"
    try:
        frame = read_table(content, filename)
    except Exception as exc:
        raise HTTPException(400, f"Could not read {filename}: {exc}") from exc

    result: ImportResult = build_rows(
        frame, filename=filename, distance_in_miles=distance_in_miles
    )

    if dry_run:
        return JSONResponse(
            {
                "dry_run": True,
                "would_import": result.imported,
                "skipped": result.skipped,
                "column_mapping": result.column_mapping,
                "unmapped_columns": result.unmapped_columns,
                "warnings": result.warnings,
                "preview": result.rows[:10],
            }
        )

    with db.get_conn() as conn:
        inserted = db.insert_planned(conn, result.rows)
        db.log_import(
            conn,
            result.batch,
            filename,
            inserted,
            result.skipped,
            "; ".join(result.warnings[:5]),
        )

    return JSONResponse(
        {
            "imported": inserted,
            "parsed": result.imported,
            "duplicates_skipped": result.imported - inserted,
            "skipped": result.skipped,
            "batch": result.batch,
            "column_mapping": result.column_mapping,
            "unmapped_columns": result.unmapped_columns,
            "warnings": result.warnings,
        }
    )


@app.get("/api/import/log")
def import_log() -> dict[str, Any]:
    with db.get_conn() as conn:
        return {"imports": db.fetch_import_log(conn)}


@app.delete("/api/import/{batch}")
def undo_import(batch: str) -> dict[str, Any]:
    with db.get_conn() as conn:
        n = db.delete_batch(conn, batch)
    return {"deleted": n, "batch": batch}


# --------------------------------------------------------------------------
# Manual corrections
# --------------------------------------------------------------------------

@app.post("/api/match/{planned_id}/link")
def link_match(planned_id: int, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Force a planned session to link to an activity, or to count as missed."""
    activity_id = payload.get("activity_id")
    with db.get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM planned_sessions WHERE id = ?", (planned_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(404, f"No planned session with id {planned_id}")
        db.set_override(conn, planned_id, activity_id, payload.get("note"))
    return {"planned_id": planned_id, "activity_id": activity_id}


@app.delete("/api/match/{planned_id}/link")
def unlink_match(planned_id: int) -> dict[str, Any]:
    with db.get_conn() as conn:
        cleared = db.clear_override(conn, planned_id)
    return {"planned_id": planned_id, "cleared": cleared}


@app.post("/api/activities/{activity_id}/ignore")
def ignore(activity_id: str) -> dict[str, Any]:
    with db.get_conn() as conn:
        db.ignore_activity(conn, activity_id)
    return {"ignored": activity_id}


@app.delete("/api/activities/{activity_id}/ignore")
def unignore(activity_id: str) -> dict[str, Any]:
    with db.get_conn() as conn:
        cleared = db.unignore_activity(conn, activity_id)
    return {"activity_id": activity_id, "cleared": cleared}


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(404, "UI not built")
    return FileResponse(index_file)
