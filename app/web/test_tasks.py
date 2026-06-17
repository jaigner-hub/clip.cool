"""Procrastinate task scaffolding (ADR 0008).

These run under the dev/test SQLite settings where the Procrastinate Django app is NOT installed
(it's Postgres-only, enabled in settings/prod). That's intentional: a task is a Procrastinate
Task object that is directly callable, so we can assert its *behavior* without a queue, a worker,
or a Postgres connection. End-to-end deferral is exercised against real Postgres on the deployed
stack, not here.
"""
from django.test import TestCase
from procrastinate.contrib.django import app

from web.tasks import health_check, heartbeat


class HealthCheckTaskTests(TestCase):
    def test_health_check_runs_and_returns_result(self):
        # WHY: proves the task body itself is correct/JSON-able independent of the queue, so a
        # green CI here means a deferred job's only remaining failure mode is infrastructure.
        self.assertEqual(health_check("ping"), {"ok": True, "note": "ping"})

    def test_health_check_default_queue(self):
        # WHY: the worker is started with an explicit queue allow-list; a task silently landing
        # on the wrong queue would never be picked up. Pin the routing in a test.
        self.assertEqual(health_check.queue, "default")

    def test_heartbeat_is_periodic(self):
        # WHY: the heartbeat must stay registered as a periodic task — if the @app.periodic
        # decorator is dropped, monitoring goes quiet with NO error to notice. Assert the cron
        # registration actually exists in the registry (not just that the task is importable).
        registered = {
            pt.task.name: pt.cron for pt in app.periodic_registry.periodic_tasks.values()
        }
        self.assertEqual(registered.get(heartbeat.name), "*/5 * * * *")
