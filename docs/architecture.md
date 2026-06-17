# clip.cool — Architecture

**clip.cool** is a GIF/meme hosting platform built to be faster and cheaper than Giphy / Tenor /
Imgur. It is a **fresh application** built on the infrastructure scaffolding developed for
[`keygrip`](../../keygrip) (Ansible-managed Docker stacks on the `vent.dog` + `vent.dog2` IONOS
pair, behind Cloudflare). clip.cool **replaces keygrip's web footprint** on that pair; the existing
**chat app at `chat.vent.dog`** (Matrix/dendrite + LiveKit + the Go `vent` server) keeps running
beside it on the shared platform (Keycloak realm, HA Postgres, the `keygrip-edge` Docker network,
observability, cloudflared).

> Status: scaffolding ported, app-tier renamed `keygrip → clip`. The media pipeline below is the
> design target, not yet built.

## The one rule that beats the incumbents

**Never serve a GIF as a GIF.** GIF is a 1987 codec — a 5 MB GIF is often a ~400 KB MP4 at *better*
quality. Every serious player transcodes ingested GIFs to looping video. So the whole pipeline is:

> accept anything (`.gif`/`.mov`/`.mp4`/`.webm`) → normalize → emit multiple modern renditions →
> serve `<video autoplay loop muted playsinline>`, **never** `<img>`.

That single decision makes us faster and cheaper than half the field before we add a feature.

## Stack — decisions (inherited from keygrip unless noted)

| Area | Decision |
|---|---|
| **Architecture** | **API-first** (inherited). A shared service/business-logic core; on top a **Django Ninja** JSON API at `/api/v1` (async, Pydantic, auto-OpenAPI/Swagger) **and** thin HTML-fragment views. Logic never lives in views. |
| **Web serving** | **Gunicorn + Uvicorn (ASGI)** behind **cloudflared** straight to the app port — no local Nginx/Caddy (inherited, ADR 0004). |
| **Auth** | **Keycloak** as sole auth, OIDC. clip uses new `clip-web` / `clip-kc-admin` / `clip-api-docs` clients in the **existing `keygrip` realm** (shared with chat; realm rename deferred). |
| **Object storage / delivery** | ⚠️ **DEVIATES from keygrip.** **Cloudflare R2** (S3-compatible, **zero egress**) as origin, **Cloudflare CDN** in front. For a business that exists to push video bytes, egress is the cost that bankrupts you — S3/Spaces egress is brutal at scale; R2 removes it. (Cloudflare Stream was considered and rejected: per-minute-stored + per-minute-delivered pricing balloons on a high-volume meme catalog. We self-manage the pipeline and use R2 as a dumb bucket.) |
| **Database** | **Postgres** — the self-hosted **HA cluster** on the vent.dog pair (Patroni + etcd, inherited ADR 0016). Holds metadata (assets, renditions, tags, users, popularity). The `clip` DB/role replaces `keygrip`. |
| **Async / transcode queue** | ⚠️ **Adapted.** The reference write-up assumes Celery + Redis; **our platform has no Redis broker.** We use **Procrastinate** (Postgres-backed, inherited ADR 0008). Queue split: `transcode` (ffmpeg, heavy) / `thumbs` (posters + scrub sprites) / `index` (search + dedup + OCR). |
| **Transcoding** | **ffmpeg** in containerized workers pulling from the `transcode` queue. Likely a **dedicated worker tier** separate from the light app workers — AV1 in particular is CPU-hungry (see pipeline below). |
| **Search & discovery** | **Meilisearch** or **Typesense** (lightweight, self-hostable, typo-tolerant). Search relevance is where Giphy/Tenor live or die — a SQL `LIKE` is not enough. Runs as its own container on the edge net; Postgres stays the source of truth, the engine is a rebuildable index. |
| **Edge / HA** | **Cloudflare Tunnel** (origins have no public ports) + **Load Balancer** across both boxes (inherited). |
| **Infra / config** | **Ansible** + **SOPS/age** + Docker (inherited, ADR 0006/0001/0007). Deploy via `ansible/ac`. |
| **Observability** | Self-hosted **Grafana + Prometheus + Loki + Tempo** (inherited). clip-web/clip-worker traces; transcode metrics are a key SLO surface. |

## Media pipeline

### 1. Ingest — presigned direct-to-bucket
The client never streams bytes through Django. Django issues a **presigned R2 PUT URL**; the browser
uploads **straight to the bucket**, then calls back to enqueue a `transcode` job. App servers stay
thin and the upload path scales horizontally. (Original is stored under a content-addressed key; see
dedup.)

### 2. Transcode — ffmpeg workers
Per upload, emit a rendition set:
- **H.264 MP4** (`yuv420p`, `+faststart`) — universal fallback, plays everywhere.
- **VP9 WebM** and/or **AV1** — 30–50% smaller than H.264, served to browsers that advertise
  support. AV1 is the bandwidth win but slow to encode — use **`svt-av1`**, not `libaom`, or it
  murders worker throughput.
- **Poster / thumbnail** — first good frame as **AVIF/WebP**.
- **Hover-scrub sprite** (or a tiny preview clip) — the scrubbable preview is a real perceived-quality
  differentiator vs. Imgur.

Heavy encodes (AV1) justify a **dedicated, separately-scaled worker pool** so they don't starve the
fast `thumbs`/`index` work. For early scale this runs on the IONOS boxes / homelab; the queue makes
it trivially horizontal later.

### 3. Deliver — `<video>`, never `<img>`
Serve `<video autoplay loop muted playsinline>` with ordered sources **AV1 → VP9 → H.264**; the
browser picks the best it supports. For clips under ~30 s, **progressive MP4/WebM is fine** — skip
HLS/DASH packaging; it's overkill and adds latency for short loops. R2 origin, Cloudflare CDN front.

## Differentiators (the reasons to switch from Tenor)

- **Perceptual dedup.** Hash uploads with **pHash / video-hash** so re-uploads of the same clip
  collapse to one canonical asset. Saves storage, cleans search, and lets us aggregate
  views/popularity across duplicates — something Imgur does badly. Runs on the `index` queue at
  ingest, before transcode commits a new canonical asset.
- **Auto-caption + OCR indexing.** Run burned-in text (OCR) and audio (transcription) through and
  index the result, so people can search memes **by what's said in them**, not just hand-tagged
  keywords. No incumbent does this well, and we're already deep in the LLM tooling world — it's in
  our wheelhouse.

## Deploy footprint on the vent.dog pair

clip-web + clip-worker deploy where keygrip-web did (`ansible/playbooks/clip-web.yml`), on the
`keygrip-edge` network, against the `clip` DB in the shared HA Postgres cluster, authenticating to
the shared Keycloak realm. New components for clip (not yet rolled into Ansible):

- **R2 bucket + credentials** (replaces the DO Spaces plan) — SOPS secrets + app env.
- **ffmpeg transcode worker** image/role (the heavy `transcode` queue tier).
- **Meilisearch/Typesense** container + role.
- **Public domain `clip.cool` at the apex** — needs the `clip.cool` Cloudflare zone and a
  tunnel-ingress / Load-Balancer cutover (the `cloudflare` role is currently single-zone on
  `vent.dog`; the app's OIDC redirect URIs already point at `https://clip.cool/*`).

See [`docs/migration-from-keygrip.md`](./migration-from-keygrip.md) for what was ported, renamed,
and what remains.
