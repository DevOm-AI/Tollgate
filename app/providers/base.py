import math
from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field

# Request and response bodies follow OpenAI's chat completions API. Only the fields Tollgate
# reads are declared; anything else (temperature, tools, ...) is kept and passed through.


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "developer", "user", "assistant", "tool", "function"]
    # A string, a list of content parts, or null (an assistant message with tool calls).
    content: str | list[dict[str, Any]] | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1)
    messages: list[ChatMessage] = Field(min_length=1)
    max_tokens: int | None = Field(default=None, gt=0)
    max_completion_tokens: int | None = Field(default=None, gt=0)
    stream: bool = False
    # One answer per request: several would multiply the cost of a single call.
    n: Literal[1] = 1

    def output_cap(self, default: int) -> int:
        """Most tokens the answer may have. max_completion_tokens is OpenAI's newer name."""
        return self.max_completion_tokens or self.max_tokens or default

    def for_upstream(self, model: str, max_tokens: int, *, stream: bool) -> Self:
        """The request as a provider gets it: its own model name and one explicit cap."""
        return self.model_copy(
            update={
                "model": model,
                "max_tokens": max_tokens,
                "max_completion_tokens": None,
                "stream": stream,
            }
        )


def estimate_prompt_tokens(request: ChatCompletionRequest) -> int:
    """A rough input token count: about 4 characters per token, the rule of thumb for English."""
    chars = 0
    for message in request.messages:
        if isinstance(message.content, str):
            chars += len(message.content)
        elif message.content:
            # Only text parts count; a malformed part (e.g. "text": null) counts as nothing.
            texts = (part.get("text") for part in message.content)
            chars += sum(len(text) for text in texts if isinstance(text, str))
    return max(1, math.ceil(chars / 4))


def billable_output_tokens(prompt: int, completion: int, total: int | None) -> int:
    """Output tokens to bill, from a provider's usage numbers.

    OpenAI and Groq count reasoning inside completion_tokens. Gemini leaves its "thinking"
    tokens out of completion_tokens but counts them in total_tokens, and charges them as
    output, so the larger of the two readings is what the answer really cost.
    """
    if total is None:
        return completion
    return max(completion, total - prompt)


class Usage(BaseModel):
    model_config = ConfigDict(extra="allow")

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @property
    def output_tokens(self) -> int:
        return billable_output_tokens(self.prompt_tokens, self.completion_tokens, self.total_tokens)


class ChatCompletion(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[dict[str, Any]]
    usage: Usage


# Provider answers that blame the request itself, not the provider.
CLIENT_ERROR_STATUSES = frozenset({400, 404, 413, 422})


class ProviderError(Exception):
    """The provider couldn't answer: connection error, timeout, or an error response.

    `status_code` is the provider's HTTP status, or None if no response came back.
    `upstream_message` is the provider's own error message, if it sent one.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        upstream_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.upstream_message = upstream_message

    @property
    def is_client_error(self) -> bool:
        return self.status_code in CLIENT_ERROR_STATUSES

    @property
    def retryable(self) -> bool:
        """Worth trying another provider: no response (timeout, connection), 429, or 5xx.

        Anything else is about the request itself, and another provider would refuse it too.
        """
        code = self.status_code
        return code is None or code == 429 or code >= 500


class ProviderTimeout(ProviderError):
    """The provider didn't connect, start answering, or finish answering in time."""


class Provider(Protocol):
    """An LLM provider Tollgate forwards requests to."""

    name: str

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletion:
        """One full answer."""
        ...

    def stream(self, request: ChatCompletionRequest) -> AsyncIterator[dict[str, Any]]:
        """The answer as OpenAI chat.completion.chunk objects, in the order they arrive."""
        ...
