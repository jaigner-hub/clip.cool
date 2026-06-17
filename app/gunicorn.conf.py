"""Gunicorn config — django-prometheus multiprocess support.

We run multiple gunicorn workers (`--workers 2`). Without multiprocess mode each
worker keeps its OWN in-memory prometheus counters, and the single `/metrics`
endpoint is answered by whichever worker served the scrape — so per-worker
series appear and vanish between scrapes, and Prometheus `rate()`/`increase()`
read each reappearance as a counter reset + new events, manufacturing phantom
rates (this is what fired the bogus `AppHttp5xx` 5xx alert).

`prometheus_client` multiprocess mode fixes it: workers write metric samples to
per-pid files under `PROMETHEUS_MULTIPROC_DIR`, and `django_prometheus`'s export
view aggregates them across workers into one consistent view. The two hooks below
are the required glue:
  - on_starting (master, pre-fork): clear stale per-pid files from a prior run.
  - child_exit: release a dead worker's files so its samples stop being counted.

We set PROMETHEUS_MULTIPROC_DIR *here* (master, before workers fork) rather than
as a Dockerfile ENV on purpose: it must NOT be set for `manage.py test`,
`collectstatic`, or one-off management commands — those run a single process with
no on_starting hook to create the dir, and prometheus_client would otherwise try
to use a missing multiproc dir. Setting it in the gunicorn config scopes it to the
forked workers only, so non-gunicorn invocations stay in normal single-process mode.
"""
import os
import shutil

# Scope multiprocess mode to gunicorn workers (inherited via fork); see above.
# /run (not /tmp) — runtime ephemeral state, and avoids Bandit B108 hardcoded-tmp.
_PROM_DIR = os.environ.setdefault("PROMETHEUS_MULTIPROC_DIR", "/run/prometheus-multiproc")


def on_starting(server):
    """Master start, before any worker forks: start from a clean metrics dir."""
    if _PROM_DIR:
        shutil.rmtree(_PROM_DIR, ignore_errors=True)
        os.makedirs(_PROM_DIR, exist_ok=True)


def child_exit(server, worker):
    """A worker died: drop its per-pid metric files so they aren't double-counted."""
    if _PROM_DIR:
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(worker.pid)
