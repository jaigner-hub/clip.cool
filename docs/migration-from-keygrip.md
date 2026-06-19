# Migration from keygrip

clip.cool is a fresh app built on keygrip's infra scaffolding. It **replaces keygrip's web
footprint** on the `vent.dog` + `vent.dog2` pair while the **chat app (`chat.vent.dog`) keeps
running** on the shared platform. This records what was ported, what was renamed, and what's left.

> **Status:** the media app has begun. The app serves on **`app.vent.dog`** (interim; `clip.cool`
> apex pending DNS), R2 + Typesense are wired, and a first image-ingest→OCR→search slice ships as
> the `clips` app. The CMS (`recommendations`, `tenancy`) has been removed. See **Landed** below.

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
  `keygrip-api-docs`→`clip-api-docs` (+ the `service-account-clip-kc-admin` user). Redirect URIs point
  at `https://app.vent.dog/*` (interim host — see below; the `keygrip_web_redirect_uris` var is now
  `clip_web_redirect_uris`).
- **Stash agent**: prefix `kg/web`→`clip/web`, runtime dir `/run/keygrip`→`/run/clip`. (The
  `clip_web` tasks + the `vent.dog2` inventory overlay still hardcoded the old `/run/keygrip` /
  `kg/web` — fixed to `/run/clip` / `clip/web`. ⚠️ The stash CLUSTER must hold the app secrets under
  `clip/web/*` for the agent to render them.)
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

## Landed (image-ingest + search pass)

Decision: serve on **`app.vent.dog`** now (already fully wired in the `cloudflare` role — LB across
both boxes, tunnel ingress on both tunnels, `/readyz` monitor, public `/metrics` 404) rather than
wait on the `clip.cool` apex DNS. This unblocks the whole edge end-to-end; the apex is a later flip.

- **App on `app.vent.dog`** — `clip_web` `app_hostname` + the `clip-web`/`api-docs` realm redirect
  URIs realigned from `clip.cool` back to `app.vent.dog`. **No `cloudflare`/`cloudflared`/`drain`/
  `observability` changes** (they already use `app.vent.dog`).
- **Cloudflare R2** — wired into the app (`clips/storage.py`, boto3, presigned direct-to-bucket
  upload + public/presigned delivery). Config is **all in stash** under `clip/web/*`:
  `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_S3_API_ENDPOINT`, `R2_BUCKET_NAME`, `R2_API_TOKEN`,
  and optional `R2_PUBLIC_BASE` (the `pub-….r2.dev`/CDN URL; empty ⇒ short-lived presigned GETs).
  compose `x-app-env` passes them through to web + worker. Bucket creation + CORS (allow `PUT` from
  `https://app.vent.dog`) is a one-time Cloudflare step (account-level — not blocked by the apex).
- **Typesense** — new `roles/typesense` + `playbooks/typesense.yml` (single instance on `primary`;
  on `keygrip-edge` and published on the box tailnet IP so `vent.dog2` reaches it cross-box, like
  pg-exporter). Server key `vault_typesense_api_key` (SOPS); the **same value** is in stash as
  `clip/web/TYPESENSE_API_KEY` for the app. `clip_web` gains `search_host/_port/_protocol` defaults
  (`vent.dog2` overrides `search_host` to vent.dog's tailnet IP in inventory).
- **`clips` Django app** — the first real media slice: `Asset` model; presigned upload → finalize →
  Procrastinate `process_asset` (one download → Pillow dimensions + sha256 + WebP poster + **Tesseract
  OCR**) → index into Typesense; a session `/clips/upload/` page and a working `/clips/search/`
  (queries Typesense, renders hits). JSON API mirror at `/api/v1/clips/*`. Worker queues are now
  `default,index,thumbs`. `tesseract-ocr` added to the image; `boto3`/`typesense`/`Pillow`/
  `pytesseract` to requirements.
- **CMS gutted** — `recommendations` and `tenancy` (Organizations › Projects) apps removed entirely,
  plus their `web` seams: the home page is now a redirect to `/clips/search/`, the API-credentials UI
  and machine-token (client-credentials) API auth are gone (**API auth is user-token-only** now), and
  `web/auth.py` no longer auto-provisions a personal org. The Django package stays named `keygrip`.

## Also landed since

- **clip.cool apex is live.** The `cloudflare` role went multi-zone: `clip.cool` added to both
  tunnels' ingress + a 3rd Load Balancer "clip.cool" in the clip.cool zone on the **same shared
  `keygrip` pool** (HA across both boxes). `clip_web` `app_hostname=clip.cool` with `app.vent.dog`
  kept as a transitional alt host; `clip-web`/`api-docs` realm redirect URIs gained `clip.cool`.
  Verified: clip.cool /readyz ok, public share + GIF links work, id.vent.dog/chat untouched.
- **Video pipeline (Phase 2a/2b):** GIF/video → ffmpeg AV1/VP9/H.264 + poster + optimized GIF
  (transcode queue, in the shared worker); `<video>` served AV1→VP9→H.264; overlay captioning
  (editable layers + transparent text PNG, re-editable). See `docs/phase2-video-captioning.md`.
- **Canonical root URLs:** a clip is `clip.cool/<id>` (one page: humans + OG/Twitter unfurl),
  `clip.cool/<id>.gif`, `clip.cool/<id>.mp4`; old `/c/<id>*` + `/clips/asset/<id>/` 301 to it. Search
  IS the root (`/`); `/clips/search/` 301s there. Public **Browse** grid at `/clips/browse/`.
- **Keycloak issuer migrated `id.vent.dog → id.clip.cool`.** `KC_HOSTNAME` flipped (keycloak.yml now
  `serial: 1`, rolling); the `cloudflare` role added `id.clip.cool` ingress (both tunnels) + a Load
  Balancer in the clip.cool zone on the shared pool + an `id.clip.cool/admin` Access app. Issuer
  repointed across **clip_web, vent_app (chat), Grafana, GlitchTip** + the blackbox probe. The
  `keygrip` realm + all users are unchanged (only the hostname moved); everyone re-logs in once.
- **Old `*.vent.dog` web hosts retired.** `app.vent.dog` + `id.vent.dog` fully removed: out of
  `clip_web` alt-hosts (`app_alt_hostnames: []`), realm redirect URIs, `cf_ingress`/`cf_ingress2`,
  `cf_lb_hostnames` (now `[]`); the LB health monitor + `drain.sh` undrain + blackbox probe moved to
  `clip.cool`; the `id.vent.dog/admin` Access app + both vent.dog-zone Load Balancers deleted via API
  (the `cloudflare` role only creates, never deletes). Both hosts now NXDOMAIN. `chat.vent.dog`,
  `livekit.vent.dog`, and the `vent.dog` marketing site are untouched. (Google's old
  `id.vent.dog/.../broker/google/endpoint` redirect URI can be removed from the Google OAuth client
  whenever — harmless to leave.)

## Also landed (recorder + transcode/deploy hardening)

- **In-browser tab recorder** (`/clips/record/`, nav "Record") — clip any tab you can watch
  (getDisplayMedia, no plugin), drag-crop + trim + caption, uploads via the existing
  presign→R2→finalize path. Crop/trim are selected client-side and **baked server-side** at transcode
  (`Asset.crop` JSON fractions, `Asset.trim_start/_end` seconds → ffmpeg `crop=` + `-ss`/`-t`). The
  web sibling of the planned desktop Snipper. Full writeup: **`docs/browser-recorder.md`**.
- **Codec ladder simplified to H.264 only.** Dropped VP9 (libvpx, very slow) + AV1 (slow): their only
  win is compression, moot on R2 (zero egress) for short clips, and neither is universal so H.264 is
  required regardless. Transcodes went from slow → near-instant; storage ~⅓. Old clips keep their
  existing av1/vp9 renditions. Re-add AV1 only on AV1-capable HW (the NAS GPU — Intel UHD 630 — does
  H.264/HEVC only, so it can't accelerate the codecs that were slow).
- **Renditions downscale to ≤1280px** (`RENDITION_MAX_W`) + recorder caps capture to ≤1080p — a 2K/4K
  tab no longer times out the encode or bloats uploads.
- **GIF quality**: per-frame palettes (`stats_mode=single` + `paletteuse new=1`) at 640px, `gifsicle
  -O3` lossless (lossy removed). Captions now burn into the **GIF and the download**, not just the
  on-platform overlay.
- **Detail page** shows a "transcoding…" placeholder and **polls a JSON status endpoint** instead of
  meta-refreshing (no more restarting the playing clip while a caption bakes). Grid cards show a
  placeholder for clips without a poster yet.
- **Self-healing transcodes**: periodic `reap_stuck_assets` re-queues jobs orphaned by a dead worker,
  via Procrastinate **worker heartbeats** (long live encodes are never falsely reaped), bounded by
  `Asset.transcode_attempts`. ffmpeg *failures* already mark the asset FAILED, so they aren't retried.
- **Deploys decoupled + fast (~1 min).** `clip_web` now: drain → `docker compose build` (ALL services)
  → recreate `webapp` only (`--no-deps`) → migrate → undrain → recreate `worker`+`worker-transcode`.
  Worker stop-grace cut 300s→30s (reaper covers interruptions). A web-only deploy never disturbs an
  in-flight encode. ⚠️ The build-all step is required — each service has its own `build:`/image, so a
  per-service `--build` leaves the workers on stale code (a bug we hit: workers silently ran old code).
- Migrations `0009`–`0012`: `Asset.crop`, `caption_burning`, `trim_start/_end`, `transcode_attempts`.

## Remaining (not done yet)

1. **HSTS ramp** on the clip.cool zone (currently `max_age: 300`; raise once verified, then
   `include_subdomains`, then `preload` last).
2. **Video tail:** captioned grid posters, perceptual `pHash` dedup, prune originals. (Codec ladder
   simplified to H.264; on-demand caption burn-in for download + GIF is done; a GPU/AV1 tier is only
   worth it on AV1-capable HW if bandwidth ever becomes the driver.)
3. **Native Snipper (2c)** — `clip-snipper` device-flow client + desktop push (the in-browser
   recorder already covers the web case; native is for higher-fidelity source capture).
4. **Cosmetic doc refs** to `keygrip_web` remain in comments (`postgres_ha` README, a few
   playbook/inventory comments, prometheus.yml, observability alert-group names). Non-blocking.
5. **CI / docs** — `.github/` (CI, dependabot), `.githooks/`, ADRs — a follow-up pass.
