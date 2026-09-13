"""SQLite storage for the training plan and manual match corrections.

Garmin actuals live in InfluxDB (written by garmin-fetch-data) and are never
copied here - this database holds only what Garmin does not know about: what
you *intended* to do, and any corrections you make to the automatic matching.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS planned_sessions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    day                   TEXT NOT NULL,              -- ISO date YYYY-MM-DD
    session_type          TEXT NOT NULL,              -- run | bike | swim | strength | rest | other
    title                 TEXT,
    planned_duration_min  REAL,
    planned_distance_km   REAL,
    planned_intensity     TEXT,                       -- easy | moderate | hard | race
    planned_rpe           REAL,                       -- 1-10 if you log it
    block                 TEXT,                       -- training block, e.g. "Base 2"
    week                  INTEGER,                    -- week number within the plan
    notes                 TEXT,
    import_batch          TEXT,                       -- which import produced this row
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_planned_day ON planned_sessions(day);

-- Manual corrections to the automatic matcher. Absence of a row means "trust
-- the matcher"; a row here always wins.
CREATE TABLE IF NOT EXISTS match_overrides (
    planned_id    INTEGER PRIMARY KEY,
    activity_id   TEXT,                               -- NULL = force "missed"
    note          TEXT,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (planned_id) REFERENCES planned_sessions(id) ON DELETE CASCADE
);

-- Activities you want the matcher to ignore entirely (e.g. a walk to the shop
-- that the watch auto-detected).
CREATE TABLE IF NOT EXISTS ignored_activities (
    activity_id   TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch         TEXT NOT NULL,
    filename      TEXT,
    rows_imported INTEGER,
    rows_skipped  INTEGER,
    detail        TEXT,
    created_at    TEXT NOT NULL
);
"""


def _connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or get_settings().db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_conn(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = _connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path: Path | None = None) -> None:
    with get_conn(path) as conn:
        conn.executescript(SCHEMA)


# --------------------------------------------------------------------------
# Planned sessions
# --------------------------------------------------------------------------

PLAN_FIELDS = (
    "day",
    "session_type",
    "title",
    "planned_duration_min",
    "planned_distance_km",
    "planned_intensity",
    "planned_rpe",
    "block",
    "week",
    "notes",
    "import_batch",
)


def insert_planned(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Insert planned sessions, skipping exact duplicates already stored.

    "Exact duplicate" means same day + type + duration + distance, which makes
    re-importing a corrected spreadsheet safe: genuinely new or edited rows are
    added, unchanged ones are not doubled up.
    """
    from datetime import datetime

    now = datetime.now().isoformat(timespec="seconds")
    inserted = 0
    for row in rows:
        dup = conn.execute(
            "SELECT 1 FROM planned_sessions WHERE day = ? AND session_type = ? "
            "AND IFNULL(planned_duration_min, -1) = IFNULL(?, -1) "
            "AND IFNULL(planned_distance_km, -1) = IFNULL(?, -1)",
            (
                row.get("day"),
                row.get("session_type"),
                row.get("planned_duration_min"),
                row.get("planned_distance_km"),
            ),
        ).fetchone()
        if dup:
            continue

        data = {k: row.get(k) for k in PLAN_FIELDS}
        data["created_at"] = row.get("created_at") or now
        data["updated_at"] = row.get("updated_at") or now

        cols = list(data)
        conn.execute(
            f"INSERT INTO planned_sessions ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            [data[c] for c in cols],
        )
        inserted += 1
    return inserted


def fetch_planned(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM planned_sessions WHERE day BETWEEN ? AND ? ORDER BY day, id",
        (start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_planned(conn: sqlite3.Connection, planned_id: int) -> bool:
    cur = conn.execute("DELETE FROM planned_sessions WHERE id = ?", (planned_id,))
    return cur.rowcount > 0


def delete_batch(conn: sqlite3.Connection, batch: str) -> int:
    cur = conn.execute("DELETE FROM planned_sessions WHERE import_batch = ?", (batch,))
    return cur.rowcount


def plan_date_range(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    row = conn.execute("SELECT MIN(day) AS lo, MAX(day) AS hi FROM planned_sessions").fetchone()
    return (row["lo"], row["hi"]) if row else (None, None)


# --------------------------------------------------------------------------
# Match overrides / ignored activities
# --------------------------------------------------------------------------

def set_override(
    conn: sqlite3.Connection, planned_id: int, activity_id: str | None, note: str | None = None
) -> None:
    from datetime import datetime

    conn.execute(
        "INSERT INTO match_overrides (planned_id, activity_id, note, created_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(planned_id) DO UPDATE SET "
        "activity_id = excluded.activity_id, note = excluded.note",
        (planned_id, activity_id, note, datetime.now().isoformat(timespec="seconds")),
    )


def clear_override(conn: sqlite3.Connection, planned_id: int) -> bool:
    cur = conn.execute("DELETE FROM match_overrides WHERE planned_id = ?", (planned_id,))
    return cur.rowcount > 0


def fetch_overrides(conn: sqlite3.Connection) -> dict[int, str | None]:
    rows = conn.execute("SELECT planned_id, activity_id FROM match_overrides").fetchall()
    return {r["planned_id"]: r["activity_id"] for r in rows}


def ignore_activity(conn: sqlite3.Connection, activity_id: str) -> None:
    from datetime import datetime

    conn.execute(
        "INSERT OR IGNORE INTO ignored_activities (activity_id, created_at) VALUES (?, ?)",
        (activity_id, datetime.now().isoformat(timespec="seconds")),
    )


def unignore_activity(conn: sqlite3.Connection, activity_id: str) -> bool:
    cur = conn.execute("DELETE FROM ignored_activities WHERE activity_id = ?", (activity_id,))
    return cur.rowcount > 0


def fetch_ignored(conn: sqlite3.Connection) -> set[str]:
    return {r["activity_id"] for r in conn.execute("SELECT activity_id FROM ignored_activities")}


def log_import(
    conn: sqlite3.Connection,
    batch: str,
    filename: str,
    imported: int,
    skipped: int,
    detail: str = "",
) -> None:
    from datetime import datetime

    conn.execute(
        "INSERT INTO import_log (batch, filename, rows_imported, rows_skipped, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (batch, filename, imported, skipped, detail,
         datetime.now().isoformat(timespec="seconds")),
    )


def fetch_import_log(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM import_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]
