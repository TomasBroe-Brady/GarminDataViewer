#!/usr/bin/env bash
#
# Garmin Data Viewer - one-command setup.
#
#   ./setup.sh            full setup: config, Garmin login, start everything
#   ./setup.sh doctor     diagnose a stack that is already set up
#   ./setup.sh --no-login skip the Garmin login step
#
set -uo pipefail
cd "$(dirname "$0")" || exit 1

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[1;31m'; GRN=$'\033[1;32m'
YEL=$'\033[1;33m'; CYN=$'\033[1;36m'; OFF=$'\033[0m'

step() { printf '\n%s==>%s %s%s%s\n' "$CYN" "$OFF" "$BOLD" "$*" "$OFF"; }
ok()   { printf '    %s✓%s %s\n' "$GRN" "$OFF" "$*"; }
warn() { printf '    %s!%s %s\n' "$YEL" "$OFF" "$*"; }
bad()  { printf '    %s✗%s %s\n' "$RED" "$OFF" "$*"; }
die()  { printf '\n%sSetup stopped:%s %s\n\n' "$RED" "$OFF" "$*" >&2; exit 1; }

TOKEN_DIR=garminconnect-tokens

# Garmin's OAuth tokens land here once you log in. Their presence is how we
# know the login worked, so it is worth being precise about what counts.
tokens_present() {
  [ -d "$TOKEN_DIR" ] && [ -n "$(find "$TOKEN_DIR" -type f ! -name '.gitkeep' -print -quit 2>/dev/null)" ]
}

env_get() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2-; }

port_busy() {
  if command -v nc >/dev/null 2>&1; then nc -z 127.0.0.1 "$1" >/dev/null 2>&1; return $?; fi
  if command -v lsof >/dev/null 2>&1; then lsof -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; return $?; fi
  (exec 3<>"/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1 && { exec 3<&-; return 0; }
  return 1
}

# --------------------------------------------------------------- preflight
preflight() {
  step "Checking prerequisites"

  command -v docker >/dev/null 2>&1 \
    || die "Docker is not installed. Install Docker Desktop: https://docs.docker.com/get-docker/"
  ok "Docker installed ($(docker --version | cut -d, -f1))"

  if ! docker info >/dev/null 2>&1; then
    die "Docker is installed but not running.
    Start Docker Desktop (or 'sudo systemctl start docker' on Linux), wait for
    it to report Running, then run ./setup.sh again."
  fi
  ok "Docker daemon running"

  docker compose version >/dev/null 2>&1 \
    || die "Docker Compose v2 is required. Update Docker Desktop, or install the compose plugin."
  ok "Docker Compose v2 available"

  for p in 3000 8086 8787; do
    if port_busy "$p"; then
      warn "Port $p is already in use - change the matching *_PORT in .env if startup fails"
    fi
  done
}

# ------------------------------------------------------------------ config
configure() {
  step "Configuration"

  if [ -f .env ]; then
    ok ".env already exists, leaving it untouched"
  else
    cp .env.example .env
    local influx_pw graf_pw
    influx_pw="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)"
    graf_pw="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16)"
    if sed --version >/dev/null 2>&1; then SEDI=(sed -i); else SEDI=(sed -i ''); fi
    "${SEDI[@]}" "s|^INFLUXDB_PASSWORD=.*|INFLUXDB_PASSWORD=${influx_pw}|" .env
    "${SEDI[@]}" "s|^GRAFANA_PASSWORD=.*|GRAFANA_PASSWORD=${graf_pw}|"     .env
    ok "Created .env with generated passwords"
  fi

  # Guess the timezone if the user has not set one; otherwise activities can
  # land on the wrong calendar day.
  if [ -z "$(env_get USER_TIMEZONE)" ]; then
    local tz=""
    if command -v timedatectl >/dev/null 2>&1; then
      tz="$(timedatectl show -p Timezone --value 2>/dev/null)"
    fi
    [ -z "$tz" ] && [ -L /etc/localtime ] && tz="$(readlink /etc/localtime | sed 's|.*/zoneinfo/||')"
    if [ -n "$tz" ]; then
      if sed --version >/dev/null 2>&1; then SEDI=(sed -i); else SEDI=(sed -i ''); fi
      "${SEDI[@]}" "s|^USER_TIMEZONE=.*|USER_TIMEZONE=${tz}|" .env
      ok "Detected timezone: $tz"
    else
      warn "Could not detect your timezone - set USER_TIMEZONE in .env if days look shifted"
    fi
  fi

  mkdir -p "$TOKEN_DIR" overlay_data training_log
  # garmin-fetch-data runs as uid 1000 and must own its token directory.
  if [ "$(uname)" = "Linux" ]; then
    if ! chown -R 1000:1000 "$TOKEN_DIR" 2>/dev/null; then
      sudo chown -R 1000:1000 "$TOKEN_DIR" 2>/dev/null \
        || warn "Could not set $TOKEN_DIR ownership to 1000:1000 - if the fetcher reports
      permission errors, run: sudo chown -R 1000:1000 $TOKEN_DIR"
    fi
  fi
  ok "Directories ready"
}

# ------------------------------------------------------------------- login
garmin_login() {
  step "Garmin Connect login"

  if tokens_present; then
    ok "Already logged in (tokens found in $TOKEN_DIR)"
    return 0
  fi

  cat <<TXT

    You will be asked for your Garmin Connect email and password, and for the
    emailed code if you have two-factor turned on.

      ${DIM}Your password is typed into this terminal and sent straight to Garmin.
      It is never written to disk. Only the OAuth tokens Garmin returns are
      saved, in ./${TOKEN_DIR}/${OFF}

    Once it starts printing that it is fetching data, the login worked -
    ${BOLD}press Ctrl-C${OFF} and setup will carry on by itself.

TXT
  printf '    Press Enter when ready... '
  read -r _ || true
  echo

  # Watch for the tokens appearing so we can tell the user the moment they can
  # stop, rather than making them guess from the log output.
  (
    for _ in $(seq 1 600); do
      sleep 1
      if tokens_present; then
        printf '\n\n    %s✓ Login successful - tokens saved. Press Ctrl-C to continue.%s\n\n' "$GRN" "$OFF"
        break
      fi
    done
  ) &
  local watcher=$!

  # Ctrl-C reaches this script too; absorb it so setup continues instead of
  # dying at the very step the user was told to interrupt.
  trap ' ' INT
  docker compose run --rm garmin-fetch-data
  trap - INT

  kill "$watcher" 2>/dev/null; wait "$watcher" 2>/dev/null

  echo
  if tokens_present; then
    ok "Garmin login complete"
  else
    die "No tokens were saved, so the login did not complete.
    Common causes: wrong password, an MFA code that expired, or a
    permissions problem on ./${TOKEN_DIR}.
    Run ./setup.sh again to retry, or ./setup.sh doctor to diagnose."
  fi
}

# ------------------------------------------------------------------- start
start_stack() {
  step "Starting the stack"
  docker compose up -d || die "docker compose up failed. Run ./setup.sh doctor for details."
  ok "Containers started"

  printf '    waiting for Grafana'
  for _ in $(seq 1 60); do
    if curl -fsS "http://localhost:$(env_get GRAFANA_PORT || echo 3000)/api/health" >/dev/null 2>&1; then
      printf '\n'; ok "Grafana is up"; break
    fi
    printf '.'; sleep 2
  done
  printf '\n'

  printf '    waiting for the overlay'
  for _ in $(seq 1 30); do
    if curl -fsS "http://localhost:$(env_get OVERLAY_PORT || echo 8787)/api/health" >/dev/null 2>&1; then
      printf '\n'; ok "Training overlay is up"; break
    fi
    printf '.'; sleep 2
  done
  printf '\n'
}

# ------------------------------------------------------------------ doctor
doctor() {
  step "Diagnosing"

  if command -v docker >/dev/null 2>&1; then ok "Docker installed"
  else bad "Docker not installed"; fi

  if docker info >/dev/null 2>&1; then ok "Docker daemon running"
  else bad "Docker daemon not running"; fi

  if [ -f .env ]; then ok ".env present"
  else bad ".env missing - run ./setup.sh"; fi

  if tokens_present; then ok "Garmin tokens present"
  else bad "No Garmin tokens - run ./setup.sh to log in"; fi

  echo
  docker compose ps --format '    {{.Service}}\t{{.State}}\t{{.Status}}' 2>/dev/null \
    || bad "Could not list containers"
  echo

  local gport oport
  gport="$(env_get GRAFANA_PORT)"; gport="${gport:-3000}"
  oport="$(env_get OVERLAY_PORT)"; oport="${oport:-8787}"

  if curl -fsS "http://localhost:${gport}/api/health" >/dev/null 2>&1; then
    ok "Grafana responding on :${gport}"
  else
    bad "Grafana not responding on :${gport}"
  fi

  local health
  if health="$(curl -fsS "http://localhost:${oport}/api/health" 2>/dev/null)"; then
    ok "Overlay responding on :${oport}"
    case "$health" in
      *'"influxdb_reachable":true'*) ok "Overlay can read InfluxDB" ;;
      *) bad "Overlay cannot reach InfluxDB" ;;
    esac
    case "$health" in
      *'ActivitySummary'*) ok "Garmin activity data present in InfluxDB" ;;
      *) warn "No activity data yet - the first sync may still be running.
      Watch it with: docker compose logs -f garmin-fetch-data" ;;
    esac
  else
    bad "Overlay not responding on :${oport}"
  fi

  echo
  printf '    %sRecent fetcher output:%s\n' "$DIM" "$OFF"
  docker compose logs --tail 12 garmin-fetch-data 2>/dev/null | sed 's/^/      /' \
    || warn "No fetcher logs available"
  echo
}

# ------------------------------------------------------------------ finish
finish() {
  local gport oport guser gpass
  gport="$(env_get GRAFANA_PORT)"; gport="${gport:-3000}"
  oport="$(env_get OVERLAY_PORT)"; oport="${oport:-8787}"
  guser="$(env_get GRAFANA_USER)"; guser="${guser:-admin}"
  gpass="$(env_get GRAFANA_PASSWORD)"

  cat <<TXT

${GRN}────────────────────────────────────────────────────────────────${OFF}
${BOLD} Everything is running.${OFF}

   Dashboards         ${CYN}http://localhost:${gport}${OFF}
       username       ${BOLD}${guser}${OFF}
       password       ${BOLD}${gpass}${OFF}

   Training overlay   ${CYN}http://localhost:${oport}${OFF}   (no login)

 Your first sync is downloading history in the background - Grafana may
 look empty for a few minutes. Follow it with:

     docker compose logs -f garmin-fetch-data

 Next: put your training plan in a spreadsheet (start from
 training_log/TEMPLATE.csv) and import it at the overlay URL above.

 If anything looks wrong:   ${BOLD}./setup.sh doctor${OFF}
${GRN}────────────────────────────────────────────────────────────────${OFF}

TXT
}

# -------------------------------------------------------------------- main
case "${1:-}" in
  doctor) doctor; exit 0 ;;
  --no-login) preflight; configure; start_stack; finish; exit 0 ;;
  -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  "") ;;
  *) die "Unknown option: $1  (try: ./setup.sh --help)" ;;
esac

printf '\n%s  Garmin Data Viewer%s  -  setup\n' "$BOLD" "$OFF"
preflight
configure
garmin_login
start_stack
finish
