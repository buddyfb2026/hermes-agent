#!/usr/bin/env bash
# hermes-safe-update — wraps `hermes update` so the BIZ-208 fork survives upstream pulls.
#
# Problem this solves:
#   `hermes update` pulls upstream NousResearch/hermes-agent into ~/.hermes/hermes-agent.
#   It does `git stash --include-untracked` and `git checkout main` to make the working
#   tree clean before pulling. When the user is on a fork branch (biab-208) with custom
#   commits that aren't in main (the BIZ-208 hermes_jobs_worker.py — the asyncio task
#   that polls central-api's hermes_jobs queue and is the consumer that lets Linear
#   assignments turn into packet-authoring), those commits get orphaned. Gateway
#   processes restart on the upstream main code (no worker) and the Linear assignment
#   → packet-authoring WF silently breaks.
#
#   This wrapper detects that state and restores the fork branch + auto-stash + restarts
#   the Avenger gateways so the WF is back online before the script exits.
#
# Usage:
#   hermes-safe-update              # safe upgrade with auto-recovery
#   hermes-safe-update --check      # verify current state without upgrading
#   hermes-safe-update --restore    # just restore the fork branch (no upgrade)
#
# Live invariants this enforces post-update:
#   1. ~/.hermes/hermes-agent is on branch FORK_BRANCH (default biab-208)
#   2. CANARY_FILE exists (gateway/hermes_jobs_worker.py — the consumer)
#   3. All Avenger gateway LaunchAgents are running with fresh PIDs
#   4. Each Avenger gateway has a postgres connection (queue consumer is wired)
#   5. The configured Desktop connection is reachable before restart
#   6. The packaged app has a valid strict code signature
#   7. Desktop reaches ready state without boot, ticket, or renderer failures
#   8. The new Desktop process remains stable before the candidate is published
#
# Exit codes:
#   0  success — all invariants hold
#   1  upgrade itself failed (network, git, etc.)
#   2  post-upgrade invariants violated and could not be auto-restored
#   3  manual invocation error (bad flag, etc.)

set -uo pipefail

# ─── Config ──────────────────────────────────────────────────────────────────
HERMES_AGENT_DIR="${HERMES_AGENT_DIR:-${HOME}/.hermes/hermes-agent}"
FORK_BRANCH="${FORK_BRANCH:-biab-208-v020-20260819}"
CANARY_FILE="${CANARY_FILE:-gateway/hermes_jobs_worker.py}"
AVENGER_PROFILES=(hermes2 hermes3 hermes4 hermes5)
PRIMARY_LABEL="ai.hermes.gateway"
UID_NUM="$(id -u)"
HERMES_BIN="${HERMES_BIN:-${HOME}/.local/bin/hermes}"
MANAGED_SCRIPT="${MANAGED_SCRIPT:-${HOME}/.hermes/scripts/bizina-managed_update.py}"
DESKTOP_DATA_DIR="${DESKTOP_DATA_DIR:-${HOME}/Library/Application Support/Hermes}"
DESKTOP_LOG="${DESKTOP_LOG:-${HOME}/.hermes/logs/desktop.log}"
DESKTOP_ROLLOUT_TIMEOUT="${DESKTOP_ROLLOUT_TIMEOUT:-45}"
DESKTOP_STABILITY_SECONDS="${DESKTOP_STABILITY_SECONDS:-12}"
CODESIGN_BIN="${CODESIGN_BIN:-/usr/bin/codesign}"

# ─── Output helpers ──────────────────────────────────────────────────────────
log()  { printf "%s [%s] %s\n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2"; }
info() { log "info" "$*"; }
warn() { log "warn" "$*" >&2; }
err()  { log "err " "$*" >&2; }

bold()  { printf "\033[1m%s\033[0m" "$*"; }
green() { printf "\033[32m%s\033[0m" "$*"; }
red()   { printf "\033[31m%s\033[0m" "$*"; }
yellow(){ printf "\033[33m%s\033[0m" "$*"; }

# ─── Invariant checks ────────────────────────────────────────────────────────
verify_branch() {
  local current
  current=$(git -C "$HERMES_AGENT_DIR" branch --show-current 2>/dev/null)
  if [[ "$current" == "$FORK_BRANCH" ]]; then
    info "branch: $(green "$current") ✓"
    return 0
  fi
  warn "branch: expected $(bold "$FORK_BRANCH"), got $(bold "$current")"
  return 1
}

verify_canary_file() {
  if [[ -f "${HERMES_AGENT_DIR}/${CANARY_FILE}" ]]; then
    local size
    size=$(stat -f%z "${HERMES_AGENT_DIR}/${CANARY_FILE}" 2>/dev/null || echo 0)
    info "canary: $(green "${CANARY_FILE}") present ($size bytes)"
    return 0
  fi
  warn "canary: $(red "${CANARY_FILE}") MISSING"
  return 1
}

verify_gateway_postgres() {
  # Returns 0 only if EVERY Avenger gateway has at least one postgres connection.
  local all_ok=0
  for p in "${AVENGER_PROFILES[@]}"; do
    local label="ai.hermes.gateway-${p}"
    local pid
    pid=$(launchctl print "gui/${UID_NUM}/${label}" 2>/dev/null | awk -F'= ' '/pid =/{print $2; exit}' | tr -d ' ')
    if [[ -z "$pid" ]]; then
      warn "$label: pid not found"
      all_ok=1
      continue
    fi
    local pg
    pg=$(lsof -p "$pid" 2>/dev/null | grep -cE "TCP.*postgres|biab_central" || true)
    if [[ "$pg" -gt 0 ]]; then
      info "$label (pid=$pid): postgres conns=$(green "$pg") ✓"
    else
      warn "$label (pid=$pid): $(red "no postgres connection") (worker not running)"
      all_ok=1
    fi
  done
  return $all_ok
}

verify_no_stale_claims() {
  # Detect hermes_jobs rows that have been claimed for >STALE_THRESHOLD_MIN
  # without completing or failing. Catches:
  #   - Worker crash mid-run (lease still alive but no progress)
  #   - Auxiliary auth cascade hangs
  #   - Pre-2026-05-19 bug pattern (worker marks completed_at on session
  #     interrupt) IF it ever resurfaces post-patch
  # Returns 0 if no stale claims; 1 if any rows are stuck.
  local STALE_THRESHOLD_MIN=10
  local dsn
  dsn=$(grep '^HERMES_JOBS_DATABASE_URL=' "${HOME}/.hermes/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
  if [[ -z "$dsn" ]]; then
    warn "stale-claim check: HERMES_JOBS_DATABASE_URL not in ~/.hermes/.env, skipping"
    return 0   # don't fail the canary if DSN missing; just degrade
  fi
  if ! command -v psql >/dev/null 2>&1; then
    warn "stale-claim check: psql not on PATH, skipping"
    return 0
  fi
  local stale
  stale=$(psql "$dsn" -At -F$'\t' -c "
    SELECT issue_key,
           coalesce(claimed_by,'?'),
           date_trunc('second', age(now(), claimed_at))::text
      FROM hermes_jobs
     WHERE claimed_at IS NOT NULL
       AND claimed_at < now() - interval '${STALE_THRESHOLD_MIN} minutes'
       AND completed_at IS NULL
       AND failed_at IS NULL
     ORDER BY claimed_at
  " 2>/dev/null)
  if [[ -z "$stale" ]]; then
    info "stale claims: $(green "none") ✓ (threshold ${STALE_THRESHOLD_MIN}min)"
    return 0
  fi
  while IFS=$'\t' read -r key by age; do
    [[ -z "$key" ]] && continue
    warn "stale claim: $(red "${key}") claimed_by=${by} age=${age} (>${STALE_THRESHOLD_MIN}min, neither completed_at nor failed_at)"
  done <<< "$stale"
  return 1
}

# ─── Recovery actions ────────────────────────────────────────────────────────
restore_fork_branch() {
  info "restoring fork branch + stash"
  git -C "$HERMES_AGENT_DIR" checkout "$FORK_BRANCH" 2>&1 | sed 's/^/  /'
  # If the most-recent stash is the update auto-stash on our fork branch, pop it.
  local stash_line
  stash_line=$(git -C "$HERMES_AGENT_DIR" stash list 2>/dev/null | head -1)
  if [[ "$stash_line" == *"On ${FORK_BRANCH}: hermes-update-autostash"* ]]; then
    info "popping auto-stash: $stash_line"
    if ! git -C "$HERMES_AGENT_DIR" stash pop 2>&1 | sed 's/^/  /'; then
      err "stash pop failed — manual resolution may be required"
      err "  git -C $HERMES_AGENT_DIR status"
      err "  git -C $HERMES_AGENT_DIR stash list"
      return 1
    fi
  else
    info "no matching auto-stash to pop ($(yellow "${stash_line:-empty}"))"
  fi
}

restart_gateways() {
  info "restarting gateways"
  for p in "" "${AVENGER_PROFILES[@]}"; do
    local label
    [[ -z "$p" ]] && label="$PRIMARY_LABEL" || label="ai.hermes.gateway-${p}"
    local plist="${HOME}/Library/LaunchAgents/${label}.plist"
    if [[ ! -f "$plist" ]]; then
      warn "  $label: no plist found, skipping"
      continue
    fi
    local before
    before=$(launchctl print "gui/${UID_NUM}/${label}" 2>/dev/null | awk -F'= ' '/pid =/{print $2; exit}' | tr -d ' ')
    launchctl unload "$plist" 2>/dev/null || true
    sleep 1
    launchctl load "$plist" 2>/dev/null || true
    sleep 2
    local after
    after=$(launchctl print "gui/${UID_NUM}/${label}" 2>/dev/null | awk -F'= ' '/pid =/{print $2; exit}' | tr -d ' ')
    if [[ -n "$after" && "$before" != "$after" ]]; then
      info "  $label: $before → $(green "$after") ✓"
    else
      warn "  $label: $before → ${after:-?} (no PID change — investigate manually)"
    fi
  done
  info "waiting 8s for gateways to initialise + connect to postgres"
  sleep 8
}

restart_desktop() {
  local exe="${HERMES_AGENT_DIR}/apps/desktop/release/mac-arm64/Hermes.app/Contents/MacOS/Hermes"
  [[ -x "$exe" ]] || { err "Desktop executable missing after build: $exe"; return 1; }

  info "restarting Hermes Desktop from promoted source"
  python3 - "$exe" <<'PY'
import os, signal, subprocess, sys, time

exe = sys.argv[1]

def exact_pids():
    output = subprocess.check_output(["ps", "-axo", "pid=,command="], text=True)
    found = []
    for line in output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[1] == exe:
            found.append(int(parts[0]))
    return found

old = exact_pids()
for pid in old:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
for _ in range(50):
    if not exact_pids():
        break
    time.sleep(0.1)
for pid in exact_pids():
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass

subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(50):
    live = exact_pids()
    if live and not set(live).intersection(old):
        print(f"Desktop relaunched: pid={live[0]}")
        raise SystemExit(0)
    time.sleep(0.1)
raise SystemExit("Desktop did not relaunch")
PY
}

selected_connection_preflight() {
  # Catch the exact 2026-08-26 outage before taking Desktop down: the saved
  # primary pointed at 100.113.48.24:9119 while no service listened there.
  python3 - "$DESKTOP_DATA_DIR/connections.json" <<'PY'
import json, pathlib, sys, urllib.error, urllib.parse, urllib.request

path = pathlib.Path(sys.argv[1])
if not path.exists():
    print("Desktop connection preflight: no connections.json; local startup expected")
    raise SystemExit(0)
data = json.loads(path.read_text())
primary = data.get("primary", "local")
if primary == "local":
    print("Desktop connection preflight: primary=local")
    raise SystemExit(0)
record = next((row for row in data.get("connections", []) if row.get("id") == primary), None)
if not record or record.get("kind") != "remote" or not record.get("url"):
    raise SystemExit(f"Desktop connection preflight failed: invalid primary connection {primary!r}")
base = str(record["url"]).rstrip("/")
url = base + "/api/status"
try:
    with urllib.request.urlopen(url, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        json.loads(response.read())
except Exception as exc:
    raise SystemExit(f"Desktop connection preflight failed for {base}: {exc}")
print(f"Desktop connection preflight: {base} /api/status=200")
PY
}

desktop_log_line_count() {
  [[ -f "$DESKTOP_LOG" ]] && wc -l < "$DESKTOP_LOG" | tr -d ' ' || printf '0\n'
}

verify_desktop_rollout() {
  local start_line="${1:-0}"
  local exe="${HERMES_AGENT_DIR}/apps/desktop/release/mac-arm64/Hermes.app/Contents/MacOS/Hermes"
  python3 - "$exe" "$DESKTOP_LOG" "$start_line" "$DESKTOP_ROLLOUT_TIMEOUT" "$DESKTOP_STABILITY_SECONDS" "${DESKTOP_TEST_PID:-}" <<'PY'
import os, pathlib, subprocess, sys, time

exe, log_name = sys.argv[1], sys.argv[2]
start_line, timeout, settle = int(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5])
test_pid = int(sys.argv[6]) if sys.argv[6] else None
failure_markers = (
    "Desktop boot failed:",
    "[error-boundary:",
    "ReferenceError:",
    "Unhandled rejection:",
    "Uncaught Exception:",
)
ready_markers = (
    "Hermes backend is ready. Finalizing desktop startup",
    "Remote Hermes backend is ready",
)

def live_pids():
    if test_pid is not None:
        try:
            os.kill(test_pid, 0)
            return [test_pid]
        except ProcessLookupError:
            return []
    output = subprocess.check_output(["ps", "-axo", "pid=,command="], text=True)
    found = []
    for line in output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and (parts[1] == exe or parts[1].startswith(exe + " ")):
            found.append(int(parts[0]))
    return found

deadline = time.monotonic() + timeout
ready_at = None
while time.monotonic() < deadline:
    pids = live_pids()
    if not pids:
        raise SystemExit("Desktop rollout failed: Desktop process exited")
    path = pathlib.Path(log_name)
    lines = path.read_text(errors="replace").splitlines()[start_line:] if path.exists() else []
    segment = "\n".join(lines)
    for marker in failure_markers:
        if marker in segment:
            raise SystemExit(f"Desktop rollout failed: observed {marker}")
    if any(marker in segment for marker in ready_markers):
        ready_at = ready_at or time.monotonic()
        if time.monotonic() - ready_at >= settle:
            print(f"Desktop rollout healthy: pid={pids[0]} stable_for={settle:g}s")
            raise SystemExit(0)
    time.sleep(0.25)
raise SystemExit("Desktop rollout failed: no stable ready state before timeout")
PY
}

verify_desktop_bundle() {
  local app="${HERMES_AGENT_DIR}/apps/desktop/release/mac-arm64/Hermes.app"
  [[ -d "$app" ]] || { err "Desktop bundle missing: $app"; return 1; }
  if ! "$CODESIGN_BIN" --verify --deep --strict --verbose=2 "$app" 2>&1; then
    err "Desktop bundle signature is invalid; refusing to restart into a Gatekeeper failure"
    return 1
  fi
  info "Desktop bundle signature: valid on disk ✓"
}

backup_connection_state() {
  local backup_dir="$1"
  mkdir -p "$backup_dir/desktop-data"
  local name
  for name in connection.json connections.json; do
    if [[ -f "$DESKTOP_DATA_DIR/$name" ]]; then
      ditto "$DESKTOP_DATA_DIR/$name" "$backup_dir/desktop-data/$name"
    fi
  done
}

restore_app_and_connections() {
  local backup_dir="$1" live_app="$2"
  if [[ -d "$backup_dir/Hermes.app" ]]; then
    rm -rf "$live_app"
    ditto "$backup_dir/Hermes.app" "$live_app"
  fi
  local name
  for name in connection.json connections.json; do
    if [[ -f "$backup_dir/desktop-data/$name" ]]; then
      ditto "$backup_dir/desktop-data/$name" "$DESKTOP_DATA_DIR/$name"
    fi
  done
}

rollback_promotion() {
  local candidate="$1" backup_dir="$2" live_app="$3" reason="$4"
  err "runtime validation failed: $reason"
  err "rolling back source, Desktop bundle, connection state, and gateways"
  if ! managed rollback "$candidate" --reason "$reason" --yes; then
    err "ROLLBACK FAILED: source rollback was refused; preserving all evidence"
    return 2
  fi
  restore_app_and_connections "$backup_dir" "$live_app"
  verify_desktop_bundle || { err "ROLLBACK FAILED: previous Desktop bundle signature is invalid"; return 2; }
  restart_gateways
  local rollback_log_start
  rollback_log_start=$(desktop_log_line_count)
  restart_desktop || { err "ROLLBACK FAILED: previous Desktop did not relaunch"; return 2; }
  verify_desktop_rollout "$rollback_log_start" || { err "ROLLBACK FAILED: previous Desktop did not become healthy"; return 2; }
  info "rollback completed and previous Desktop is healthy"
  return 2
}

# ─── Modes ───────────────────────────────────────────────────────────────────
mode_check() {
  bold "=== hermes-safe-update check (read-only) ==="; echo
  local rc=0
  verify_branch           || rc=1
  verify_canary_file      || rc=1
  verify_gateway_postgres || rc=1
  verify_no_stale_claims  || rc=1
  selected_connection_preflight || rc=1
  verify_desktop_bundle   || rc=1
  if [[ $rc -eq 0 ]]; then
    info "$(green "ALL INVARIANTS HOLD") — WF is healthy"
  else
    err "$(red "INVARIANTS VIOLATED") — run without --check to attempt auto-recovery"
  fi
  return $rc
}

mode_restore() {
  bold "=== hermes-safe-update restore (no upgrade) ==="; echo
  if ! restore_fork_branch; then
    err "fork branch restore failed"
    return 2
  fi
  restart_gateways
  mode_check
}

managed() {
  [[ -x "$MANAGED_SCRIPT" ]] || { err "managed updater missing: $MANAGED_SCRIPT"; return 3; }
  python3 "$MANAGED_SCRIPT" "$@"
}

mode_prepare() {
  bold "=== hermes-safe-update prepare (live checkout remains untouched) ==="; echo
  mode_check || return 2
  managed prepare
}

mode_verify() {
  local candidate="${1:-}"
  [[ -n "$candidate" ]] || { err "--verify requires a candidate path"; return 3; }
  managed verify "$candidate"
}

mode_finalize() {
  local candidate="${1:-}"
  [[ -n "$candidate" ]] || { err "--finalize requires a candidate path"; return 3; }
  managed finalize "$candidate"
}

mode_promote() {
  local candidate="${1:-}" confirmation="${2:-}"
  local old_head backup_dir live_app
  [[ -n "$candidate" ]] || { err "--promote requires a candidate path"; return 3; }
  [[ "$confirmation" == "--yes" ]] || { err "promotion requires trailing --yes"; return 3; }
  old_head=$(git -C "$HERMES_AGENT_DIR" rev-parse HEAD)
  backup_dir="${HOME}/.hermes/update-backups/${old_head}"
  live_app="${HERMES_AGENT_DIR}/apps/desktop/release/mac-arm64/Hermes.app"
  mkdir -p "$backup_dir"
  if [[ -d "$live_app" ]]; then
    info "backing up current Desktop bundle to $backup_dir/Hermes.app"
    if [[ -e "${backup_dir}/Hermes.app" ]]; then
      mv "${backup_dir}/Hermes.app" "${backup_dir}/Hermes.app.previous.$(date +%s)"
    fi
    ditto "$live_app" "${backup_dir}/Hermes.app"
  fi
  backup_connection_state "$backup_dir"
  managed promote "$candidate" --yes || return 2
  info "building promoted Desktop bundle"
  if ! (cd "$HERMES_AGENT_DIR" && "$HERMES_BIN" desktop --build-only); then
    rollback_promotion "$candidate" "$backup_dir" "$live_app" "promoted Desktop build failed"
    return $?
  fi
  if ! verify_desktop_bundle; then
    rollback_promotion "$candidate" "$backup_dir" "$live_app" "promoted Desktop bundle failed strict code-signature verification"
    return $?
  fi
  if ! selected_connection_preflight; then
    rollback_promotion "$candidate" "$backup_dir" "$live_app" "configured Desktop connection preflight failed"
    return $?
  fi
  restart_gateways
  local rollout_log_start
  rollout_log_start=$(desktop_log_line_count)
  if ! restart_desktop; then
    rollback_promotion "$candidate" "$backup_dir" "$live_app" "Desktop did not relaunch"
    return $?
  fi
  if ! verify_desktop_rollout "$rollout_log_start"; then
    rollback_promotion "$candidate" "$backup_dir" "$live_app" "Desktop boot, ticket, renderer, or stability gate failed"
    return $?
  fi
  if ! mode_check; then
    rollback_promotion "$candidate" "$backup_dir" "$live_app" "post-promotion workflow invariants failed"
    return $?
  fi
  if ! managed complete "$candidate" --yes; then
    err "runtime is healthy but publish/receipt completion failed; candidate remains pending and is NOT reported promoted"
    return 2
  fi
  info "promotion committed only after Desktop runtime validation passed"
}

# ─── Entry point ─────────────────────────────────────────────────────────────
if [[ "${BIZINA_SAFE_UPDATE_LIB_ONLY:-0}" != "1" ]]; then
case "${1:-}" in
  --check)   mode_check ;;
  --restore) mode_restore ;;
  ""|--prepare|--update) mode_prepare ;;
  --finalize) mode_finalize "${2:-}" ;;
  --verify) mode_verify "${2:-}" ;;
  --promote) mode_promote "${2:-}" "${3:-}" ;;
  -h|--help)
    cat <<EOF
hermes-safe-update — prepares and promotes verified upstream integrations for the Bizina fork.

Usage:
  hermes-safe-update              prepare a candidate; NEVER mutates the live checkout
  hermes-safe-update --prepare    same as above
  hermes-safe-update --finalize PATH
                                  commit a fully resolved merge and refresh its receipt
  hermes-safe-update --verify PATH
                                  run the full fork verification suite in PATH
  hermes-safe-update --promote PATH --yes
                                  stage the verified candidate locally, back up source,
                                  app, and connection state, then rebuild/relaunch and
                                  validate Desktop. Publish only after runtime health;
                                  otherwise automatically roll back the whole transaction
  hermes-safe-update --check      verify current state (read-only)
  hermes-safe-update --restore    just restore the fork branch + restart (no upgrade)
  hermes-safe-update --help       this message

Invariants enforced after run:
  1. ~/.hermes/hermes-agent is on branch '${FORK_BRANCH}'
  2. ${CANARY_FILE} exists
  3. All ${#AVENGER_PROFILES[@]} Avenger gateways have a postgres connection (worker live)
  4. The configured Desktop connection passes preflight before restart
  5. The Desktop bundle passes strict code-signature verification
  6. Desktop reaches ready state without ticket/boot/renderer failures
  7. Desktop remains stable for ${DESKTOP_STABILITY_SECONDS}s before publication

Exit codes:
  0 — success, all invariants hold
  1 — candidate preparation or verification failed
  2 — post-upgrade invariants violated and could not be auto-restored
  3 — invocation error

Logs go to stdout; redirect as needed for cron-style use.
EOF
    exit 0
    ;;
  *)
    err "unknown flag: $1 (try --help)"
    exit 3
    ;;
esac
fi
