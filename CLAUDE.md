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
> R2 → OCR → Typesense search). The keygrip CMS (`recommendations`, `tenancy`) has been **removed**;
> the Django package stays named `keygrip` for now. The app serves on **`app.vent.dog`** (interim —
> the `clip.cool` apex edge cutover is pending DNS). R2 + Typesense are wired.
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

## Remaining work (from `docs/migration-from-keygrip.md`)

1. **Cloudflare apex cutover (`clip.cool`)** — the app serves on `app.vent.dog` for now; the apex is
   the later flip (the `clip.cool` zone + moving `app_hostname`/redirect URIs/`cf_zone`/ingress/LB/
   monitor/uptime-check/`drain.sh` from `app.vent.dog` → `clip.cool`). Pending the DNS verification.
2. **Video pipeline** — the ffmpeg/`svt-av1` transcode tier + heavy-worker image, `<video>`
   renditions, perceptual `pHash` dedup. The current slice is **images only** (R2 + Typesense + OCR
   are done and wired). R2 bucket/CORS provisioning is a one-time Cloudflare step.
3. **App rewrite** — under way: `clips` media app shipped, CMS (`recommendations`/`tenancy`) removed.
   Still keygrip-named as a Django package; full package rename is a later pass.
4. **CI / docs** — `.github/`, `.githooks/`, ADRs.
