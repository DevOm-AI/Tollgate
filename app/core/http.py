import httpx2

from app.core.config import Settings, get_settings


def provider_timeout(settings: Settings) -> httpx2.Timeout:
    """Connecting gets its own short limit; the total limit backs up every other wait.

    The first-token and total limits themselves are enforced around each call (see
    app/api/chat.py), so they hold for every provider, not just HTTP ones.
    """
    return httpx2.Timeout(
        settings.provider_total_timeout_s, connect=settings.provider_connect_timeout_s
    )


# One pooled client for every provider call. Lazy: nothing connects until the first request.
http_client = httpx2.AsyncClient(timeout=provider_timeout(get_settings()))
