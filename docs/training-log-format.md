# Training log format

The importer accepts a CSV or Excel file with **one row per planned session**.
It is deliberately forgiving: it tries to read the spreadsheet you already keep
rather than making you reformat one.

Start from [`training_log/TEMPLATE.csv`](../training_log/TEMPLATE.csv).

## The only hard requirement

A **date column**. Everything else is optional, though the more you give it the
better the matching gets.

## Recognised columns

Header matching is case-insensitive and ignores spaces and punctuation, so
`Planned Duration`, `planned_duration` and `PLANNED DURATION` are the same.

| What it means | Headers it accepts |
|---|---|
| Date of the session **(required)** | `Date`, `Day`, `When`, `Session Date`, `Workout Date` |
| Kind of session | `Session`, `Type`, `Workout`, `Activity`, `Discipline`, `Sport` |
| Session name | `Title`, `Name`, `Description`, `Session Name` |
| Planned duration | `Duration`, `Time`, `Minutes`, `Mins`, `Target Time` |
| Planned distance (km) | `Distance`, `KM`, `Kilometres`, `Target Distance` |
| Planned distance (miles) | `Miles`, `Mi`, `Distance Miles` |
| Intended effort | `Intensity`, `Effort`, `Zone`, `Pace` |
| Rate of perceived exertion | `RPE`, `Perceived Exertion`, `Exertion` |
| Training block / phase | `Block`, `Phase`, `Mesocycle`, `Cycle` |
| Week number | `Week`, `Wk`, `Week Number` |
| Free text | `Notes`, `Comment`, `Detail`, `Remarks` |

Columns it does not recognise are listed back to you as `unmapped_columns`
after an import. They are ignored, never guessed at.

## Formats it understands

**Dates** — `2026-03-02`, `02/03/2026`, `2 March 2026`, `2 Mar 26`.

> Ambiguous slash dates are read **day-first**: `04/03/2026` is 4 March, not
> 3 April. ISO dates (`2026-03-04`) are always taken literally.

**Durations** — `45`, `45 min`, `45mins`, `1:30`, `1:30:00`, `1h30`, `1h 30m`, `2 hours`.
A bare number is read as minutes.

**Distances** — `8`, `8km`, `8 km`, `5 miles`, `5mi`, `400m`, `10,5` (comma decimal).
A bare number is read as kilometres unless you tick **Distances are in miles**
on import, or the column is named something like `Miles`.

## Session types

The matcher maps your free text onto a category so it can pair a planned
session with what the watch recorded. `Easy run`, `Long run`, `Tempo`,
`Intervals` and `Fartlek` all become **run**; `Turbo`, `Zwift`, `Spin` and
`Ride` become **bike**; `Gym`, `Weights`, `Lifting` and `Core` become
**strength**, and so on for swim, walk, row, cardio and yoga.

A row whose type reads as **rest** (`Rest`, `Rest day`, `Off`) is treated
specially: it counts as kept if nothing was recorded that day, and is flagged
if you trained anyway.

Anything unrecognised becomes **other**, which matches permissively rather
than not at all.

## Preview before you commit

Always hit **Preview** first. It shows the detected column mapping, any
unmapped columns, warnings for rows it could not read, and the first ten
parsed rows — without writing anything to the database.

## Re-importing

Re-importing the same file is safe. A row is considered a duplicate when the
date, session type, duration and distance all match something already stored,
so a corrected spreadsheet adds only what actually changed.

To undo a whole import, note the `batch` id from the import response and call
`DELETE /api/import/{batch}`.
