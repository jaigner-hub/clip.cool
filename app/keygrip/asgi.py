import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "keygrip.settings.dev")

# get_asgi_application() runs django.setup() → web.apps.ready() → configure_tracing() (provider +
# psycopg/httpx). We then wrap the handler so every HTTP request gets a SERVER span; this is the
# ASGI server path, which the Django instrumentation doesn't trace (ADR 0015, keygrip/tracing.py).
from keygrip.tracing import instrument_asgi  # noqa: E402  (after settings module is set)

django_application = instrument_asgi(get_asgi_application())


async def application(scope, receive, send):
    """ASGI entrypoint.

    Django's handler only speaks HTTP, so we answer the ASGI ``lifespan`` protocol here — uvicorn
    opens a lifespan channel on worker startup, and passing it to Django raises
    ``ValueError: Django can only handle ASGI/HTTP connections, not lifespan.`` (which sentry-sdk
    then captures as a noisy error). We ack startup/shutdown as no-ops and hand everything else
    (http) to Django.
    """
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    else:
        await django_application(scope, receive, send)
