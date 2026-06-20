# In-browser tab recorder (`/clips/record/`)

> **Status: live.** A no-plugin, in-browser way to clip a moment from any tab you can watch
> (YouTube, a stream, anything), crop + trim it, caption it, and share it — the web-native sibling of
> the planned desktop **Snipper** (see [`phase2-video-captioning.md`](./phase2-video-captioning.md)
> §2c). Solves the content cold-start problem ("I wish you already had the clip I wanted") by letting
> users bring the source themselves.

## What it is

A page at **`/clips/record/`** (nav: **Record**, signed-in users) that:

1. **Shares a browser tab** via `navigator.mediaDevices.getDisplayMedia()` — no extension, no install.
2. **Records the moment** with `MediaRecorder` (the record window *is* the trim) into a `video/webm`
   blob.
3. **Optionally crop** (drag a box on the live preview) and **trim** (in/out scrubber on the
   recorded clip).
4. **Uploads** through the **existing** ingest path — presign → PUT-to-R2 → finalize — exactly like a
   file upload. `video/webm` already routes to the transcode queue, so there were **no backend ingest
   changes** for the recorder itself; crop/trim are applied server-side at transcode.

It then flows through the normal pipeline (transcode → poster → OCR/vision → Typesense) and you can
caption it like any clip.

## Files

| File | Role |
|---|---|
| `app/templates/clips/record.html` | The page: share button, live preview + crop overlay, playback + trim bar, title/tags, upload. Loads `clips.css` + `record.js`. |
| `app/clips/static/clips/record.js` | All client logic (CSP-clean external script): getDisplayMedia, MediaRecorder, crop selection, trim scrubber, presign/PUT/finalize. |
| `app/clips/views.py` → `record_page` | `@login_required @ensure_csrf_cookie`; renders `record.html`. Reuses `presign`/`finalize`. |
| `app/clips/urls.py` → `clips_record` | Route `clips/record/`. |
| `app/templates/app_base.html` | "Record" nav link. |

## Key design decisions (and the walls we hit)

### Record the RAW tab, crop/trim on the server
The first instinct — crop client-side by drawing the region to a `<canvas>` and recording that — **was
abandoned**, because the canvas draw loop runs on `requestAnimationFrame`, which the browser
**throttles to a stop when the clip.cool tab is backgrounded**. And you *must* background it (you tab
over to YouTube to press play). So a canvas crop froze the video while recording (audio kept flowing).

Fix: **always record the raw `getDisplayMedia` stream** (MediaRecorder keeps encoding in the
background), keep the crop/trim as *selections*, and **bake them in with ffmpeg at transcode**:
- **Crop** → `Asset.crop` = `{x,y,w,h}` fractions (0–1) → ffmpeg `crop=` filter on every rendition.
- **Trim** → `Asset.trim_start` / `Asset.trim_end` seconds → ffmpeg input seek (`-ss` / `-t`), so the
  encode only ever processes the kept range ("decided before transcoding begins").

The trim scrubber's timeline uses the **wall-clock record length** as its reference, because
MediaRecorder's webm duration metadata is unreliable (often `Infinity` until you seek to the end).

### Why not Region Capture (`cropTo`)?
The purpose-built "crop a captured tab to one element" API is **self-capture only** — it can only crop
the *same* tab doing the capture, never a different tab (YouTube). So it can't help here. True
client-side region crop that survives backgrounding would need a WebCodecs / insertable-streams worker
(`MediaStreamTrackProcessor`) — a bigger, Chromium-only build, deferred unless upload size demands it.

### Keep focus on clip.cool
Sharing a tab normally yanks focus to the captured tab. We use the **Conditional Focus API**
(`CaptureController.setFocusBehavior("no-focus-change")`, feature-detected) so focus stays on
clip.cool after you pick the tab.

### Pop the controls out (Document Picture-in-Picture)
The tab-dance — switch to the source tab to press play, switch back to hit Record — is the recorder's
main ergonomic wart. There's **no way to inject a click into a captured tab** (browser security:
synthesized clicks into an arbitrary captured surface would be a clickjacking primitive — the only
sanctioned back-channel, Captured Surface Control, is wheel-scroll + zoom only, never clicks/keys).
So instead of pulling the source's play button into clip.cool, we **push our controls out**:
**“⧉ Pop out controls”** opens a **Document Picture-in-Picture** window
(`documentPictureInPicture.requestWindow()`, Chromium 116+) and **moves** the live preview +
Record/Stop into it. It floats always-on-top, so the user stays on the source tab, presses play
natively, and hits Record in the float — no switch back.

- **Move, don't clone:** adopting a node into the PiP document **preserves its event listeners**, so
  the same Record/Stop buttons keep working — no re-wiring. On close we move them back to their
  original spot (tracked via parent + nextSibling).
- **Styles:** the PiP window starts blank; we clone the same-origin `<link rel="stylesheet">`s into it
  (CSP-clean — no inline CSS). Body padding/background set via the CSSOM `.style` API, which isn't
  subject to CSP (unlike `style=""` attributes).
- **Lifecycle:** the float auto-restores on its native close button, on **record stop** (trim/upload
  UI lives on the page), on **Share a different tab**, and on teardown/unload. Feature-detected — the
  button is hidden where Document PiP is unsupported (Firefox/Safari), which fall back to the
  page-only flow.
- **Repaint:** the crop overlay is repainted with the **PiP window's** `requestAnimationFrame`, not
  the main window's — the latter is throttled the instant clip.cool is backgrounded, which is exactly
  when the float is in use.

### Cap the capture resolution
`getDisplayMedia` is constrained to **≤1920×1080** so a 2K/4K tab doesn't bloat the upload or server
decode. Pairs with the server-side rendition downscale (≤1280px — see architecture). Doesn't affect
background recording (it's a capture constraint, not a canvas pipeline).

## The ingest contract (also used by the JSON API / future Snipper)

`finalize` (`POST /clips/upload/finalize` session, or `POST /api/v1/clips/assets` bearer) accepts,
beyond `key`/`title`/`content_type`/`tags`:

```jsonc
{
  "crop":       { "x": 0.05, "y": 0.08, "w": 0.6, "h": 0.37 },  // fractions of source; video only
  "trim_start": 1.5,   // seconds (optional)
  "trim_end":   6.0    // seconds (optional; omit = to end)
}
```

Both are **sanitized server-side** (`services._clean_crop` / `_clean_trim`) — a degenerate/junk value
is dropped rather than erroring — and applied only to video assets, baked in by `transcode()`.

## Browser support & honest caveats

- **Works:** desktop Chrome / Edge / Firefox. The page feature-detects and tells iOS Safari users (no
  `getDisplayMedia`) to use Upload instead.
- **It's a capture, not the source bytes:** quality is "video of a video" — lossier than what
  `yt-dlp` would pull, and DRM'd tabs (Netflix etc.) record as black frames. YouTube/Twitch/Reddit
  capture fine. This is the deliberate trade for the clean-legal-posture, no-server-download path.
- **Audio:** tab audio is captured if the user ticks "share tab audio", **but** served renditions are
  currently muted (`-an`) by platform design — so the shared clip is silent. Revisit if audio memes
  matter.
