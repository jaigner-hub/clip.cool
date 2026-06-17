#!/usr/bin/env bash
# rolling-deploy.sh — zero-downtime rolling deploy across the HA pair.
#
# For each host, ONE AT A TIME (never two drained at once):
#   1. graceful drain  — drain.sh hands the Postgres leader off (Patroni switchover)
#                        and disables the CF Load-Balancer origin, so no user traffic
#                        and no ungraceful DB failover hit this box during the deploy.
#   2. deploy          — ./ac ansible-playbook <playbook> --limit <host>
#   3. health-gate     — wait for the app's /readyz to return 200 on this box.
#   4. undrain         — re-enable the CF origin (drain.sh re-checks /readyz first).
# The peer keeps serving throughout. If a box fails to deploy or never goes healthy,
# it is left DRAINED (out of the pool) and the script aborts for a human to look.
#
# Usage (run from ansible/, next to ./ac):
#   ./rolling-deploy.sh keygrip-web.yml
#   ./rolling-deploy.sh keygrip-web.yml keycloak.yml      # multiple playbooks, per box
#
# Env overrides:
#   DEPLOY_HOSTS   space-separated host list (default: the inventory 'dev' pair)
#   READYZ_HOST    Host header for the /readyz probe   (default: app.vent.dog)
#   READYZ_TIMEOUT seconds to wait for /readyz=200      (default: 150)
set -euo pipefail

cd "$(dirname "$0")"   # ansible/ — ./ac and playbooks/ are relative to here

# --- the pair. Mirrors the inventory 'dev' group; override with DEPLOY_HOSTS when it grows. ---
read -ra HOSTS <<<"${DEPLOY_HOSTS:-vent.dog vent.dog2}"
DRAIN=/opt/keygrip/drain/drain.sh
WEBAPP=keygrip-web-webapp-1
READYZ_HOST=${READYZ_HOST:-app.vent.dog}
READYZ_TIMEOUT=${READYZ_TIMEOUT:-150}

log()  { printf '\n\033[1;36m[rolling-deploy]\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31m[rolling-deploy] FATAL:\033[0m %s\n' "$*" >&2; exit 1; }

usage() { echo "usage: $0 <playbook.yml> [more.yml ...]" >&2; exit 2; }
[ $# -ge 1 ] || usage
for pb in "$@"; do [ -f "playbooks/$pb" ] || die "playbooks/$pb not found (run from ansible/)"; done
[ "${#HOSTS[@]}" -ge 1 ] || die "no hosts (set DEPLOY_HOSTS)"

# /readyz code for a host (200 = app + its deps healthy). Probed inside the webapp container so we
# don't depend on host port exposure or the Host-header allowlist of an external path.
readyz() {
  ssh -o ConnectTimeout=10 "$1" \
    "docker exec $WEBAPP sh -c 'curl -s -o /dev/null -w %{http_code} -H \"Host: $READYZ_HOST\" http://localhost:8000/readyz'" \
    2>/dev/null || true
}

wait_ready() {  # poll until /readyz=200 or timeout
  local h=$1 waited=0
  while [ "$waited" -lt "$READYZ_TIMEOUT" ]; do
    [ "$(readyz "$h")" = "200" ] && { log "$h: /readyz=200"; return 0; }
    sleep 3; waited=$((waited + 3))
  done
  return 1
}

# --- pre-flight: every box must be healthy before we drain ANYTHING ---
log "pre-flight health check: ${HOSTS[*]}"
for h in "${HOSTS[@]}"; do
  [ "$(readyz "$h")" = "200" ] || die "$h is not healthy (/readyz != 200) — refusing to start a rolling deploy"
done
log "all healthy. playbooks: $*"

# --- roll one box at a time ---
for h in "${HOSTS[@]}"; do
  log "================  $h  ================"

  log "$h: draining (Patroni leader switchover + CF LB disable)"
  ssh -o ConnectTimeout=20 "$h" "sudo $DRAIN drain" || die "$h: drain failed (nothing deployed yet)"

  for pb in "$@"; do
    log "$h: deploying playbooks/$pb"
    if ! ./ac ansible-playbook "playbooks/$pb" --limit "$h"; then
      die "$h: playbook '$pb' FAILED — box left DRAINED (out of the pool) for inspection"
    fi
  done

  log "$h: waiting for /readyz=200 (timeout ${READYZ_TIMEOUT}s)"
  wait_ready "$h" || die "$h: never returned /readyz=200 — box left DRAINED for inspection"

  log "$h: undraining (back into the CF pool)"
  ssh -o ConnectTimeout=20 "$h" "sudo $DRAIN undrain" || die "$h: undrain failed — re-enable manually: ssh $h sudo $DRAIN undrain"

  log "$h: ✅ deployed and back in the pool"
done

log "✅ rolling deploy complete across: ${HOSTS[*]}"
