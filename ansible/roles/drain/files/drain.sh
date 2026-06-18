#!/usr/bin/env bash
# Gracefully drain THIS box from the Cloudflare LB pool around a reboot (and undrain on boot) so a
# planned reboot is zero-interruption (ADR 0016). Driven by keygrip-drain.service (ExecStop=drain,
# ExecStart=undrain) — so every graceful reboot, including unattended-upgrades' kernel auto-reboot,
# drains first. Config + the CF token come from drain.env (0600).
#
#   drain.sh safe-reboot    # gate (3/3 etcd + peer), hand off the leader + drain, THEN reboot (refuses if unsafe)
#   drain.sh reboot-if-safe # like safe-reboot but only if a reboot is actually pending (the auto-reboot timer)
#   drain.sh check          # is it safe to lose this node? (read-only)
#   drain.sh drain          # ExecStop fallback: best-effort switchover + disable origin (never aborts)
#   drain.sh undrain        # ExecStart on boot: wait for local /readyz, then re-enable this box's origin
#
# KEY: the graceful prep (switchover + drain) happens BEFORE the reboot in safe-reboot/reboot-if-safe,
# while etcd/patroni/CF are all healthy — NOT at shutdown, where they may already be tearing down.
set -euo pipefail
. /opt/keygrip/drain/drain.env
API="https://api.cloudflare.com/client/v4"
PGHA=/opt/keygrip-pgha/compose.yml
log() { echo "[drain] $*"; }
cf()  { curl -fsS --max-time 15 -H "Authorization: Bearer ${CF_TOKEN}" "$@"; }

pool() { cf "${API}/accounts/${CF_ACCOUNT}/load_balancers/pools" | jq -c --arg n "${CF_POOL_NAME}" '.result[]|select(.name==$n)'; }

# ALL etcd members must be healthy before we take this DB node down — otherwise we drop below quorum
# (3 members, majority 2; rebooting one while another (e.g. the witness) is already down = 1/3 = no
# leader = outage). This is the check that was missing the day the witness silently disappeared.
etcd_all_healthy() {
  local out total ok
  out=$(docker compose -f "$PGHA" exec -T etcd etcdctl --endpoints=http://127.0.0.1:2379 \
        --command-timeout=5s endpoint health --cluster 2>&1) || true
  total=$(grep -cE 'is (healthy|unhealthy)' <<<"$out") || true
  ok=$(grep -c 'is healthy' <<<"$out") || true
  if [ "${total:-0}" -lt 3 ] || [ "${ok:-0}" -ne "${total:-0}" ]; then
    log "etcd: ${ok:-0}/${total:-0} members healthy (need 3/3) — cluster has NO margin to lose a node"
    return 1
  fi
}

# Is the OTHER box's origin enabled AND health-check-healthy? (Safety: never drain into a down peer.)
other_ready() {
  local p addr pid
  p=$(pool) || return 1
  [ "$(jq -r --arg o "${OTHER_ORIGIN}" '.origins[]|select(.name==$o)|.enabled' <<<"$p")" = "true" ] \
    || { log "peer ${OTHER_ORIGIN} is disabled"; return 1; }
  addr=$(jq -r --arg o "${OTHER_ORIGIN}" '.origins[]|select(.name==$o)|.address' <<<"$p")
  pid=$(jq -r .id <<<"$p")
  cf "${API}/accounts/${CF_ACCOUNT}/load_balancers/pools/${pid}/health" \
    | jq -e --arg a "$addr" '[.result.pop_health[].origins[]|to_entries[]|select(.key==$a)|.value.healthy]|any' >/dev/null \
    || { log "peer ${OTHER_ORIGIN} not healthy"; return 1; }
}

set_enabled() {  # $1=origin name  $2=true|false
  local p pid body
  p=$(pool); pid=$(jq -r .id <<<"$p")
  body=$(jq -c --arg o "$1" --argjson e "$2" \
    '{name, enabled, monitor, origins:[.origins[]|if .name==$o then .enabled=$e else . end]}' <<<"$p")
  cf -X PUT -H "Content-Type: application/json" "${API}/accounts/${CF_ACCOUNT}/load_balancers/pools/${pid}" --data "$body" >/dev/null
}

switchover_if_leader() {
  local leader
  leader=$(docker compose -f "$PGHA" exec -T patroni patronictl -c /etc/patroni/patroni.yml list -f json 2>/dev/null \
           | jq -r '.[]|select(.Role=="Leader")|.Member') || return 0
  if [ "$leader" = "${MY_PATRONI}" ]; then
    log "this box is the Postgres leader — switching over to ${OTHER_PATRONI}"
    docker compose -f "$PGHA" exec -T patroni patronictl -c /etc/patroni/patroni.yml \
      switchover --leader "${MY_PATRONI}" --candidate "${OTHER_PATRONI}" --force \
      || log "switchover failed (continuing — DB will fail over normally on reboot)"
  fi
}

# Graceful pre-reboot steps — BEST EFFORT, never abort: hand off the DB leader, then remove this box
# from the LB. Done BEFORE the reboot (safe-reboot / reboot-if-safe) WHILE everything is healthy, so
# the switchover (no DB-failover gap) and the CF call are reliable. Also run at shutdown as a fallback.
# Wait until HAProxy's write port (5000) has a healthy primary again — i.e. the leader handoff is
# done and writes work — before we let the reboot proceed.
wait_for_leader() {
  for _ in $(seq 1 20); do
    docker compose -f "$PGHA" exec -T patroni pg_isready -h 127.0.0.1 -p 5000 >/dev/null 2>&1 && return 0
    sleep 1
  done
  log "WARN: no writable leader on HAProxy :5000 after 20s (continuing anyway)"
}

prepare_for_reboot() {
  # 1. Leave the LB FIRST, so app traffic moves to the peer BEFORE the DB leader handoff — the brief
  #    no-writable-leader window then lands on the peer (which is becoming the leader), not on this box.
  if set_enabled "${MY_ORIGIN}" false; then
    log "DRAINED: ${MY_ORIGIN} disabled in pool ${CF_POOL_NAME}"
  else
    log "WARN: could not disable ${MY_ORIGIN} (CF unreachable?) — the /readyz monitor will catch it"
  fi
  sleep 3   # let CF stop routing here + in-flight requests finish
  # 2. Hand off the DB leader, then wait for the new leader to accept writes before rebooting.
  switchover_if_leader
  wait_for_leader
}

# ExecStop at shutdown. The reboot is ALREADY unstoppable here, so just do our best — NEVER abort
# (aborting would skip the drain entirely and cause exactly the outage we're avoiding; and at shutdown
# etcd/docker may be tearing down, so the quorum check is unreliable). Real gating lives in `check`.
drain() { prepare_for_reboot; }

# Deploy drain: pull THIS box out of the LB only — NO Postgres switchover (an app deploy doesn't
# touch the DB). Pair with `undrain` (waits for local /readyz, re-enables). Used by the rolling
# clip-web deploy so one box keeps serving while the other recreates. Best-effort, never aborts.
lb_out() {
  if set_enabled "${MY_ORIGIN}" false; then
    log "LB-OUT: ${MY_ORIGIN} disabled in pool ${CF_POOL_NAME}"
  else
    log "WARN: could not disable ${MY_ORIGIN} (CF unreachable?) — skipping (no rolling guarantee)"
  fi
  sleep 3   # let CF stop routing here + in-flight requests finish
}

# Can the cluster safely lose THIS node right now? (all 3 etcd members healthy + the peer up.)
reboot_safe() { etcd_all_healthy && other_ready; }

# safe-reboot (manual): gate FIRST, then prep while healthy, then reboot. Refuses if unsafe.
safe_reboot() {
  reboot_safe || { log "REFUSING reboot — the cluster cannot safely lose this node right now (see above)"; exit 1; }
  log "cluster fully healthy — handing off + draining, then rebooting"
  prepare_for_reboot
  systemctl reboot
}

# Guarded auto-reboot timer: only when a reboot is actually pending AND the cluster is fully healthy.
reboot_if_safe() {
  [ -f /run/reboot-required ] || { log "no reboot pending — nothing to do"; exit 0; }
  reboot_safe || { log "reboot pending but cluster not fully healthy — SKIPPING this window"; exit 0; }
  log "reboot pending + cluster healthy — handing off + draining, then rebooting"
  prepare_for_reboot
  systemctl reboot
}

undrain() {
  local code=000
  for _ in $(seq 1 60); do                       # wait up to ~5 min for the local app to be ready
    code=$(curl -fsS -o /dev/null -w '%{http_code}' --max-time 5 -H 'Host: app.vent.dog' http://127.0.0.1:8000/readyz 2>/dev/null || echo 000)
    [ "$code" = "200" ] && break
    sleep 5
  done
  set_enabled "${MY_ORIGIN}" true
  log "UNDRAINED: ${MY_ORIGIN} enabled (local /readyz=${code})"
}

case "${1:-}" in
  drain)          drain ;;
  lb-out)         lb_out ;;
  undrain)        undrain ;;
  safe-reboot)    safe_reboot ;;
  reboot-if-safe) reboot_if_safe ;;
  check)          reboot_safe && log "SAFE: 3/3 etcd healthy + peer up — ok to reboot this node" ;;
  *) echo "usage: $0 {drain|lb-out|undrain|safe-reboot|reboot-if-safe|check}"; exit 2 ;;
esac
