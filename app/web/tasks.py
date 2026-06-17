"""Procrastinate tasks (ADR 0008). Autodiscovered from each app's ``tasks.py``.

Tasks are thin adapters over the service layer — same rule as views (CLAUDE.md): the logic
lives in services, the task just schedules and invokes it. This module is scaffolding: a
``health_check`` to prove the default queue end-to-end and a ``heartbeat`` periodic task that
exercises the periodic deferrer (one worker wins the pg_advisory_lock) + LISTEN/NOTIFY.

The four logical queues (``default`` / ``batch`` / ``workflow`` / ``aeo``) are served by the
worker container; real generation/publishing/aeo work will land on them as those features ship.
"""
import logging

from procrastinate.contrib.django import app

logger = logging.getLogger(__name__)


@app.task(queue="default")
def health_check(note: str = "") -> dict:
    """No-op task proving the queue round-trips a job. Returns a small JSON-able result."""
    logger.info("procrastinate health_check ran (note=%r)", note)
    return {"ok": True, "note": note}


@app.periodic(cron="*/5 * * * *")
@app.task(queue="default")
def heartbeat(timestamp: int) -> None:
    """Fires every 5 min. The periodic deferrer coordinates across workers via an advisory
    lock, so exactly one job is enqueued per slot even with workers on both boxes. ``timestamp``
    is the slot's epoch seconds, supplied by Procrastinate."""
    logger.info("procrastinate heartbeat @ %s", timestamp)
