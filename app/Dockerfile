FROM python:3.14-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DJANGO_SETTINGS_MODULE=keygrip.settings.prod

WORKDIR /app
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
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
