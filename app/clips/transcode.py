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


def make_gif(src_path, out_path, crop_filter=None):
    """Optimized animated GIF (15fps, downscaled, palette-optimized) for chat autoplay — a fraction
    of a raw GIF. Reused by the transcode pass and by caption burn-in (so the shareable GIF carries
    the caption). `crop_filter` is an optional leading ffmpeg `crop=...` (used at transcode; a
    caption burn passes an already-cropped source so it's omitted)."""
    vf = _vf(crop_filter, "fps=15", "scale='min(480,iw)':-1:flags=lanczos") + \
        ",split[s0][s1];[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer"
    _run(["ffmpeg", "-y", "-i", src_path, "-vf", vf, out_path])
    return out_path


def transcode(src_path, workdir, crop=None):
    """Produce renditions + poster from src_path into workdir. Returns
    {'renditions': [{kind, path, mime}], 'poster': path|None, 'meta': <probe>}.
    `crop` ({x,y,w,h} fractions) is baked into every output via an ffmpeg crop filter.
    Raises RuntimeError if even H.264 fails (we cannot serve the asset without a fallback)."""
    meta = probe(src_path)
    cf, cropped = _crop_filter(crop, meta)
    renditions = []
    for kind, fname, mime, vargs in _RENDITIONS:
        out = os.path.join(workdir, fname)
        try:
            vf = ["-vf", cf] if cf else []
            _run(["ffmpeg", "-y", "-i", src_path, *vf, *vargs, out])
            renditions.append({"kind": kind, "path": out, "mime": mime})
        except Exception:
            logger.warning("transcode: %s rendition failed (encoder missing/error?)", kind, exc_info=True)
    if not any(r["kind"] == "h264" for r in renditions):
        raise RuntimeError("H.264 transcode failed; cannot serve the asset.")
    poster = os.path.join(workdir, "poster.webp")
    try:
        _run(["ffmpeg", "-y", "-i", src_path,
              "-vf", _vf(cf, "thumbnail", "scale='min(640,iw)':-1"), "-frames:v", "1",
              "-c:v", "libwebp", "-quality", "80", poster])
    except Exception:
        logger.warning("transcode: poster generation failed", exc_info=True)
        poster = None
    # Optimized animated GIF for chat autoplay (Discord/Slack loop GIFs inline); on-platform we
    # still serve the mp4. Re-burned with the caption later if one is added (see burn_caption_asset).
    gif = os.path.join(workdir, "preview.gif")
    try:
        make_gif(src_path, gif, crop_filter=cf)
    except Exception:
        logger.warning("transcode: gif generation failed", exc_info=True)
        gif = None
    # Report the cropped dimensions so the Asset stores what's actually served (caption overlays size
    # to these, etc.).
    if cropped:
        meta = {**meta, "width": cropped[0], "height": cropped[1]}
    return {"renditions": renditions, "poster": poster, "gif": gif, "meta": meta}
