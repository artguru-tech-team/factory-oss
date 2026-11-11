#!/bin/bash
# Keeps `factory serve` up on 127.0.0.1:3045, from the checkout this script sits in.
# Live, that checkout is ~/factory-live (through the shared tunnel in ~/.cloudflared/config.yml).
# Cron every 5 min; idempotent. Logs only restarts.
# The tunnel itself is kept alive by ~/ds-pages/tunnel-watchdog.sh.
REPO=${FACTORY_REPO:-$(cd "$(dirname "$0")/.." && pwd)}
LIVE_REPO="$HOME/factory-live"

if [ -z "$FACTORY_PORT" ]; then
  REPO_REAL=$(realpath "$REPO" 2>/dev/null || echo "$REPO")
  LIVE_REAL=$(realpath "$LIVE_REPO" 2>/dev/null || echo "$LIVE_REPO")
  if [ "$REPO_REAL" != "$LIVE_REAL" ]; then
    echo "deploy/watchdog.sh: refusing to run in development checkout without FACTORY_PORT" >&2
    exit 1
  fi
fi

STATE=${FACTORY_HOME:-$HOME/.factory}
mkdir -p "$STATE"

PORT=${FACTORY_PORT:-3045}  # FACTORY_PORT: tests run it on another port
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/")
[ "$code" = "401" ] && exit 0

if ss -ltn | grep -q "127.0.0.1:$PORT "; then
  pids=$(ss -ltnp | grep "127.0.0.1:$PORT " | grep -oP 'pid=\K[0-9]+' | sort -u)
  [ -n "$pids" ] && kill $pids 2>/dev/null
  for i in {1..50}; do
    ss -ltn | grep -q "127.0.0.1:$PORT " || break
    [ "$i" -eq 25 ] && [ -n "$pids" ] && kill -9 $pids 2>/dev/null
    sleep 0.2
  done
  reason="port listened but answered $code"
else
  reason="not listening"
fi

# Run from an agent session (a deploy), the server would sit in that session's systemd scope and
# die when T3 stops the session (2026-10-30 outage), so there it gets its own scope. Cron starts
# keep the plain start: they live in cron.service, which outlives the user manager (no lingering).
plain=(setsid -f nohup python3 bin/factory serve --port "$PORT")
serve=("${plain[@]}")
if grep -q "/user@$(id -u).service/" /proc/self/cgroup; then
  export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
  serve=(systemd-run --user --scope --collect --quiet --unit="factory-serve-$PORT-$(date +%s%N)" "${plain[@]}")
fi
# systemd-run fails only before starting anything (no bus): fall back to the plain start then
(cd "$REPO" && { "${serve[@]}" || "${plain[@]}"; } >>"$STATE/serve.log" 2>&1 </dev/null)
echo "$(date -u +%FT%TZ) restarted factory serve ($reason)" >>"$STATE/watchdog.log"
