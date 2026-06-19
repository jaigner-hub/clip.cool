FROM python:3.14-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DJANGO_SETTINGS_MODULE=keygrip.settings.prod

WORKDIR /app
# curl: health/debug. tesseract-ocr: OCR (index queue). ffmpeg: video transcode tier
# (AV1/VP9/H.264 + poster) on the `transcode` queue — Debian's ffmpeg includes libsvtav1/libvpx/
# libx264. gifsicle: lossless (-O3) + gentle lossy GIF re-compression post-pass so the chat GIF
# (Discord/Signal only autoplay-loop real GIFs) is small without an obvious quality drop.
# Heavy; a dedicated worker tier can be split out later (docs/phase2-video-captioning.md).
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl tesseract-ocr ffmpeg gifsicle \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# Build-time only; real secrets/hosts are injected at runtime via compose env.
RUN python manage.py collectstatic --noinput

EXPOSE 8000
# -c gunicorn.conf.py wires prometheus multiprocess hooks so /metrics aggregates
# across workers (PROMETHEUS_MULTIPROC_DIR above). The dir is (re)created clean on
# master start by the on_starting hook.
CMD ["gunicorn", "keygrip.asgi:application", \
     "-c", "gunicorn.conf.py", \
     "-k", "uvicorn_worker.UvicornWorker", \
     "-b", "0.0.0.0:8000", "--workers", "2", "--access-logfile", "-"]
