from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import OpenAIError
from app.core.db import get_db
from app.core.security import bearer, hash_api_key
from app.models import ApiKey
from app.providers.base import ChatCompletion, ChatCompletionRequest, Provider, ProviderError
from app.providers.catalog import get_catalog

router = APIRouter(prefix="/v1", tags=["openai"])


async def require_customer_key(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApiKey:
    """The active key the request was sent with. Looked up by hash; the key itself isn't kept."""
    if credentials is None:
        raise _invalid_key("Missing API key. Send it as: Authorization: Bearer tg_live_...")
    key = await db.scalar(
        select(ApiKey).where(ApiKey.key_hash == hash_api_key(credentials.credentials))
    )
    if key is None or not key.is_active:
        raise _invalid_key("Incorrect or revoked API key")
    return key


def _invalid_key(message: str) -> OpenAIError:
    return OpenAIError(
        status.HTTP_401_UNAUTHORIZED,
        message,
        code="invalid_api_key",
        headers={"WWW-Authenticate": "Bearer"},
    )


@router.post("/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    key: Annotated[ApiKey, Depends(require_customer_key)],
    catalog: Annotated[dict[str, Provider], Depends(get_catalog)],
) -> ChatCompletion:
    """Same request and response as OpenAI's POST /v1/chat/completions."""
    if body.stream:
        raise OpenAIError(status.HTTP_400_BAD_REQUEST, "Streaming is not supported", param="stream")
    provider = catalog.get(body.model)
    if provider is None:
        raise OpenAIError(
            status.HTTP_404_NOT_FOUND,
            f"The model '{body.model}' does not exist",
            code="model_not_found",
            param="model",
        )
    try:
        return await provider.complete(body)
    except ProviderError as exc:
        # The provider's own message may echo the prompt, so it stays out of the response.
        raise OpenAIError(
            status.HTTP_502_BAD_GATEWAY,
            f"The provider '{provider.name}' failed to answer",
            type="api_error",
            code="provider_error",
        ) from exc
