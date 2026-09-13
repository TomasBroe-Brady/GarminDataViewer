#!/usr/bin/env bash
# One-time setup for the Garmin Data Viewer stack.
set -euo pipefail

cd "$(dirname "$0")"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "Docker is not installed. See https://docs.docker.com/get-docker/"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required (try: docker compose version)"

if [[ ! -f .env ]]; then
  say "Creating .env from .env.example"
  cp .env.example .env
  # Generate real passwords so the stack is not left on the example defaults.
  influx_pw="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)"
  graf_pw="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 16)"
  if sed --version >/dev/null 2>&1; then SEDI=(sed -i); else SEDI=(sed -i ''); fi
  "${SEDI[@]}" "s|^INFLUXDB_PASSWORD=.*|INFLUXDB_PASSWORD=${influx_pw}|"  .env
  "${SEDI[@]}" "s|^GRAFANA_PASSWORD=.*|GRAFANA_PASSWORD=${graf_pw}|"      .env
  say "Generated random passwords."
  printf '\n    Grafana login:  \033[1madmin\033[0m / \033[1m%s\033[0m\n\n' "$graf_pw"
  say "These are saved in .env - keep it, it is git-ignored."
else
  say ".env already exists, leaving it alone."
fi

# garmin-fetch-data runs as uid/gid 1000 and needs to own its token directory.
say "Preparing token + data directories"
mkdir -p garminconnect-tokens overlay_data training_log
if [[ "$(uname)" == "Linux" ]]; then
  if ! chown -R 1000:1000 garminconnect-tokens 2>/dev/null; then
    warn "Could not chown garminconnect-tokens to 1000:1000; trying with sudo"
    sudo chown -R 1000:1000 garminconnect-tokens || \
      warn "Still failed. If the fetcher reports permission errors, see README > Troubleshooting."
  fi
fi

cat <<'NEXT'

------------------------------------------------------------------------
Setup complete.  Next step - log in to Garmin Connect once, interactively:

    docker compose run --rm garmin-fetch-data

  Enter your Garmin email and password at the prompt (plus the emailed MFA
  code if you have two-factor on).  Tokens are then cached and you will not
  be asked again.  Let it run until it starts printing that it is fetching
  data, then press Ctrl-C.

Then bring the whole stack up in the background:

    docker compose up -d

    Dashboards        ->  http://localhost:3000
    Training overlay  ->  http://localhost:8787
------------------------------------------------------------------------

NEXT
