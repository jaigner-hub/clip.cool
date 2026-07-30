# postgres_ha — self-hosted HA Postgres for dev (ADR 0016)

Patroni + etcd across the **vent.dog pair** (`vent.dog` + `vent.dog2`, both IONOS) with a **vote-only
NixOS witness** (`monitor`) over Tailscale. Async streaming replication, automatic failover, HAProxy
leader routing, PgBouncer (transaction mode) for the web tier, nightly `pg_dump` → Backblaze B2.

> **Status: LIVE on the vent.dog pair — bootstrapped + failover-verified 2026-06-11.** The first
> bring-up surfaced and fixed: the `.env` filename, the entrypoint chowning a read-only mount,
> PgBouncer's userlist uid (the edoburu image runs as uid 70), and `\gexec` not working in `psql -c`.
> Automatic failover drilled clean: leader killed → replica promoted in ~20–30s (lock TTL) →
> writes recovered via both HAProxy and PgBouncer → old leader rejoined as a streaming replica.

## What runs where (per data node, all host-networked)

| Service | Port(s) | Bound on | Purpose |
|---|---|---|---|
| etcd | 2379/2380 | tailnet IP (+lo client) | DCS (3 members: 2 data + `monitor`) |
| Patroni/Postgres | 5432 (PG), 8008 (REST) | tailnet IP + lo | the DB + failover agent |
| HAProxy | 5000 write, 5001 read, 7000 stats | 0.0.0.0 (5000/5001), lo (7000) | routes to the **current leader** via Patroni `/primary` |
| PgBouncer | 6432 | 0.0.0.0 | **transaction-mode** pool for the WEB tier |

The **cluster plane** (etcd/PG/Patroni) is confined to the **tailnet IP**. The **app-facing proxies**
(5000/6432) bind `0.0.0.0` so app containers reach them via host-gateway — **so a host firewall is
required** (below).

## Prerequisites (do these first)

1. **Tailnet:** both boxes + `monitor` are `tag:keygrip` (`playbooks/tailscale.yml` handles the boxes;
   tag `monitor` in its `nixos-config`). The role addresses members by **tailnet IP** (MagicDNS isn't
   resolved — `accept-dns=false`).
2. **Tailnet ACL:** allow `tag:keygrip ⇄ tag:keygrip` on **2379, 2380, 5432, 8008**.
3. **Witness:** bring up etcd on `monitor` from `files/monitor-etcd.nix.example` (cluster token +
   member list + timeouts must match `defaults/main.yml`).
4. **New SOPS secrets** (`group_vars/dev/secrets.sops.yml`) — add before deploying:
   - `vault_pg_superuser_password`, `vault_pg_replication_password`, `vault_patroni_restapi_password`
   - `vault_b2_account_id`, `vault_b2_app_key` (Backblaze B2 application key for backups)
   - `vault_app_db_password` already exists (shared with `keygrip_web`).
5. **Host firewall (ufw):** deny `5000/5001/6432` (and `2379/2380/5432/8008`) on the **public IONOS
   interface**; allow on `tailscale0` and the Docker bridge(s). Not auto-applied by the role (ufw ↔
   Docker interactions are box-specific) — do it explicitly and verify.

## Bootstrap

```
# ⚠️ rolling-deploy.sh is the RIGHT path but is BROKEN on this tier as of 2026-07-30 — see below.
./ac ansible-playbook playbooks/postgres-ha.yml -l vent.dog2   # replica first
./ac ansible-playbook playbooks/postgres-ha.yml -l vent.dog
```

Patroni coordinates bootstrap through etcd: whichever node wins the DCS leader key runs `initdb`; the
other clones it as a replica (`pg_rewind`). The two data-node etcd members are a 2/3 majority on their
own, so **first bootstrap does not require the witness** — but bring the witness up anyway so failover
is safe afterward.

### ⚠️ Never run this against BOTH boxes in one invocation

This README documented a bare, unlimited `./ac ansible-playbook playbooks/postgres-ha.yml` for the
role's whole life, and that is the unsafe path. The play has no `serial:`, so one invocation
recreates etcd on **both** data nodes inside the same task: 3 members become 1, there is no
majority, Patroni self-fences, and Postgres goes read-only for clip **and** chat.

`rolling-deploy.sh` exists to serialize exactly this (`--limit <host>`, one box at a time, with a
Patroni leader handoff first) and is the path this file should recommend. **It does not work on this
tier today:** its drain calls the Cloudflare *Load Balancer* pool API, this tier is tunnel-only
(`cloudflared`), and the call returns 401 — so `drain.sh check` fails and rolling-deploy aborts
before deploying anything. keygrip fixed the same drift by rewriting drain around the tunnel
connector (its #159–#161); that has not been ported here. Note the knock-on: `reboot_safe()` calls
the same failing check, so the guarded auto-reboot timer refuses every window once a reboot is
actually pending — kernel updates never land that way.

Until drain is fixed, roll it by hand, follower first, checking `endpoint health` in between:

```
./ac ansible-playbook playbooks/postgres-ha.yml -l vent.dog2   # the replica first
./ac ansible-playbook playbooks/postgres-ha.yml -l vent.dog
```

The play's `pre_tasks` refuse to start when etcd is *already* short a voter (usually the homelab
witness), which is the other half of the same hazard — override with `-e confirm_degraded_etcd=yes`
only if you accept a read-only window.

**After any run, check where etcd's raft leadership landed.** Recreating a member can leave the
witness as raft leader, which puts every DCS write across a WAN hop (measured: ~104 ms proposals vs
~5 ms). It does **not** stop auto-compaction, as an earlier version of this file claimed: only the
leader *schedules* a compaction, but it does so as a raft proposal that every member then applies —
observed 2026-07-30, all three members logging `finished scheduled compaction` at the same revision
(3826161) and the same hash. The witness still needs the settings in its own config for the case
where it *is* leader, which is exactly why leadership there is a latency problem and not a
correctness one:

```
docker exec keygrip-pgha-etcd-1 etcdctl \
  --endpoints=100.106.141.112:2379,100.110.200.36:2379,100.109.229.12:2379 -w table endpoint status
# if the witness (100.109.229.12) is IS LEADER, move it back:
docker exec keygrip-pgha-etcd-1 etcdctl --endpoints=100.109.229.12:2379 move-leader <data-node-id>
```

Note that `endpoint status --cluster` reports only members that advertise a client URL, so it can
hide a member entirely — pass `--endpoints` explicitly as above.

## Verify (the whole point — do these)

```
# Cluster topology: exactly one Leader, one Replica, both running
docker compose -f /opt/keygrip-pgha/compose.yml exec patroni patronictl -c /etc/patroni/patroni.yml list

# HAProxy is pointing the write port at the leader
psql "host=<box-tailnet-ip> port=5000 user=keygrip dbname=keygrip" -c "select pg_is_in_recovery();"  # => f

# etcd has 3 healthy members
docker compose -f /opt/keygrip-pgha/compose.yml exec etcd etcdctl member list
```

### Failover drills (prove it actually works)
- **Kill the leader** (`docker compose stop patroni` on the leader box) → within ~`ttl` seconds the
  replica promotes; HAProxy's write port follows; the old node rejoins as a replica on restart.
- **Reboot the witness** (`monitor`) while both boxes are healthy → **no impact** (2/3 quorum holds).
- **Witness down + one box down** → cluster goes read-only until one returns (expected; ADR 0016).

## App cutover (DEFERRED — not done by this role)

This role stands up the cluster **alongside** the existing single-node `appdb`; it does **not** touch
`keygrip_web` or migrate data. When ready, cut `keygrip_web` over in a separate change:
- **web** → PgBouncer `6432` (transaction mode); **worker** → HAProxy `5000` (session — LISTEN/NOTIFY
  must NOT go through PgBouncer; ADR 0008).
- App containers reach the host proxies via `host.docker.internal` (`extra_hosts: host-gateway`).
- Migrate data (dump/restore from the old `appdb`), then retire `appdb`.

## Watch-outs
- Keep the worker on a **session** path; the transaction pooler silently breaks `LISTEN/NOTIFY`.
- etcd must stay **3 members** with the **relaxed timeouts**; never run 2-member etcd.
- The witness is **vote-only** — never give it data or make it promotable.
- **Replication is not backup** — the nightly `pg_dump` → B2 is the real recovery floor.
