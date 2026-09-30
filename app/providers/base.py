from typing import Any, Literal, Protocol

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


class Usage(BaseModel):
    model_config = ConfigDict(extra="allow")

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class ChatCompletion(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[dict[str, Any]]
    usage: Usage


class ProviderError(Exception):
    """The provider couldn't answer: connection error, timeout, or an error response."""


class Provider(Protocol):
    """An LLM provider Tollgate forwards requests to."""

    name: str

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletion: ...
