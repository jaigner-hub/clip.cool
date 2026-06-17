#!/usr/bin/env bash
# Stage host-mounted SSH + age keys into root-owned copies with correct perms,
# so ssh/sops don't reject them for ownership (host files are uid 1000; container is root).
set -e

if [ -d /mnt/ssh ]; then
  mkdir -p /root/.ssh
  cp -rL /mnt/ssh/. /root/.ssh/ 2>/dev/null || true
  chmod -R go-rwx /root/.ssh 2>/dev/null || true
fi

if [ -f /mnt/age/keys.txt ]; then
  cp -L /mnt/age/keys.txt /root/age-keys.txt
  chmod 600 /root/age-keys.txt
  export SOPS_AGE_KEY_FILE="${SOPS_AGE_KEY_FILE:-/root/age-keys.txt}"
fi

exec "$@"
