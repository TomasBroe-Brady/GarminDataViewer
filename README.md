# Garmin Data Viewer

A self-hosted dashboard for your Garmin data, with one thing no other project
does: it overlays **the training plan you actually wrote** on top of what your
watch recorded, and tells you where the two diverged.

Built for a **Garmin Venu 3**, but works with any watch that syncs to Garmin
Connect.

```
Garmin Connect ──► garmin-fetch-data ──► InfluxDB ──┬──► Grafana          (wellness dashboards)
                                                     └──► Training Overlay (plan vs actual)
                    ▲                                          ▲
                    └── off-the-shelf, BSD-3                   └── this repo
```

## Why it is built this way

The wellness half of this problem is already solved well, so this repo does not
reinvent it. Ingestion and dashboards come from
[**garmin-grafana**](https://github.com/arpanghosh8453/garmin-grafana)
(BSD-3-Clause), which is the most complete open-source Garmin fetcher available
— it covers exactly the Venu 3 metric set: HRV, Body Battery, SpO2, breathing
rate, sleep stages, stress, HR zones and GPS. Knowing which of Garmin's
undocumented endpoints yield which metrics is the genuinely brittle part, and
that project already tracks it.

What no existing project does is connect your **intended** training to your
**actual** training. Grafana is a time-series panel tool; it cannot join a
spreadsheet of planned sessions against recorded activities. That gap is what
the overlay service in this repo fills.

Other projects considered, and when to prefer them:

| Project | Strength | Use instead if |
|---|---|---|
| [garmin-grafana](https://github.com/arpanghosh8453/garmin-grafana) | Wellness metrics + polished Grafana dashboards | You only want charts and no plan tracking — this repo just wraps it |
| [GarminDB](https://github.com/tcgoetz/GarminDB) | Mature ingestion to SQLite; also reads the watch over USB and the GDPR export | You want Jupyter-notebook analysis, or need the USB/export import paths |
| [FIT Dashboard](https://github.com/arpanghosh8453/fit-dashboard) | Deep per-activity telemetry, route maps, side-by-side comparison | You care about analysing individual workouts rather than trends |

## What you get

**Grafana** at `localhost:3000` — the prebuilt garmin-grafana dashboards: heart
rate, sleep and sleep stages, stress, Body Battery, HRV, steps heatmap, SpO2,
breathing rate, activity timeline, HR zones, GPS routes, VO2 max, and a
separate strength-training dashboard.

**Training Overlay** at `localhost:8787` — this repo's addition:

- Import your training log from a spreadsheet (CSV or Excel)
- Automatic matching of planned sessions to recorded activities
- Per-week and per-range **adherence**: completed / missed / unplanned
- **Deltas**: did you run 8.4km against a planned 8km, or 6km?
- **Recovery context** per week — average sleep, resting HR and overnight HRV,
  so a missed session can be read against how you were actually doing
- **Ask a question** — "How did my sleep look in weeks I missed a session?" —
  answered deterministically from your plan and wellness data, no LLM involved
- Manual override when the matcher gets a pairing wrong

---

## See it before you connect anything

There is a [live preview of the overlay](https://claude.ai/code/artifact/44b11ca4-3239-4c7b-be4b-ebfb8fbcd12c)
running on generated data - no install needed, and nothing connected to a
real Garmin account.

To run the same thing locally with realistic data — no Docker, no Garmin account:

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r overlay/requirements.txt
.venv/bin/python scripts/demo.py
```

Then open http://localhost:8787. It generates eight weeks of synthetic Venu 3
data and a matching training plan with deliberate gaps, so you can see all
three outcomes (completed, missed, unplanned) before committing to setup.

The demo writes to its own `overlay_data/demo.db` and never touches your real
data.

---

## Setup

### Requirements

Docker and Docker Compose v2. Nothing else — no Python or Node install needed.

### 1. Clone and run setup

```bash
git clone https://github.com/TomasBroe-Brady/GarminDataViewer.git
cd GarminDataViewer
./setup.sh
```

That single command does everything:

1. Checks Docker is installed and actually running, and warns about port clashes
2. Creates `.env` with generated passwords and detects your timezone
3. Prepares the token directory with the ownership `garmin-fetch-data` needs
4. Prompts you to log in to Garmin Connect, and tells you the moment it worked
5. Starts the stack and waits until Grafana and the overlay are answering
6. Prints your URLs and Grafana password

At the login step you type your Garmin email and password into your own
terminal. **The password is never written to disk** — only the OAuth tokens
Garmin returns are saved, in `./garminconnect-tokens/`, which is git-ignored.
Setup watches for those tokens and tells you when to press `Ctrl-C` to carry on,
so you are not guessing from the log output.

If anything looks wrong afterwards:

```bash
./setup.sh doctor
```

It checks Docker, the containers, both HTTP endpoints, whether the overlay can
reach InfluxDB, whether any Garmin data has actually landed yet, and tails the
fetcher log — so you get a named problem instead of a wall of container output.

| Service | URL | Notes |
|---|---|---|
| Grafana | http://localhost:3000 | Login printed by `setup.sh`, also in `.env` |
| Training Overlay | http://localhost:8787 | No login |
| InfluxDB | http://localhost:8086 | Rarely accessed directly |

The first backfill pulls your history and can take a while. Follow it with:

```bash
docker compose logs -f garmin-fetch-data
```

### 2. Import your training plan

Move your notebook into a spreadsheet. Start from
[`training_log/TEMPLATE.csv`](training_log/TEMPLATE.csv):

| Date | Session | Duration | Distance | Intensity | RPE | Block | Week | Notes |
|---|---|---|---|---|---|---|---|---|
| 2026-03-02 | Easy run | 45 min | 8km | easy | 4 | Base 1 | 1 | Keep HR under 145 |
| 2026-03-04 | Gym | 55 min | | moderate | 6 | Base 1 | 1 | Upper body + core |
| 2026-03-06 | Intervals | 50 min | 9km | hard | 8 | Base 1 | 1 | 6 x 800m off 90s |

Open http://localhost:8787, choose the file, and hit **Preview** to check how
the columns were read before anything is saved. Then **Import**.

The importer is deliberately forgiving — a `Date` column is the only hard
requirement, and it understands `45 min`, `1:30`, `1h30`, `8km`, `5 miles` and
day-first dates. See [`docs/training-log-format.md`](docs/training-log-format.md)
for the full list of accepted headers and formats.

---

## How matching works

For each planned session the matcher looks for a recorded activity that agrees
on **type**, is **within a day**, and is close on **duration and distance**. It
scores every viable pairing and assigns greedily, best match first.

It is deliberately conservative. A planned swim will never absorb a recorded
run, however close in time — a wrong match produces a misleading adherence
number, which is worse than an honest gap.

| Outcome | Meaning |
|---|---|
| **Completed** | Planned and recorded, with the deltas between them |
| **Missed** | Planned, nothing matching recorded |
| **Unplanned** | Recorded, not in the plan |

A **rest day** counts as kept when nothing was recorded, and is flagged if you
trained anyway.

When the matcher gets it wrong, override it:

```bash
# Force a planned session to link to a specific activity
curl -X POST localhost:8787/api/match/12/link -H 'Content-Type: application/json' \
     -d '{"activity_id": "1004"}'

# Force it to count as missed
curl -X POST localhost:8787/api/match/12/link -H 'Content-Type: application/json' -d '{}'

# Back to automatic
curl -X DELETE localhost:8787/api/match/12/link

# Tell the matcher to ignore an auto-detected activity entirely
curl -X POST localhost:8787/api/activities/1004/ignore
```

---

## API

The overlay exposes a small REST API; interactive docs at
http://localhost:8787/docs.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Service status, InfluxDB reachability, measurements found |
| `GET` | `/api/overview?start=&end=` | Plan vs actual, grouped by week, with summary and recovery context |
| `GET` | `/api/activities?start=&end=` | Recorded activities in a range |
| `GET` | `/api/plan?start=&end=` | Planned sessions |
| `POST` | `/api/plan` | Add one planned session |
| `DELETE` | `/api/plan/{id}` | Delete a planned session |
| `POST` | `/api/import?dry_run=` | Upload a training log (CSV/Excel) |
| `GET` | `/api/import/log` | Past imports |
| `DELETE` | `/api/import/{batch}` | Undo a whole import |
| `POST`/`DELETE` | `/api/match/{id}/link` | Set or clear a manual match |
| `POST`/`DELETE` | `/api/activities/{id}/ignore` | Ignore or unignore an activity |
| `GET` | `/api/ask?q=` | Ask a question spanning plan + wellness data, e.g. "How did my sleep look in weeks I missed a session?" |
| `GET` | `/api/ask/suggestions` | Example questions the asker understands |

---

## Repository layout

```
docker-compose.yml          the whole stack
setup.sh                    one-time setup
grafana/
  datasources/              auto-provisioned InfluxDB connection
  dashboards/               prebuilt garmin-grafana dashboards
overlay/                    this repo's service (Python 3.12 + FastAPI)
  app/
    influx.py               read-only InfluxDB client for Garmin actuals
    importer.py             forgiving CSV/Excel training-log parser
    matching.py             plan-vs-actual reconciliation engine
    nlq.py                  deterministic natural-language question answering
    db.py                   SQLite: planned sessions, manual overrides
    main.py                 REST API
    static/                 the UI (no build step - edit and refresh)
  tests/                    99 tests
scripts/demo.py             run the UI on synthetic data, no Docker needed
training_log/               put your spreadsheet here (git-ignored)
docs/
```

## Development

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r overlay/requirements.txt pytest ruff

cd overlay && ../.venv/bin/python -m pytest tests/ -q
.venv/bin/ruff check overlay/
```

Tests use a stub InfluxDB server that speaks the real 1.x `/query` JSON, so the
HTTP path, InfluxQL building and response parsing are all exercised without
needing the stack running.

To run the overlay outside Docker against a running stack:

```bash
cd overlay
INFLUXDB_HOST=localhost INFLUXDB_PASSWORD=$(grep INFLUXDB_PASSWORD ../.env | cut -d= -f2) \
OVERLAY_DB=../overlay_data/overlay.db \
../.venv/bin/python -m uvicorn app.main:app --reload --port 8787
```

---

## Privacy and security

- Everything runs on your machine. No third party sees your health data.
- Your Garmin **password is never stored** — only OAuth tokens, in
  `./garminconnect-tokens/`.
- `.gitignore` excludes tokens, `.env`, the databases and your training log.
  **Do not commit `garminconnect-tokens/`** — those tokens grant access to your
  Garmin account.
- The services bind to `localhost`. Do not expose them to the internet without
  putting authentication in front of them.

## Troubleshooting

**Start here: `./setup.sh doctor`.** It names the failing component rather
than leaving you to read container logs.

**`garmin-fetch-data` reports permission errors on the token directory.** It
runs as uid 1000. Run `sudo chown -R 1000:1000 garminconnect-tokens`.

**Grafana shows "No data".** The first backfill may still be running — check
`docker compose logs -f garmin-fetch-data`. Confirm the time range in Grafana
covers days you have data for.

**The overlay says Garmin data is unavailable.** It still shows your plan.
Check `curl localhost:8787/api/health` — if `influxdb_reachable` is false, the
stack is not up or the first sync has not finished.

**Activities land on the wrong day.** Set `USER_TIMEZONE` in `.env` to your
IANA zone, e.g. `Europe/Dublin`, then `docker compose up -d`.

**MFA code never arrives.** Garmin sends it by email, not SMS. Check spam.

## Roadmap

- [x] Natural-language questions over combined plan and wellness data
- [ ] In-app training-plan editor, so the spreadsheet becomes optional
- [ ] Planned-vs-actual load tracking (acute:chronic ratio against the plan)
- [ ] Push the overlay's derived metrics back into InfluxDB for Grafana panels

## Credits

- [garmin-grafana](https://github.com/arpanghosh8453/garmin-grafana) by Arpan
  Ghosh (BSD-3-Clause) — data ingestion and the Grafana dashboards. The
  measurement and field names this repo reads were taken from that project's
  writer.
- [python-garminconnect](https://github.com/cyberjunky/python-garminconnect)
  and [garth](https://github.com/matin/garth) — the Garmin Connect client and
  SSO auth underneath it.

## Licence

MIT for the code in this repository. Bundled Grafana dashboard JSON originates
from garmin-grafana and remains under its BSD-3-Clause licence.
