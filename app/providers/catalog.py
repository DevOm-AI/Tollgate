from dataclasses import dataclass
from functools import lru_cache

import httpx2

from app.core.config import Settings, get_settings
from app.core.http import http_client
from app.providers.base import Provider
from app.providers.gemini import GeminiProvider
from app.providers.groq import GroqProvider
from app.providers.mock import MockProvider

MOCK_MODEL = "mock"


@dataclass(frozen=True)
class ResolvedModel:
    provider: Provider
    # The model name the provider knows it by.
    upstream_model: str


class Catalog:
    """Which providers serve a model name.

    "mock" is the mock provider. "<provider>/<model>" (e.g. groq/openai/gpt-oss-20b) is
    that provider's model, for each provider whose API key is set. A route name (e.g.
    fast-chat) is its list of models, in order, skipping those whose provider isn't set up.
    """

    def __init__(
        self, providers: list[Provider], routes: dict[str, list[str]] | None = None
    ) -> None:
        self.providers = {provider.name: provider for provider in providers}
        self.routes = routes or {}

    def is_route(self, model: str) -> bool:
        return model in self.routes

    def candidates(self, model: str) -> list[ResolvedModel]:
        """The models to try for `model`, primary first. Empty if none can serve it."""
        targets = self.routes.get(model, [model])
        return [resolved for target in targets if (resolved := self.resolve(target))]

    def resolve(self, model: str) -> ResolvedModel | None:
        """One model name ("mock" or "<provider>/<model>"), not a route."""
        if model == MOCK_MODEL:
            provider = self.providers.get(MOCK_MODEL)
            return ResolvedModel(provider, MOCK_MODEL) if provider else None
        name, _, upstream_model = model.partition("/")
        provider = self.providers.get(name)
        if provider is None or name == MOCK_MODEL or not upstream_model:
            return None
        return ResolvedModel(provider, upstream_model)


def build_catalog(settings: Settings, client: httpx2.AsyncClient) -> Catalog:
    providers: list[Provider] = [
        MockProvider(
            delay_ms=settings.mock_delay_ms,
            output_tokens=settings.mock_output_tokens,
            error_rate=settings.mock_error_rate,
        )
    ]
    if settings.groq_api_key is not None:
        providers.append(GroqProvider(settings.groq_api_key.get_secret_value(), client))
    if settings.gemini_api_key is not None:
        providers.append(GeminiProvider(settings.gemini_api_key.get_secret_value(), client))
    return Catalog(providers, settings.routes)


@lru_cache
def get_catalog() -> Catalog:
    return build_catalog(get_settings(), http_client)
