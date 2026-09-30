from app.providers.base import Provider


def get_catalog() -> dict[str, Provider]:
    """Model name (as customers send it) -> the provider that serves it."""
    return {}
