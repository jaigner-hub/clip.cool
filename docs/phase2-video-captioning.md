# Phase 2 — Video pipeline, captioning, and Snipper integration

> **Status: plan, not built.** Phase 1 (image/template meme builder) is live. This is the in-depth
> plan for video. Decisions below are settled; the step lists are the execution backlog.

Phase 2 turns clip.cool into a real video meme host and create surface. It's three interlocking
tracks, shippable in sequence:

- **2a — Video transcode tier** (the deferred "never serve a GIF as a GIF" core).
- **2b — Video captioning** (WYSIWYG, editable, no burn-in for on-platform display).
- **2c — Snipper API integration** (desktop capture pushes straight into clip.cool).

## Settled decisions

| Decision | Choice | Why |
|---|---|---|
| Caption storage/render | **Editable layers + on-demand burn-in** | Source of truth is the text-box geometry; the player overlays it over the *clean* looping video (instant, re-editable forever). A burned-in file is rendered only for download/off-platform share. |
| Transcode packaging | **ffmpeg/svt-av1 in the existing worker + a `transcode` queue** | Fastest path; split into a dedicated heavy-worker tier when AV1 volume demands it (architecture.md). |
| Snipper auth | **OIDC device-code flow** | Desktop app logs the user in via Keycloak once, gets a *user* token, calls the existing user-scoped bearer API. No new credential system (we removed machine tokens). |

## The WYSIWYG insight (why captioning won't drift like Snipper's did)

A meme caption is a **static layer** — it doesn't change frame to frame. So the browser renders the
text **once** to a **transparent PNG** using the *exact same canvas code as the Phase 1 builder*
(`clips/static/clips/builder.js`). That single PNG is:
- overlaid over the looping `<video>` in the player (on-platform display), and
- composited onto every frame by `ffmpeg overlay` for the downloadable burned-in file.

Both consume the *same* PNG, so preview === output by construction — no second renderer, no
`drawtext`, no drift. The source video stays untouched; the text-box geometry is stored for re-edit.

---

## 2a — Video transcode tier

**Data model** (`clips/models.py`):
- `Asset.media_type` = `image | video` (derive from mime at ingest).
- `Asset.duration` (float, nullable), `Asset.has_audio` (bool).
- New `Rendition(asset FK, kind, r2_key, mime, width, height, bytes)` where `kind ∈
  {av1, vp9, h264, poster, sprite}`. One Asset → many renditions.
- `status` gains `transcoding` between `pending` and `ready`.

**Ingest**: `finalize_asset` detects video by content-type → `media_type=video`, `status=pending`
→ enqueue `transcode_asset` on the **`transcode`** queue (worker `--queues` gains `transcode`).

**Worker image**: add `ffmpeg` (with `libsvtav1`, `libvpx`, `libx264`) to `app/Dockerfile`'s
apt install. (Debian's ffmpeg includes these.) Heavy; revisit a dedicated worker tier later.

**`transcode_asset(asset_id)`** (queue `transcode`): download original → ffmpeg →
- **H.264 MP4** — `-c:v libx264 -crf 23 -preset medium -pix_fmt yuv420p -movflags +faststart`
  (universal fallback; keep audio if present, else `-an`).
- **VP9 WebM** — `-c:v libvpx-vp9 -crf 33 -b:v 0 -row-mt 1`.
- **AV1** — `-c:v libsvtav1 -crf 35 -preset 8` (svt-av1; the bandwidth win, slow — why it wants a
  dedicated tier eventually).
- **Poster** — a representative frame → WebP/AVIF (reuse the existing poster path).
- **Scrub sprite** — N frames tiled into one sheet (the Imgur-beating hover-scrub). *MVP-optional.*
→ upload each to R2 (`renditions/<asset>/<kind>.<ext>`), create `Rendition` rows, `status=ready`,
then the existing `index` work (OCR + vision caption still run on the poster).

**Serve**: `<video autoplay loop muted playsinline>` with ordered `<source>` **AV1 → VP9 → H.264**
(public R2 URLs); poster as the `poster=` attribute. Short clips → progressive, skip HLS/DASH.

**Steps**: model + migration → Dockerfile ffmpeg → `transcode_asset` task + queue → R2 rendition
keys → `serialize`/templates emit `<video>` sources → detail/search/library render video.

---

## 2b — Video captioning (layers + on-demand burn-in)

**Builder, extended to video** (`clips/static/clips/builder.js`): the base is the asset's video
(or a template). Show a representative frame behind the editing canvas; the same draggable text-box
UI from Phase 1. On publish, export the text layer as a **transparent PNG at native resolution**.

**Storage** (`Asset`):
- `caption_layers` JSON — the boxes (`text, cx, cy, w, size, …`) — the editable source of truth
  (generalizes Phase 1's `builder_state`; for video the "template" is the asset's own video).
- `text_layer_key` — the rendered transparent PNG in R2.

**On-platform display**: player = `<video>` (clean renditions) + the `text_layer_key` PNG
absolutely positioned over it, scaled identically (it's transparent → perfect overlay). Same PNG as
the builder export ⇒ WYSIWYG. Source video never altered.

**On-demand burn-in** (download / off-platform share): enqueue an ffmpeg job that overlays the text
PNG onto the renditions (`-i video -i text.png -filter_complex overlay`) → a captioned MP4/GIF →
cache in R2. Generated lazily on first download.

**Re-edit** (ties in the Phase 1 ask): "Edit meme" reopens the builder from `caption_layers`,
re-renders the PNG, re-runs the overlay/index. Works for any clip with stored layers
(builder- or Snipper-sourced), not burned-in uploads.

**Indexing**: we know the typed caption → it's the clean indexed text (no OCR needed for
builder/captioned clips; OCR stays the fallback for raw uploads).

**Steps**: `caption_layers` + `text_layer_key` fields → builder video mode (frame behind canvas,
export text PNG) → publish stores layers + PNG → player overlay → on-demand burn-in task → wire
re-edit.

---

## 2c — Snipper API integration (OIDC device flow)

**Keycloak**: add a public client `clip-snipper` in the `keygrip` realm with the **device
authorization grant** enabled (`roles/keycloak_realm`).

**Snipper (desktop, separate repo)**: device flow → poll for the user's access token → call the
existing user-scoped API: `POST /api/v1/clips/uploads/presign` → `PUT` the capture to R2 →
`POST /api/v1/clips/assets`. The user then captions on clip.cool (no captioning in Snipper).

**clip.cool side (small)**: add video content types to `ALLOWED_CONTENT_TYPES`; `finalize` routes
video → the transcode queue (2a). That's most of it — the heavy lifting (capture) stays in Snipper.

**Steps**: `clip-snipper` realm client (device flow) → allow video content types → document the
ingest API (it already exists for user tokens) → Snipper implements its client side.

---

## Sequence & risks

**Sequence**: 2a (unlocks video at all) → 2b (captioning on top) → 2c (Snipper push). Each ships
independently and is independently useful.

**Risks / watch-outs**:
- **AV1 encode cost/time** on the shared worker — start `-preset 8`, watch the `transcode` queue
  depth; this is the trigger to split out the dedicated heavy tier.
- **Player overlay alignment** — the text PNG and `<video>` must scale identically (same intrinsic
  size + `object-fit`); test across aspect ratios.
- **R2 storage growth** — multiple renditions per asset + burn-ins; perceptual `pHash` dedup
  (deferred from Phase 1) becomes more valuable here.
- **Bucket CORS** — the player fetching the text PNG cross-origin is fine (`<img>`), but the
  builder loading the *video frame* onto a canvas needs the same same-origin-proxy trick as Phase 1
  templates (avoid canvas taint).
