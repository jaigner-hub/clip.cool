"""Postgres backend that retries connect() through a brief leader handoff (ADR 0016).

On an HA reboot the Patroni leader switches over; for ~1-2s there's no writable primary. With
CONN_MAX_AGE=0 every request opens a fresh connection, and the failure surfaces in Django's
connection *setup* — ``init_connection_state`` runs a ``SET TIME ZONE`` while configuring the new
connection, and that errors. That's at connect() time, NOT query execution, which is why a request
middleware (execute_wrapper) can't see it. Retrying connect() does.

Safe for ALL request methods: only the CONNECT is retried, never a query — so a write is never
replayed (it runs exactly once, after a connection is established). Keeps django_prometheus's DB
metrics by subclassing its wrapper. Tunable via DB_CONNECT_RETRY_ATTEMPTS /
DB_CONNECT_RETRY_BACKOFF_SECONDS.
"""
import logging
import time

from django.conf import settings
from django.db.utils import InterfaceError, OperationalError
from django_prometheus.db.backends.postgresql.base import DatabaseWrapper as _PrometheusWrapper

logger = logging.getLogger(__name__)


class DatabaseWrapper(_PrometheusWrapper):
    def connect(self):
        attempts = getattr(settings, "DB_CONNECT_RETRY_ATTEMPTS", 8)
        backoff = getattr(settings, "DB_CONNECT_RETRY_BACKOFF_SECONDS", 0.3)
        for attempt in range(attempts):
            try:
                return super().connect()
            except (OperationalError, InterfaceError) as exc:
                if attempt == attempts - 1:
                    logger.warning("DB connect failed after %d attempts; giving up: %s", attempts, exc)
                    raise
                self._discard_failed_connection()
                logger.warning(
                    "DB connect failed (attempt %d/%d), retrying — likely a leader switchover: %s",
                    attempt + 1, attempts, exc,
                )
                time.sleep(min(backoff * (attempt + 1), 1.0))

    def _discard_failed_connection(self):
        # connect() may have opened the socket and then failed in init_connection_state; drop the
        # half-open handle so the retry starts clean.
        conn, self.connection = self.connection, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # pragma: no cover - best effort cleanup
                pass
