#!/usr/bin/env bash
# Take this box out of service gracefully, then put it back (ADR 0016): stop the local cloudflared
# connector so Cloudflare routes the app tunnel's hostnames to the peer, then hand off the Postgres
# leader. Driven by keygrip-drain.service (ExecStop=drain, ExecStart=undrain) — so every graceful
# reboot, including unattended-upgrades' kernel auto-reboot, drains first. Config + the CF token come
# from drain.env (0600).
#
# HOW TRAFFIC ACTUALLY LEAVES THIS BOX (rewritten 2026-07-30 — there is no longer a Load Balancer):
# one tunnel, `vent-keygrip2`, has a connector on EACH box, both serving the same remotely-managed
# ingress against their OWN local services. Stopping the connector deregisters its connections and the
# edge is left with only the peer's — that IS the drain, and it needs no API call. The Cloudflare API is
# used only to CONFIRM it (my connections gone / the peer's still there); without a working token those
# checks degrade to a fixed settle wait plus the Patroni peer check, and the drain still happens.
#
# ⚠️ It stops the `cloudflared` SERVICE only. vent.dog also runs `cloudflared-chat`, the connector for
# the vent.dog-only chat tunnel (chat/livekit.vent.dog — Dendrite and LiveKit are single-active). That
# one has no peer to fail over to, so stopping it would take chat DOWN rather than move it. Leave it.
#
# What this replaced, and why it matters: the previous version drained by disabling this box's origin in
# a Cloudflare LOAD BALANCER pool. When the account API token was rotated outside the repo, that call
# started returning 401 — and because the API call WAS the drain, everything downstream failed with it:
# `check` failed, rolling-deploy.sh aborted before deploying anything, and reboot_safe() (the same check)
# made the guarded auto-reboot timer refuse every window, so kernel updates silently stopped landing.
# Ported from keygrip's #161.
#
#   drain.sh safe-reboot    # gate (3/3 etcd + peer), hand off the leader + drain, THEN reboot (refuses if unsafe)
#   drain.sh reboot-if-safe # like safe-reboot but only if a reboot is actually pending (the auto-reboot timer)
#   drain.sh check          # is it safe to lose this node? (read-only)
#   drain.sh drain          # ExecStop fallback: best-effort stop-connector + switchover (never aborts)
#   drain.sh undrain        # ExecStart on boot: wait for local /readyz, THEN start the connector
#
# KEY: the graceful prep (drain + switchover) happens BEFORE the reboot in safe-reboot/reboot-if-safe,
# while etcd/patroni/the connector are all healthy — NOT at shutdown, where they may already be tearing
# down. And `undrain` is what re-arms a drained box: `restart: unless-stopped` will not restart a
# container that was explicitly stopped, so a drained reboot comes back taking no traffic until it runs.
set -euo pipefail
. /opt/keygrip/drain/drain.env
API="https://api.cloudflare.com/client/v4"
# From drain.env (Ansible renders these from the same vars roles/postgres_ha and roles/cloudflared use).
# The fallbacks are for a box whose drain.env predates them: under `set -u` an unset var aborts the
# script, which would fail every reboot gate instead of just missing the newer check.
PGHA="${PGHA_COMPOSE:-/opt/keygrip-pgha/compose.yml}"
ETCD_ENDPOINT="http://127.0.0.1:${ETCD_CLIENT_PORT:-2379}"
CFD="${CLOUDFLARED_COMPOSE:-/opt/keygrip/cloudflared/compose.yml}"
BACKUP_LOCK="${BACKUP_LOCK:-/run/lock/keygrip-pgha-backup.lock}"
READYZ_HOST="${READYZ_HOST:-clip.cool}"
READYZ_URL="${READYZ_URL:-http://127.0.0.1:8000/readyz}"
log() { echo "[drain] $*"; }
cf()  { curl -fsS --max-time 15 -H "Authorization: Bearer ${CF_TOKEN}" "$@"; }

# Can we ASK Cloudflare about the tunnel? Only ever gates verification, never the drain — see the
# header. Missing token ⇒ we drain blind (settle wait) rather than refuse to drain.
api_enabled() { [ -n "${CF_TOKEN:-}" ] && [ -n "${CF_ACCOUNT:-}" ] && [ -n "${CF_TUNNEL_NAME:-}" ]; }

tunnel_id() {
  cf "${API}/accounts/${CF_ACCOUNT}/cfd_tunnel?is_deleted=false" \
    | jq -er --arg n "${CF_TUNNEL_NAME}" '[.result[]|select(.name==$n)][0].id'
}

# How many tunnel connections are currently registered from an egress IP? Each box's cloudflared opens
# ~4; a connector that has gone away has 0. $1 = public IP.
conns_from() {
  local tid; tid=$(tunnel_id) || return 1
  cf "${API}/accounts/${CF_ACCOUNT}/cfd_tunnel/${tid}/connections" \
    | jq -e --arg ip "$1" '[.result[]?|(.conns//[])[]|select(.origin_ip==$ip)]|length'
}

# `cloudflared` = the APP tunnel's connector. Named explicitly so the chat connector in the same compose
# project (vent.dog) is never caught by a bare `stop`.
connector_stop() { docker compose -f "$CFD" stop -t "${CFD_STOP_TIMEOUT:-40}" cloudflared; }
connector_start() { docker compose -f "$CFD" start cloudflared; }

# Wait until the edge has actually dropped our connections. Bounded: a stuck deregistration must not
# hold a reboot open forever, and by this point the container is already down either way.
wait_deregistered() {
  local n=
  if ! api_enabled; then
    log "no tunnel API token — sleeping ${CFD_SETTLE:-15}s for the edge to drop our connections"
    sleep "${CFD_SETTLE:-15}"; return 0
  fi
  for _ in $(seq 1 20); do
    n=$(conns_from "${MY_PUBLIC_IP}" 2>/dev/null) || n=
    [ "${n:-}" = "0" ] && { log "connector deregistered — 0 connections from ${MY_PUBLIC_IP}"; return 0; }
    sleep 2
  done
  log "WARN: ${n:-?} connection(s) still registered from ${MY_PUBLIC_IP} after 40s — continuing"
}

# Wait for our connector to come BACK, so undrain doesn't report success on a connector that died.
wait_registered() {
  local n=
  api_enabled || { log "no tunnel API token — not verifying re-registration"; return 0; }
  for _ in $(seq 1 30); do
    n=$(conns_from "${MY_PUBLIC_IP}" 2>/dev/null) || n=
    [ "${n:-0}" -gt 0 ] 2>/dev/null && { log "connector re-registered — ${n} connection(s) from ${MY_PUBLIC_IP}"; return 0; }
    sleep 2
  done
  log "WARN: connector has NOT re-registered after 60s — this box is still taking no tunnel traffic"
  return 1
}

# Is the peer still serving HTTP? i.e. does its connector hold any tunnel connections? This is the check
# that stops us draining into a box that can't take over. Fails CLOSED when the API is reachable but
# says zero; falls through to the Patroni check when there's no token to ask with.
peer_tunnel_ready() {
  local n
  api_enabled || { log "no tunnel API token — cannot verify ${OTHER_ORIGIN}'s connector (relying on Patroni)"; return 0; }
  n=$(conns_from "${OTHER_PUBLIC_IP}") || { log "peer ${OTHER_ORIGIN}: tunnel API unreachable"; return 1; }
  [ "${n:-0}" -gt 0 ] || { log "peer ${OTHER_ORIGIN}: NO tunnel connections — draining this box would take the site DOWN"; return 1; }
}

# ALL etcd members must be healthy before we take this DB node down — otherwise we drop below quorum
# (3 members, majority 2; rebooting one while another (e.g. the witness) is already down = 1/3 = no
# leader = outage). This is the check that was missing the day the witness silently disappeared.
etcd_all_healthy() {
  local out total ok
  out=$(docker compose -f "$PGHA" exec -T etcd etcdctl --endpoints="$ETCD_ENDPOINT" \
        --command-timeout=5s endpoint health --cluster 2>&1) || true
  total=$(grep -cE 'is (healthy|unhealthy)' <<<"$out") || true
  ok=$(grep -c 'is healthy' <<<"$out") || true
  if [ "${total:-0}" -eq 0 ]; then
    # 0/0 is NOT "every member is down" — etcdctl produced no health lines at all, i.e. we never reached
    # etcd (wrong endpoint, container down). Say so, or it reads as a real quorum loss.
    log "etcd: could not query ${ETCD_ENDPOINT} — no health output. Wrong port, or etcd is down?"
    log "etcd: ${out}"
    return 1
  fi
  if [ "$total" -lt 3 ] || [ "${ok:-0}" -ne "$total" ]; then
    log "etcd: ${ok:-0}/${total} members healthy (need 3/3) — cluster has NO margin to lose a node"
    return 1
  fi
}

# Is the peer a live Patroni member? This is the signal that actually gates losing this node: if the peer
# isn't streaming, handing it the leader (or rebooting into it) is what causes the outage. `patronictl`
# failing leaves state empty (the pipeline succeeds through jq), which the "" case below catches — so
# this fails CLOSED.
peer_patroni_ready() {
  local state
  state=$(docker compose -f "$PGHA" exec -T patroni patronictl -c /etc/patroni/patroni.yml list -f json 2>/dev/null \
          | jq -r --arg m "${OTHER_PATRONI}" '.[]|select(.Member==$m)|.State')
  case "$state" in
    running|streaming) return 0 ;;
    "") log "peer ${OTHER_PATRONI}: not a Patroni member right now (or patronictl unreachable)"; return 1 ;;
    *)  log "peer ${OTHER_PATRONI}: state=${state} (need running/streaming)"; return 1 ;;
  esac
}

# Can the peer take over BOTH jobs — serving HTTP and running Postgres? Both must hold: a box whose
# connector is up but whose Patroni has stopped can serve pages and not writes, and vice versa.
other_ready() { peer_tunnel_ready && peer_patroni_ready; }

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

# Wait until HAProxy's write port (5000) has a healthy primary again — i.e. the leader handoff is done and
# writes work — before we let the reboot proceed.
wait_for_leader() {
  for _ in $(seq 1 20); do
    docker compose -f "$PGHA" exec -T patroni pg_isready -h 127.0.0.1 -p 5000 >/dev/null 2>&1 && return 0
    sleep 1
  done
  log "WARN: no writable leader on HAProxy :5000 after 20s (continuing anyway)"
}

prepare_for_reboot() {
  # 1. Leave the tunnel FIRST, so app traffic moves to the peer BEFORE the DB leader handoff — the brief
  #    no-writable-leader window then lands on the peer (which is becoming the leader), not here.
  if connector_stop; then
    log "DRAINED: cloudflared stopped — Cloudflare now routes the app tunnel to ${OTHER_ORIGIN}"
    wait_deregistered
  else
    log "WARN: could not stop the cloudflared connector — this box may still be taking traffic"
  fi
  # 2. Hand off the DB leader, then wait for the new leader to accept writes before rebooting.
  switchover_if_leader
  wait_for_leader
}

# ExecStop at shutdown. The reboot is ALREADY unstoppable here, so just do our best — NEVER abort
# (aborting would skip the drain entirely and cause exactly the outage we're avoiding; and at shutdown
# etcd/docker may be tearing down, so the quorum check is unreliable). Real gating lives in `check`.
drain() { prepare_for_reboot; }

# Can the cluster safely lose THIS node right now? (all 3 etcd members healthy + the peer up.)
reboot_safe() { etcd_all_healthy && other_ready; }

# Is the nightly pg_dump in flight? Kept SEPARATE from reboot_safe() — that answers "can the cluster
# survive losing this node", which is still true during a backup; this answers "would rebooting now
# destroy something". It matters because prepare_for_reboot hands off the Patroni leader, severing a dump
# that streams through HAProxy's write port, and `rclone rcat` then finalises the truncated stream into an
# object big enough to pass backup.sh's size floor. A silently corrupt backup is worse than a skipped
# reboot: the timer is daily with Persistent=true, so waiting a night is free.
# Measured 2026-07-29, before pg_ha_backup_hour moved to 02:00: the dump started at 04:00:00 and this
# timer fired at 04:00:58 — the two had been racing nightly with nothing between them.
backup_running() {
  [ -e "$BACKUP_LOCK" ] || return 1
  ! flock -n "$BACKUP_LOCK" true 2>/dev/null
}

# safe-reboot (manual): gate FIRST, then prep while healthy, then reboot. Refuses if unsafe.
safe_reboot() {
  reboot_safe || { log "REFUSING reboot — the cluster cannot safely lose this node right now (see above)"; exit 1; }
  # `if`, not `X && { … }` — under `set -e` the negative case of an && list is a footgun here.
  if backup_running; then
    log "REFUSING reboot — a pg_dump holds ${BACKUP_LOCK}; rebooting now would truncate it. Wait for it to finish, then retry."
    exit 1
  fi
  log "cluster fully healthy — handing off + draining, then rebooting"
  prepare_for_reboot
  systemctl reboot
}

# Guarded auto-reboot timer: only when a reboot is actually pending AND the cluster is fully healthy.
reboot_if_safe() {
  [ -f /run/reboot-required ] || { log "no reboot pending — nothing to do"; exit 0; }
  reboot_safe || { log "reboot pending but cluster not fully healthy — SKIPPING this window"; exit 0; }
  if backup_running; then
    log "reboot pending but a pg_dump holds ${BACKUP_LOCK} — SKIPPING this window (rebooting would truncate the backup); the timer retries tomorrow"
    exit 0
  fi
  log "reboot pending + cluster healthy — handing off + draining, then rebooting"
  prepare_for_reboot
  systemctl reboot
}

# Bring the box back: only start taking traffic once the LOCAL app is actually ready. Note the connector
# must be started explicitly — `restart: unless-stopped` does NOT bring back a container that was
# deliberately stopped, so after a drained reboot this is what re-arms the box.
#
# This is also the closest thing left to the health check the retired LB monitor performed: with
# connector-only failover nothing at the edge notices a box whose app is broken, so refusing to register
# while /readyz is bad is what keeps a broken box from serving 502s.
undrain() {
  local code=000
  for _ in $(seq 1 60); do                       # wait up to ~5 min for the local app to be ready
    code=$(curl -fsS -o /dev/null -w '%{http_code}' --max-time 5 -H "Host: ${READYZ_HOST}" "${READYZ_URL}" 2>/dev/null || echo 000)
    [ "$code" = "200" ] && break
    sleep 5
  done
  # Unhealthy app: registering the connector would hand users 502s from this box. Stay drained and let the
  # peer serve — UNLESS the peer isn't serving either, in which case a degraded box beats an unreachable
  # hostname and we take our chances.
  if [ "$code" != "200" ]; then
    if peer_tunnel_ready; then
      log "REFUSING to undrain: local /readyz=${code} and ${OTHER_ORIGIN} is serving — leaving this box drained"
      return 1
    fi
    log "WARN: local /readyz=${code} AND ${OTHER_ORIGIN} has no connector — starting anyway (last box standing)"
  fi
  connector_start || { log "WARN: could not start the cloudflared connector — this box is taking NO traffic"; return 1; }
  wait_registered || true
  log "UNDRAINED: connector running (local /readyz=${code})"
}

case "${1:-}" in
  drain)          drain ;;
  undrain)        undrain ;;
  safe-reboot)    safe_reboot ;;
  reboot-if-safe) reboot_if_safe ;;
  check)          reboot_safe && log "SAFE: 3/3 etcd healthy + peer up — ok to reboot this node" ;;
  *) echo "usage: $0 {drain|undrain|safe-reboot|reboot-if-safe|check}"; exit 2 ;;
esac
