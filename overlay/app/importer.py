"""Import a hand-kept training log (CSV / Excel) into the planned-sessions table.

Real training logs are messy: the date column might be called "Date", "Day" or
"When"; durations get written as "45", "45 min", "1:30" or "1h30"; distance
might be km or miles. This importer tries hard to accept a spreadsheet as it
was actually kept, rather than demanding one be reformatted first.

Anything it cannot confidently interpret is reported back as a skipped row with
a reason, never guessed at.
"""

from __future__ import annotations

import io
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)

# Canonical field -> header spellings we accept (lowercased, non-alnum stripped).
COLUMN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "day": ("date", "day", "sessiondate", "when", "workoutdate", "traindate"),
    "session_type": (
        "type", "session", "sessiontype", "workout", "workouttype",
        "activity", "activitytype", "discipline", "sport",
    ),
    "title": ("title", "name", "sessionname", "workoutname", "description", "session_description"),
    "planned_duration_min": (
        "duration", "durationmin", "durationminutes", "time", "minutes", "mins",
        "plannedduration", "planneddurationmin", "targettime", "durationmins",
    ),
    "planned_distance_km": (
        "distance", "distancekm", "km", "kms", "kilometres", "kilometers",
        "planneddistance", "planneddistancekm", "targetdistance",
    ),
    "planned_distance_mi": ("distancemi", "distancemiles", "miles", "mi"),
    "planned_intensity": ("intensity", "effort", "zone", "pace", "targetzone", "targetintensity"),
    "planned_rpe": ("rpe", "perceivedexertion", "exertion"),
    "block": ("block", "phase", "mesocycle", "trainingblock", "cycle"),
    "week": ("week", "wk", "weeknumber", "weekno", "weeknum"),
    "notes": ("notes", "note", "comment", "comments", "detail", "details", "remarks"),
}

MILES_TO_KM = 1.609344


@dataclass
class ImportResult:
    batch: str
    filename: str
    imported: int = 0
    skipped: int = 0
    column_mapping: dict[str, str] = field(default_factory=dict)
    unmapped_columns: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------
# Value parsers
# --------------------------------------------------------------------------

def _norm_header(h: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(h).strip().lower())


def parse_duration_minutes(value: Any) -> float | None:
    """Accept 45, '45', '45 min', '1:30', '1:30:00', '1h30', '90mins'."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None

    # pandas may hand us a real timedelta or a time object.
    if isinstance(value, pd.Timedelta):
        return round(value.total_seconds() / 60.0, 1)
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None

    text = str(value).strip().lower()
    if not text:
        return None

    # H:MM or H:MM:SS
    if ":" in text:
        parts = [p for p in re.split(r"[:]", text) if p.strip() != ""]
        try:
            nums = [float(re.sub(r"[^0-9.]", "", p) or 0) for p in parts]
        except ValueError:
            return None
        if len(nums) == 2:
            return round(nums[0] * 60 + nums[1], 1)
        if len(nums) >= 3:
            return round(nums[0] * 60 + nums[1] + nums[2] / 60.0, 1)

    # "1h30", "1h 30m", "2 hr"
    hm = re.match(r"^\s*(\d+(?:\.\d+)?)\s*h(?:ours?|rs?|r)?\s*(\d+(?:\.\d+)?)?\s*m?", text)
    if hm:
        hours = float(hm.group(1))
        mins = float(hm.group(2)) if hm.group(2) else 0.0
        return round(hours * 60 + mins, 1)

    # plain number, possibly with a unit suffix
    num = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(min|mins|minutes|m)?\s*$", text)
    if num:
        return float(num.group(1))

    return None


def parse_distance_km(value: Any, *, assume_miles: bool = False) -> float | None:
    """Accept 8, '8', '8km', '5 miles', '5mi', '10 k'."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)):
        km = float(value)
        return round(km * MILES_TO_KM, 3) if assume_miles else round(km, 3)

    text = str(value).strip().lower()
    if not text:
        return None

    num_match = re.search(r"(\d+(?:[.,]\d+)?)", text)
    if not num_match:
        return None
    num = float(num_match.group(1).replace(",", "."))

    # No leading \b on the unit: "8km" has no word boundary between digit and
    # letter, so requiring one would silently misread the most common spelling.
    if re.search(r"(?:km|kms|kilomet)", text):
        return round(num, 3)
    if re.search(r"(?:mi|mile|miles)\b", text):
        return round(num * MILES_TO_KM, 3)
    if re.search(r"(?:m|metre|metres|meter|meters)\b", text):
        return round(num / 1000.0, 3)
    if assume_miles:
        return round(num * MILES_TO_KM, 3)
    return round(num, 3)


def parse_day(value: Any) -> str | None:
    """Return an ISO date string, or None if the value is not a usable date."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()

    text = str(value).strip()
    if not text:
        return None

    # An unambiguous ISO date must be taken literally. pandas applies
    # `dayfirst` even to YYYY-MM-DD, turning 2026-03-02 into 3 February.
    iso = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$", text)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3))).isoformat()
        except ValueError:
            return None

    # Otherwise assume day-first: a hand-kept log is far more likely to be
    # DD/MM/YYYY than MM/DD/YYYY.
    try:
        ts = pd.to_datetime(text, dayfirst=True, errors="raise")
        if pd.isna(ts):
            return None
        return ts.date().isoformat()
    except (ValueError, TypeError, pd.errors.ParserError):
        return None


def parse_number(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"(\d+(?:[.,]\d+)?)", str(value))
    return float(m.group(1).replace(",", ".")) if m else None


def _clean_text(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


# --------------------------------------------------------------------------
# Column mapping
# --------------------------------------------------------------------------

def detect_columns(headers: list[Any]) -> tuple[dict[str, str], list[str]]:
    """Map spreadsheet headers onto our canonical fields.

    Returns (mapping of canonical_field -> original header, unmapped headers).
    """
    mapping: dict[str, str] = {}
    used: set[str] = set()

    normalised = {h: _norm_header(h) for h in headers}

    # Exact synonym hits first, so "distance km" beats a fuzzy "distance".
    for field_name, synonyms in COLUMN_SYNONYMS.items():
        for header, norm in normalised.items():
            if header in used:
                continue
            if norm in synonyms:
                mapping[field_name] = header
                used.add(header)
                break

    # Then a prefix/substring pass for anything still unmapped.
    for field_name, synonyms in COLUMN_SYNONYMS.items():
        if field_name in mapping:
            continue
        for header, norm in normalised.items():
            if header in used or not norm:
                continue
            if any(norm.startswith(s) or s in norm for s in synonyms):
                mapping[field_name] = header
                used.add(header)
                break

    unmapped = [str(h) for h in headers if h not in used]
    return mapping, unmapped


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------

def read_table(content: bytes, filename: str) -> pd.DataFrame:
    """Read CSV or Excel bytes into a DataFrame."""
    suffix = Path(filename).suffix.lower()
    if suffix in (".xlsx", ".xlsm", ".xls"):
        return pd.read_excel(io.BytesIO(content))
    # Be tolerant of BOMs and stray blank lines from spreadsheet exports.
    return pd.read_csv(io.BytesIO(content), encoding="utf-8-sig", skip_blank_lines=True)


def build_rows(
    df: pd.DataFrame,
    *,
    filename: str = "upload",
    mapping_override: dict[str, str] | None = None,
    distance_in_miles: bool = False,
) -> ImportResult:
    """Turn a training-log DataFrame into planned_session row dicts."""
    batch = uuid.uuid4().hex[:12]
    result = ImportResult(batch=batch, filename=filename)

    df = df.dropna(how="all")
    if df.empty:
        result.warnings.append("The file contained no data rows.")
        return result

    mapping, unmapped = detect_columns(list(df.columns))
    if mapping_override:
        mapping.update({k: v for k, v in mapping_override.items() if v in df.columns})
    result.column_mapping = mapping
    result.unmapped_columns = unmapped

    if "day" not in mapping:
        result.warnings.append(
            "Could not find a date column. Name one of your columns 'Date' "
            f"(seen: {', '.join(str(c) for c in df.columns)})."
        )
        return result

    # A miles column, if present, overrides the km reading.
    miles_col = mapping.get("planned_distance_mi")
    assume_miles = distance_in_miles or bool(miles_col)

    now = datetime.now().isoformat(timespec="seconds")

    for idx, raw in df.iterrows():
        row_no = int(idx) + 2  # +2: 1-based, plus the header row

        day = parse_day(raw.get(mapping["day"]))
        if not day:
            result.skipped += 1
            result.warnings.append(f"Row {row_no}: unreadable date, skipped.")
            continue

        session_type = (
            _clean_text(raw.get(mapping["session_type"])) if "session_type" in mapping else None
        )
        title = _clean_text(raw.get(mapping["title"])) if "title" in mapping else None

        # A log often puts the session in the title and leaves type blank.
        if not session_type and title:
            session_type = title
        if not session_type:
            result.skipped += 1
            result.warnings.append(
                f"Row {row_no}: no session type or title, skipped."
            )
            continue

        if miles_col is not None:
            distance_km = parse_distance_km(raw.get(miles_col), assume_miles=True)
        elif "planned_distance_km" in mapping:
            distance_km = parse_distance_km(
                raw.get(mapping["planned_distance_km"]), assume_miles=assume_miles
            )
        else:
            distance_km = None

        week_val = parse_number(raw.get(mapping["week"])) if "week" in mapping else None

        result.rows.append(
            {
                "day": day,
                "session_type": session_type,
                "title": title,
                "planned_duration_min": (
                    parse_duration_minutes(raw.get(mapping["planned_duration_min"]))
                    if "planned_duration_min" in mapping
                    else None
                ),
                "planned_distance_km": distance_km,
                "planned_intensity": (
                    _clean_text(raw.get(mapping["planned_intensity"]))
                    if "planned_intensity" in mapping
                    else None
                ),
                "planned_rpe": (
                    parse_number(raw.get(mapping["planned_rpe"]))
                    if "planned_rpe" in mapping
                    else None
                ),
                "block": _clean_text(raw.get(mapping["block"])) if "block" in mapping else None,
                "week": int(week_val) if week_val is not None else None,
                "notes": _clean_text(raw.get(mapping["notes"])) if "notes" in mapping else None,
                "import_batch": batch,
                "created_at": now,
                "updated_at": now,
            }
        )
        result.imported += 1

    return result
