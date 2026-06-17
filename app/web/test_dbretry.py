"""keygrip.dbretry — Postgres backend that retries connect() through an HA leader handoff (ADR 0016).

WHY: on a planned leader switchover the new connection's setup (init_connection_state) errors for a
beat; retrying connect() bridges it so a page load doesn't 500. Only the connect is retried — never a
query — so this is safe even for writes; that boundary is asserted via the exception type handling.
"""
from unittest import mock

from django.db import connections
from django.db.utils import InterfaceError, OperationalError
from django.test import SimpleTestCase, override_settings

from keygrip.dbretry.base import DatabaseWrapper

_SUPER_CONNECT = "django_prometheus.db.backends.postgresql.base.DatabaseWrapper.connect"


@override_settings(DB_CONNECT_RETRY_BACKOFF_SECONDS=0, DB_CONNECT_RETRY_ATTEMPTS=8)
class ConnectRetryTests(SimpleTestCase):
    def _wrapper(self):
        settings_dict = dict(connections["default"].settings_dict)
        settings_dict["ENGINE"] = "keygrip.dbretry"
        return DatabaseWrapper(settings_dict, "retry_test")

    def test_retries_connect_then_succeeds(self):
        # WHY: the handoff is a couple seconds; the connect, retried, must land on the new primary.
        outcomes = [OperationalError("could not connect"), OperationalError("could not connect"), "live-conn"]
        with mock.patch(_SUPER_CONNECT, autospec=True, side_effect=outcomes) as sup, \
             mock.patch("keygrip.dbretry.base.time.sleep") as sleep:
            result = self._wrapper().connect()
        self.assertEqual(result, "live-conn")
        self.assertEqual(sup.call_count, 3)
        self.assertTrue(sleep.called)

    def test_gives_up_and_reraises_after_max_attempts(self):
        # WHY: a genuinely-down DB must still surface — the retry is a brief bridge, not a mask.
        with mock.patch(_SUPER_CONNECT, autospec=True, side_effect=InterfaceError("down")), \
             mock.patch("keygrip.dbretry.base.time.sleep"):
            with self.assertRaises(InterfaceError):
                self._wrapper().connect()

    def test_non_db_error_is_not_retried(self):
        # WHY: only connectivity errors are transient; a config/programming error must fail fast.
        with mock.patch(_SUPER_CONNECT, autospec=True, side_effect=ValueError("bad config")) as sup, \
             mock.patch("keygrip.dbretry.base.time.sleep"):
            with self.assertRaises(ValueError):
                self._wrapper().connect()
        self.assertEqual(sup.call_count, 1)
