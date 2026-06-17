#!/usr/bin/env bash
# Patroni container entrypoint (ADR 0016). The PG data dir lives on a named volume that mounts
# root-owned on first use; Patroni runs Postgres as the 'postgres' user, so hand it ownership,
# then drop privileges with gosu (shipped in the official postgres image).
set -euo pipefail

# Only the data volume needs chowning. /etc/patroni/patroni.yml is mounted read-only (and is
# world-readable) — chowning it fails on the ro filesystem, so leave it alone.
mkdir -p /var/lib/postgresql/data/pgroot
chown -R postgres:postgres /var/lib/postgresql/data
chmod 700 /var/lib/postgresql/data/pgroot

exec gosu postgres patroni /etc/patroni/patroni.yml
