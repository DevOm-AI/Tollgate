import json
from collections.abc import AsyncIterator
from typing import Any

import httpx2
from pydantic import ValidationError

from app.providers.base import (
    ChatCompletion,
    ChatCompletionRequest,
    ProviderError,
    ProviderTimeout,
)

# Longest provider error message passed back to the caller.
UPSTREAM_MESSAGE_MAX_LENGTH = 500


class OpenAICompatibleProvider:
    """A provider that serves OpenAI's chat completions API at `base_url`."""

    name: str
    base_url: str

    def __init__(self, api_key: str, client: httpx2.AsyncClient) -> None:
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = client

    @property
    def _url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _body(self, request: ChatCompletionRequest, *, stream: bool) -> dict[str, Any]:
        return request.model_copy(update={"stream": stream}).model_dump(exclude_none=True)

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletion:
        try:
            response = await self._client.post(
                self._url, json=self._body(request, stream=False), headers=self._headers
            )
        except httpx2.HTTPError as exc:
            raise self._transport_error(exc) from exc
        if response.is_error:
            raise self._status_error(response)
        try:
            return ChatCompletion.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise ProviderError(f"{self.name}: unreadable answer") from exc

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[dict[str, Any]]:
        try:
            async with self._client.stream(
                "POST", self._url, json=self._body(request, stream=True), headers=self._headers
            ) as response:
                if response.is_error:
                    await response.aread()
                    raise self._status_error(response)
                # Server-Sent Events: each chunk is a "data: {...}" line; "data: [DONE]" ends it.
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line.removeprefix("data:").strip()
                    if data == "[DONE]":
                        return
                    try:
                        yield json.loads(data)
                    except ValueError as exc:
                        raise ProviderError(f"{self.name}: unreadable stream chunk") from exc
        except httpx2.HTTPError as exc:
            raise self._transport_error(exc) from exc

    def _transport_error(self, exc: httpx2.HTTPError) -> ProviderError:
        """No usable response: a timeout (connect, read, ...) or a broken connection."""
        error = ProviderTimeout if isinstance(exc, httpx2.TimeoutException) else ProviderError
        return error(f"{self.name}: {type(exc).__name__}")

    def _status_error(self, response: httpx2.Response) -> ProviderError:
        return ProviderError(
            f"{self.name}: HTTP {response.status_code}",
            status_code=response.status_code,
            upstream_message=_error_message(response),
        )


def _error_message(response: httpx2.Response) -> str | None:
    """The message from an OpenAI-style error body: {"error": {"message": ...}}."""
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, list) and body:
        body = body[0]  # Gemini wraps some errors in a list.
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    if not isinstance(message, str):
        return None
    return message[:UPSTREAM_MESSAGE_MAX_LENGTH]
