# Migration from keygrip

clip.cool is a fresh app built on keygrip's infra scaffolding. It **replaces keygrip's web
footprint** on the `vent.dog` + `vent.dog2` pair while the **chat app (`chat.vent.dog`) keeps
running** on the shared platform. This records what was ported, what was renamed, and what's left.

## Two-layer rename model

The infra splits in two. The chat app is welded to the lower layer, so the rename is **surgical**:

- **Shared platform — NOT renamed** (chat + shared services depend on it): the Keycloak **`keygrip`
  realm** (the `vent-web` client lives inside it), the **`keygrip-pgha`** Patroni cluster + `/keygrip/`
  etcd namespace, the **`keygrip-edge`** Docker network, tailscale hosts **`vent-keygrip` /
  `vent-keygrip2`**, `/opt/keygrip/*` install paths, the observability stack, cloudflared tunnels,
  the `keygrip` LB pool, `keygrip-safe-reboot` units, the `keygrip-ansible`/`keygrip-patroni` images.
  Renaming any of these would break chat and buys nothing now — defer to a dedicated platform-rebrand.
- **App / web tier — renamed `keygrip → clip`** (this is keygrip's footprint we're replacing).

## Brought over (deploy-relevant support files)

| File | Notes |
|---|---|
| `.sops.yaml` | Reuses the **dev age recipient** as-is (key already on the boxes). Needed to decrypt the copied `secrets.sops.yml`. |
| `bin/secrets` | SOPS/age wrapper for group_vars. |
| `bin/whereami` | Per-prompt orientation; wired via `.claude/settings.json` UserPromptSubmit hook. Still carries keygrip refs (cosmetic). |
| `.gitleaks.toml` | Allowlists SOPS ciphertext for the secrets scan. |
| `.claude/settings.json` | The `whereami` hook. |

Already present (copied verbatim before this pass): `ansible/`, `app/`, `.gitignore`, and
`ansible/group_vars/dev/secrets.sops.yml` (keygrip's dev secrets, encrypted to the dev age key —
reused; fresh app secrets to be set via `bin/secrets`).

## Renamed (app-tier `keygrip → clip`)

- **Role** `roles/keygrip_web` → `roles/clip_web`; **playbook** `keygrip-web.yml` → `clip-web.yml`.
- **Compose project** `keygrip-web` → `clip-web`; **OTEL** `service.namespace=clip`,
  services `clip-web` / `clip-worker`.
- **App DB / role** `keygrip` → `clip` (`postgres_ha` defaults + `clip_web`; consumed everywhere via
  `app_db_name`/`app_db_user`, so PgBouncer alias, userlist, init SQL, backups all follow).
- **OIDC clients** in the `keygrip` realm: `keygrip-web`→`clip-web`, `keygrip-kc-admin`→`clip-kc-admin`,
  `keygrip-api-docs`→`clip-api-docs` (+ the `service-account-clip-kc-admin` user). Redirect URIs now
  point at `https://clip.cool/*` (the `keygrip_web_redirect_uris` var is now `clip_web_redirect_uris`).
- **Stash agent**: prefix `kg/web`→`clip/web`, runtime dir `/run/keygrip`→`/run/clip`.
- **Observability (functional)**: Loki break-glass alert + Grafana trace→logs query re-keyed from
  `stack="keygrip-web"` to `stack="clip-web"` (the compose-project label moved with the rename).

## Deliberately kept (avoid churn / shared)

- **Secret variable names** `vault_keygrip_web_client_secret` and the `KEYGRIP_WEB_CLIENT_SECRET`
  passthrough env — renaming would force re-editing the encrypted SOPS file. Both sides reference the
  same vault var, so the secret still matches. Rename in a deliberate secrets pass.
- **`/opt/keygrip` install root** — shared platform path (chat's dendrite/vent/livekit live under it).
- **Django package `keygrip`** (`DJANGO_SETTINGS_MODULE: keygrip.settings.prod`) — the app is being
  gutted/rewritten; the package renames with that rewrite, not now.
- **Realm-level branding** (`displayName`, `loginTheme: keygrip`, SMTP from-display) — the login page
  is shared with chat; rebrand with the platform, not the web tier.

## Remaining (not done yet)

1. **Cloudflare apex cutover — `clip.cool`.** The `cloudflare` role is single-zone on `vent.dog`
   (the app was `app.vent.dog`, a subdomain; the `id.vent.dog` LB must stay in that zone). Serving
   the app at the **apex of the `clip.cool` zone** needs: the `clip.cool` zone in the CF account, a
   tunnel-ingress + Load-Balancer entry for `clip.cool` (apex proxied-CNAME / flattening), and a CF
   Access app. `app.vent.dog` still appears in `cloudflare`, `observability` (uptime check), and
   `drain.sh` (health-probe `Host:` header) — those move with this cutover. **Until then the app has
   no working public origin** (OIDC redirects already target `clip.cool`).
2. **New clip components** (architecture.md): R2 bucket + creds, ffmpeg transcode worker tier,
   Meilisearch/Typesense — none are in Ansible yet.
3. **App rewrite** — `app/` is still keygrip's Django CMS. Gut to the clip.cool media app.
4. **Cosmetic doc refs** to `keygrip_web` remain in comments (`stash_agent` tasks, `postgres_ha`
   README, a few playbook/inventory comments, prometheus.yml comments, observability alert-group
   names). Non-blocking.
5. **CI / docs** — `.github/` (CI, dependabot), `.githooks/`, a fresh `CLAUDE.md`/`README.md`/ADRs
   were intentionally left for a follow-up pass.
