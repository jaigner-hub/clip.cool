"""ffmpeg transcode helpers for the video tier (docs/phase2-video-captioning.md).

Subprocess-based; only invoked by the `transcode` Procrastinate task on the worker (where ffmpeg
is installed). Each rendition is attempted independently — a missing encoder (e.g. libsvtav1) skips
that codec rather than failing the whole asset; H.264 is the required universal fallback. Audio is
stripped (-an): meme loops play muted (docs/architecture.md); revisit with audio transcription.
"""
import json
import logging
import os
import subprocess

logger = logging.getLogger(__name__)

# (kind, output filename, mime, ffmpeg video args)
_RENDITIONS = [
    ("h264", "h264.mp4", "video/mp4",
     ["-c:v", "libx264", "-crf", "23", "-preset", "medium", "-pix_fmt", "yuv420p",
      "-movflags", "+faststart", "-an"]),
    ("vp9", "vp9.webm", "video/webm",
     ["-c:v", "libvpx-vp9", "-crf", "33", "-b:v", "0", "-row-mt", "1", "-an"]),
    ("av1", "av1.mp4", "video/mp4",
     ["-c:v", "libsvtav1", "-crf", "35", "-preset", "8", "-pix_fmt", "yuv420p",
      "-movflags", "+faststart", "-an"]),
]


def probe(path):
    """{'width','height','duration','has_audio'} via ffprobe (best-effort, {} on failure)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(out.stdout or "{}")
    except Exception:
        logger.warning("ffprobe failed for %s", path, exc_info=True)
        return {}
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    dur = (data.get("format") or {}).get("duration")
    return {
        "width": v.get("width"),
        "height": v.get("height"),
        "duration": float(dur) if dur else None,
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def _run(args, timeout=600):
    subprocess.run(args, check=True, capture_output=True, timeout=timeout)


def burn_caption(src_path, overlay_path, workdir, video=True):
    """Flatten a transparent caption PNG onto the source → a captioned file with the text baked into
    the pixels (mp4 for video, png for image). The overlay PNG is exported at the clip's native size,
    same as the H.264 rendition / original, so a plain overlay=0:0 aligns. Video output strips audio
    (matches the renditions) and is +faststart for streaming. Returns the output path."""
    out = os.path.join(workdir, "captioned." + ("mp4" if video else "png"))
    args = ["ffmpeg", "-y", "-i", src_path, "-i", overlay_path,
            "-filter_complex", "[0:v][1:v]overlay=0:0"]
    if video:
        args += ["-c:v", "libx264", "-crf", "23", "-preset", "medium", "-pix_fmt", "yuv420p",
                 "-movflags", "+faststart", "-an", out]
    else:
        args += ["-frames:v", "1", out]
    _run(args)
    return out


def _crop_filter(crop, meta):
    """Turn a {x,y,w,h}-fractions crop + probed source dims into an ffmpeg `crop=W:H:X:Y` filter
    string (even dims, clamped in-bounds), and the resulting (w,h). Returns (None, None) if there's
    no crop or we couldn't read the source size."""
    if not crop:
        return None, None
    iw, ih = meta.get("width"), meta.get("height")
    if not iw or not ih:
        return None, None
    x = int(round(crop["x"] * iw))
    y = int(round(crop["y"] * ih))
    w = int(round(crop["w"] * iw))
    h = int(round(crop["h"] * ih))
    x = max(0, min(x, iw - 2))
    y = max(0, min(y, ih - 2))
    w = max(2, min(w, iw - x)) & ~1   # clamp, then force even
    h = max(2, min(h, ih - y)) & ~1
    return "crop=%d:%d:%d:%d" % (w, h, x, y), (w, h)


def _vf(*filters):
    """Join non-empty ffmpeg filter strings with commas."""
    return ",".join(f for f in filters if f)


def _seek(trim):
    """A (start, dur) trim → (args before -i, args after -i). `-ss` before the input is a fast seek;
    `-t` after it caps the duration. With re-encoding (every rendition) this is frame-accurate."""
    if not trim:
        return [], []
    start, dur = trim
    pre = ["-ss", "%.3f" % start] if start and start > 0 else []
    post = ["-t", "%.3f" % dur] if dur and dur > 0 else []
    return pre, post


# Max width for the heavy video renditions (AV1/VP9/H.264). A meme loop doesn't need more, and high-res
# captures (2K–4K tabs) otherwise slow the encode enough to hit the per-ffmpeg timeout. Never upscales.
RENDITION_MAX_W = 1280

# gifsicle --lossy level for the GIF re-compression pass. 0 = LOSSLESS only (-O3): visibly degraded
# the GIF at higher values, so we keep it off. Bump >0 only if you want smaller files at the cost of
# quality (e.g. 30 is gentle, 80 is aggressive).
GIF_LOSSY = 0


def _optimize_gif(path):
    """Shrink the GIF in place with gifsicle -O3 (fully lossless re-pack; ~10-25% smaller, no quality
    change) + optional --lossy when GIF_LOSSY > 0. Best-effort — if gifsicle is missing or errors,
    keep the ffmpeg GIF untouched."""
    tmp = path + ".opt"
    args = ["gifsicle", "-O3"]
    if GIF_LOSSY:
        args.append("--lossy=%d" % GIF_LOSSY)
    args += [path, "-o", tmp]
    try:
        _run(args)
        os.replace(tmp, path)
    except FileNotFoundError:
        logger.info("gifsicle not installed; skipping GIF re-compression")
    except Exception:
        logger.warning("gifsicle optimization failed; using the unoptimized GIF", exc_info=True)
        try:
            os.path.exists(tmp) and os.remove(tmp)
        except OSError:
            pass


def make_gif(src_path, out_path, crop_filter=None, seek_pre=None, seek_post=None):
    """Optimized animated GIF (15fps, downscaled, palette-optimized) for chat autoplay — a fraction
    of a raw GIF. Reused by the transcode pass and by caption burn-in (so the shareable GIF carries
    the caption). `crop_filter` is an optional leading ffmpeg `crop=...`; seek_pre/seek_post carry a
    trim (used at transcode; a caption burn passes an already-cropped+trimmed source so they're omitted)."""
    # Highest-quality GIF ffmpeg can do: a PER-FRAME local palette (palettegen stats_mode=single →
    # paletteuse new=1) so every frame gets its own best 256 colours instead of one palette shared
    # across the whole clip — the big quality lever short of a dedicated encoder (gifski). 20fps,
    # 640px, fine Bayer dither. GIF is still 8-bit, so dark gradients band somewhat; this minimises it.
    # Larger files than a global palette, but quality > size for the GIF fallback.
    vf = _vf(crop_filter, "fps=20", "scale='min(640,iw)':-2:flags=lanczos") + \
        ",split[s0][s1];[s0]palettegen=stats_mode=single[p];" \
        "[s1][p]paletteuse=new=1:dither=bayer:bayer_scale=5"
    _run(["ffmpeg", "-y", *(seek_pre or []), "-i", src_path, *(seek_post or []), "-vf", vf, out_path])
    _optimize_gif(out_path)
    return out_path


def _scale_cap(w, h):
    """Downscale filter to keep renditions at most RENDITION_MAX_W wide (height even, never upscale),
    and the resulting (w, h). A high-res capture (e.g. a 2.4K tab) is pointless for a web/meme loop and
    can blow past the encode timeout (libx264 preset medium at 2400px is slow). Returns (None, w, h)
    when no scaling is needed."""
    if not w or not h or w <= RENDITION_MAX_W:
        return None, w, h
    out_w = RENDITION_MAX_W & ~1
    out_h = int(round(h * out_w / w)) & ~1
    return "scale=%d:%d:flags=lanczos" % (out_w, out_h), out_w, out_h


def transcode(src_path, workdir, crop=None, trim=None):
    """Produce renditions + poster from src_path into workdir. Returns
    {'renditions': [{kind, path, mime}], 'poster': path|None, 'meta': <probe>}.
    `crop` ({x,y,w,h} fractions) is baked into every output via an ffmpeg crop filter; `trim`
    ((start, dur) seconds, dur None = to end) is applied as an input seek so the encode only
    processes the kept range. Heavy renditions are downscaled to RENDITION_MAX_W so a high-res capture
    can't time out the encode. Raises RuntimeError if even H.264 fails (no servable fallback)."""
    meta = probe(src_path)
    cf, cropped = _crop_filter(crop, meta)
    pre, post = _seek(trim)
    # Base (post-crop) dims → scale cap for the heavy codecs (poster/GIF already downscale on their own).
    base_w = cropped[0] if cropped else meta.get("width")
    base_h = cropped[1] if cropped else meta.get("height")
    scale_f, out_w, out_h = _scale_cap(base_w, base_h)
    rend_vf = _vf(cf, scale_f)
    renditions = []
    for kind, fname, mime, vargs in _RENDITIONS:
        out = os.path.join(workdir, fname)
        try:
            vf = ["-vf", rend_vf] if rend_vf else []
            _run(["ffmpeg", "-y", *pre, "-i", src_path, *post, *vf, *vargs, out])
            renditions.append({"kind": kind, "path": out, "mime": mime})
        except Exception:
            logger.warning("transcode: %s rendition failed (encoder missing/error?)", kind, exc_info=True)
    if not any(r["kind"] == "h264" for r in renditions):
        raise RuntimeError("H.264 transcode failed; cannot serve the asset.")
    poster = os.path.join(workdir, "poster.webp")
    try:
        _run(["ffmpeg", "-y", *pre, "-i", src_path, *post,
              "-vf", _vf(cf, "thumbnail", "scale='min(640,iw)':-1"), "-frames:v", "1",
              "-c:v", "libwebp", "-quality", "80", poster])
    except Exception:
        logger.warning("transcode: poster generation failed", exc_info=True)
        poster = None
    # Optimized animated GIF for chat autoplay (Discord/Slack loop GIFs inline); on-platform we
    # still serve the mp4. Re-burned with the caption later if one is added (see burn_caption_asset).
    gif = os.path.join(workdir, "preview.gif")
    try:
        make_gif(src_path, gif, crop_filter=cf, seek_pre=pre, seek_post=post)
    except Exception:
        logger.warning("transcode: gif generation failed", exc_info=True)
        gif = None
    # Report the served dimensions (post-crop, post-downscale) + duration (trimmed) so the Asset stores
    # what's actually served — caption overlays are exported at these dims and must match the renditions.
    if out_w and out_h:
        meta = {**meta, "width": out_w, "height": out_h}
    if trim:
        start, dur = trim
        if dur is not None:
            meta = {**meta, "duration": dur}
        elif meta.get("duration") is not None and start:
            meta = {**meta, "duration": max(0.0, meta["duration"] - start)}
    return {"renditions": renditions, "poster": poster, "gif": gif, "meta": meta}
