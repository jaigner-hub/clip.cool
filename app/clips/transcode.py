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


def transcode(src_path, workdir):
    """Produce renditions + poster from src_path into workdir. Returns
    {'renditions': [{kind, path, mime}], 'poster': path|None, 'meta': <probe>}.
    Raises RuntimeError if even H.264 fails (we cannot serve the asset without a fallback)."""
    meta = probe(src_path)
    renditions = []
    for kind, fname, mime, vargs in _RENDITIONS:
        out = os.path.join(workdir, fname)
        try:
            _run(["ffmpeg", "-y", "-i", src_path, *vargs, out])
            renditions.append({"kind": kind, "path": out, "mime": mime})
        except Exception:
            logger.warning("transcode: %s rendition failed (encoder missing/error?)", kind, exc_info=True)
    if not any(r["kind"] == "h264" for r in renditions):
        raise RuntimeError("H.264 transcode failed; cannot serve the asset.")
    poster = os.path.join(workdir, "poster.webp")
    try:
        _run(["ffmpeg", "-y", "-i", src_path,
              "-vf", "thumbnail,scale='min(640,iw)':-1", "-frames:v", "1",
              "-c:v", "libwebp", "-quality", "80", poster])
    except Exception:
        logger.warning("transcode: poster generation failed", exc_info=True)
        poster = None
    # Optimized animated GIF for chat autoplay (Discord/Slack loop GIFs inline). Downscaled +
    # palette-optimized so it's a fraction of a raw GIF; on-platform we still serve the mp4.
    gif = os.path.join(workdir, "preview.gif")
    try:
        _run(["ffmpeg", "-y", "-i", src_path, "-vf",
              "fps=15,scale='min(480,iw)':-1:flags=lanczos,split[s0][s1];"
              "[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer", gif])
    except Exception:
        logger.warning("transcode: gif generation failed", exc_info=True)
        gif = None
    return {"renditions": renditions, "poster": poster, "gif": gif, "meta": meta}
