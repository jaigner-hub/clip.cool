# CLAUDE.md

**clip.cool** is a GIF/meme hosting platform — built to beat Giphy / Tenor / Imgur on speed and
cost. The core bet: **never serve a GIF as a GIF.** Ingest anything, transcode to looping video
(AV1 / VP9 / H.264), serve `<video autoplay loop muted playsinline>`. Perceptual dedup and
search-by-text (OCR + transcription) are the differentiators on top.

This is a **fresh app built on the infrastructure scaffolding developed for `keygrip`**
(`/home/enum/Projects/keygrip`). It **replaces keygrip's web footprint** on the dev pair
(**vent.dog + vent.dog2**, IONOS, on the tailnet), while the existing **chat app at `chat.vent.dog`**
(Matrix/dendrite + LiveKit + the Go `vent` server) **keeps running beside it** on the shared
platform.

> **Status: media app under way.** Ansible infra + deploy tooling are in place, the app-tier is
> renamed `keygrip → clip`, and the first product slice ships: the **`clips`** app (image ingest →
> R2 → transcode → OCR/vision → Typesense search; in-app meme builder; video + captioning).
> The keygrip CMS (`recommendations`, `tenancy`) has been **removed**; the Django package stays
> named `keygrip` for now. The app serves on the **`clip.cool` apex** (HA via a CF Load Balancer;
> `app.vent.dog` still dual-served during the transition). R2 + Typesense are wired.
> **Trust [`docs/migration-from-keygrip.md`](./docs/migration-from-keygrip.md) for current state.**
>
> - What clip is + the media pipeline: [`docs/architecture.md`](./docs/architecture.md)
> - What's ported / renamed / remaining: [`docs/migration-from-keygrip.md`](./docs/migration-from-keygrip.md)
> - The reference platform (decisions, ADRs, the chat app it shares infra with):
>   `/home/enum/Projects/keygrip` (its `CLAUDE.md` + `docs/adr/`).

## The one rule

**Never serve a GIF as a GIF.** GIF is a 1987 codec — a 5 MB GIF is often a ~400 KB MP4 at better
quality. The pipeline is: accept anything (`.gif`/`.mov`/`.mp4`/`.webm`) → normalize → emit modern
renditions (AV1 → VP9 → H.264, + poster + scrub sprite) → serve `<video>`, never `<img>`. See
`docs/architecture.md`.

## ⚠️ The two-layer rename model — read before touching `ansible/`

clip and the chat app share **one platform** on the vent.dog pair. The rename from keygrip was
**surgical**, and a blind find/replace of `keygrip → clip` **will break the chat app**:

- **Shared platform — keep the `keygrip` names** (chat + shared services depend on them): the
  Keycloak **`keygrip` realm** (the `vent-web` chat client lives inside it), the **`keygrip-pgha`**
  Patroni cluster + `/keygrip/` etcd namespace, the **`keygrip-edge`** Docker network, tailscale
  hosts **`vent-keygrip` / `vent-keygrip2`**, **`/opt/keygrip/*`** install paths, the observability
  stack, cloudflared tunnels, the `keygrip` LB pool, the `keygrip-safe-reboot` units, the
  `keygrip-ansible` / `keygrip-patroni` images, and realm login branding.
- **App / web tier — renamed to `clip`** (keygrip's footprint we replaced): the **`clip_web`** role +
  **`clip-web.yml`** playbook, the **`clip`** DB/role, the **`clip-web` / `clip-kc-admin` /
  `clip-api-docs`** OIDC clients (in the shared realm), the **`clip/web`** stash prefix + `/run/clip`
  runtime dir, the OTEL namespace/services and the Loki `stack="clip-web"` label.

When in doubt, check whether the chat app (`vent_app` / `dendrite` / `livekit` roles, `vent.yml`)
touches the thing. If yes, it's shared — leave it.

## Architecture — decisions

Inherited from keygrip unless flagged. Rationale for inherited decisions lives in keygrip's
`docs/adr/`.

| Area | Decision |
|---|---|
| **Architecture** | **API-first** — a service/business-logic core; on top a **Django Ninja** JSON API at `/api/v1` + thin HTML-fragment views. Logic never lives in views. |
| **Web serving** | **Gunicorn + Uvicorn (ASGI)** behind **cloudflared** straight to the app port; no local Nginx/Caddy (ADR 0004). |
| **Auth** | **Keycloak**, OIDC. clip uses `clip-*` clients in the **existing `keygrip` realm** (shared with chat). |
| **Object storage / delivery** | ⚠️ **DEVIATES from keygrip.** **Cloudflare R2** (S3-compatible, **zero egress**) + Cloudflare CDN — egress is the cost that kills a video host; R2 removes it. (Cloudflare Stream rejected: per-minute pricing balloons at meme volume.) |
| **Database** | **Postgres** — the shared self-hosted **HA cluster** (Patroni + etcd, ADR 0016). The `clip` DB/role replaces `keygrip`. |
| **Async / transcode** | ⚠️ **Adapted.** No Redis broker on this platform — use **Procrastinate** (Postgres-backed, ADR 0008), **not** Celery. Queues: `transcode` (ffmpeg, heavy) / `thumbs` / `index` (search + dedup + OCR). Heavy AV1 encodes likely a dedicated worker tier. |
| **Search** | **Meilisearch** or **Typesense** (self-hosted, typo-tolerant). Postgres is source of truth; the engine is a rebuildable index. |
| **Edge / HA** | **Cloudflare Tunnel** (no public origin ports) + **Load Balancer** across both boxes. |
| **Infra / config** | **Ansible** + **SOPS/age** + Docker; run via `ansible/ac` (ADR 0006/0001/0007). |
| **Secrets** | **SOPS**-encrypted (age) in git, decrypted to **tmpfs** at deploy. Never commit a plaintext secret. Edit via `bin/secrets`. The dev age key is reused from keygrip. |
| **Observability** | Self-hosted **Grafana + Prometheus + Loki + Tempo**. clip-web/clip-worker traces; transcode throughput/latency is a key SLO. |

## Infrastructure

- **Dev pair (IONOS, tailnet):** **vent.dog** (`100.106.141.112`) + **vent.dog2** (`100.110.200.36`),
  Ubuntu 24.04, the HA pair behind the Cloudflare LB. clip-web deploys to both; single-instance
  services (Keycloak, observability, the chat app) live on vent.dog only. Inventory:
  `ansible/inventory.yml` — it **only** targets these boxes.
- **Deploy tooling — `./ac`:** all Ansible runs go through `ansible/ac`, a wrapper that runs
  `ansible-core` + collections + `sops` inside a pinned control container (ADR 0007). Always from
  `ansible/`: `./ac ansible-playbook playbooks/<name>.yml` (e.g. `clip-web`, `postgres-ha`,
  `keycloak-realm`, `observability`). Never invoke a host `ansible-playbook` directly.
- **Secrets — `bin/secrets`:** the SOPS/age wrapper (`view`/`get`/`set`/`edit`/`validate`). Use it
  instead of raw `sops`. New secret ⇒ add to the SOPS store; both `clip_web` and the realm read
  shared `vault_*` names (some still keygrip-named — see migration doc).
- **Orientation — `bin/whereami`:** prints which environment a shell is in. Wired to every prompt via
  the `UserPromptSubmit` hook in `.claude/settings.json` — trust its `[whereami]` line over
  assumptions.

## Conventions (carried from keygrip)

- **Superuser-first** in every permission function.
- **Service layer holds the logic.** Views (HTML or JSON) are thin adapters.
- **Secrets from day one** — no secret in code, committed files, or the image. SOPS → tmpfs.
- **CSP from the start** — inline `<script>`/`<style>` carry `nonce="{{ CSP_NONCE }}"`; use Alpine's
  CSP build.
- **Migration safety** — expand/contract; never ship a breaking schema change with the code needing
  it, so a quick-revert never lands on an incompatible schema.
- **Log-level discipline** — `warning()` for transient/expected, `error()` only for genuine bugs.
- **Tests encode WHY** the behavior matters, not just what it does.

## How to work

- **Plan first** for non-trivial work (3+ steps / architectural calls). State assumptions, ask
  rather than guess, push back when a simpler path exists.
- **Simplicity first** — build the minimum that meets the goal; greenfield is not a license to
  over-engineer.
- **Surgical changes**, match conventions, verify before claiming done (run it, show it).
- **Mind the shared platform** (see the rename model above) — the chat app must keep working.

## Not in this repo yet (don't reference as if present)

These exist in keygrip but were **intentionally not ported**: `marketing/`, `portal/`, `mc` (local
worktree tool), `ROADMAP.md`, `docs/adr/` (consult keygrip's), `.github/` CI (`ci.yml`, Dependabot),
`.githooks/`, `docs/style-guide.md`. Bringing CI over is a planned follow-up. Until then there is **no
automated test/secret-scan gate** — be careful committing.

## Video / transcode — current shape (changed materially; read before touching `clips/transcode.py`)

- **H.264 only.** VP9 + AV1 were **dropped** — they were the transcode bottleneck (libvpx-vp9 very
  slow; libsvtav1 slow) and their only benefit is compression, which is moot on R2 (zero egress) for
  short ≤1280 clips, and neither is universal so H.264 is required anyway. Re-add AV1 in `_RENDITIONS`
  only if bandwidth/storage becomes the driver — and on **AV1-capable HW** (the homelab NAS GPU is an
  Intel **UHD 630**: H.264/HEVC encode only, *no* AV1/VP9 — so it can't accelerate our slow codecs).
- **Renditions downscale to ≤1280px** (`RENDITION_MAX_W`) so a 2K/4K capture can't time out the encode.
- **GIF**: per-frame palettes (`palettegen stats_mode=single` → `paletteuse new=1`), 20fps, 640px,
  `gifsicle -O2` **lossless** (no `--lossy` — it visibly degraded; `GIF_LOSSY=0`). GIF is the chat
  fallback (Discord/Signal only autoplay real GIFs); the `<video>`/`.mp4` is the quality path. Long
  clips make big/slow GIFs (it's the format) — trim them. Caption burn-in uses `libx264 -preset
  veryfast` (download-only artifact); on-platform captioning is a live CSS overlay (instant, no encode).
- **In-browser tab recorder** (`/clips/record/`) is live — crop + trim + caption a clip from any tab,
  no plugin. Crop/trim are selected client-side but **baked server-side** at transcode (`Asset.crop`,
  `trim_start/_end`). See [`docs/browser-recorder.md`](./docs/browser-recorder.md).
- **Captions burn into the download AND the GIF** (`burn_caption_asset`); detail page polls a JSON
  status endpoint (no full-page meta-refresh).
- **Self-healing**: a periodic `reap_stuck_assets` task re-queues jobs orphaned by a dead worker,
  detected via Procrastinate **worker heartbeats** (so a long *live* encode is never falsely reaped),
  bounded by `Asset.transcode_attempts`.
- **Deploys are decoupled** (see below) — recreate the webapp first (fast, LB-drained), workers
  separately; `clip-web` deploys in ~1 min, never blocked on an encode.

## Deploys (`./rolling-deploy.sh` is legacy — use the playbook)

`./ac ansible-playbook playbooks/clip-web.yml` is the canonical deploy (serial:1, per box: drain →
**`docker compose build` ALL services** → recreate `webapp` only (`--no-deps`) → migrate → undrain →
recreate `worker`+`worker-transcode`). The build-all step is load-bearing: each service has its own
`build:`/image, so a per-service `--build` would leave the workers on stale code. Worker stop-grace is
30s (the reaper covers any interrupted encode). A web-only deploy never disturbs an in-flight encode.

## Remaining work (from `docs/migration-from-keygrip.md`)

1. **HSTS ramp** on the clip.cool zone (`app.vent.dog`/`id.vent.dog` are already fully retired).
2. **Video pipeline tail** — perceptual `pHash` dedup, prune originals, captioned grid posters.
   (Codec ladder simplified to H.264; on-demand caption burn-in for download **and** GIF is done.)
3. **Native Snipper (2c)** — `clip-snipper` device-flow client + desktop push. The **in-browser
   recorder** already covers the web case; native Snipper is for higher-fidelity source capture.
4. **App rewrite** — `clips` media app shipped, CMS removed. Django package still `keygrip`; full
   rename is a later pass.
5. **CI / docs** — `.github/`, `.githooks/`, ADRs.
