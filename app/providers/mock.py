import asyncio
import math
import random
import time
import uuid
from collections.abc import AsyncIterator
from itertools import cycle, islice
from typing import Any

from app.providers.base import ChatCompletion, ChatCompletionRequest, ProviderError

WORDS = "tollgate checks the key the limit and the budget then forwards the request".split()


def estimate_prompt_tokens(request: ChatCompletionRequest) -> int:
    """About 4 characters per token, the usual rule of thumb for English text."""
    chars = 0
    for message in request.messages:
        if isinstance(message.content, str):
            chars += len(message.content)
        elif message.content:
            chars += sum(len(part.get("text", "")) for part in message.content)
    return max(1, math.ceil(chars / 4))


class MockProvider:
    """Fake answers of one word per token. Costs nothing: every test and load test uses it.

    `delay_ms` is how long a whole answer takes, `output_tokens` its length (cut to the
    request's max_tokens), and `error_rate` the share of requests that fail like a 503.
    """

    name = "mock"

    def __init__(
        self,
        *,
        delay_ms: int = 0,
        output_tokens: int = 32,
        error_rate: float = 0.0,
        rng: random.Random | None = None,
    ) -> None:
        self.delay_ms = delay_ms
        self.output_tokens = output_tokens
        self.error_rate = error_rate
        self._rng = rng or random.Random()

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletion:
        self._maybe_fail()
        await asyncio.sleep(self.delay_ms / 1000)
        words, finish_reason = self._answer(request)
        return ChatCompletion(
            id=_completion_id(),
            created=int(time.time()),
            model=request.model,
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": " ".join(words)},
                    "finish_reason": finish_reason,
                }
            ],
            usage=_usage(request, len(words)),
        )

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[dict[str, Any]]:
        self._maybe_fail()
        words, finish_reason = self._answer(request)
        chunk = {
            "id": _completion_id(),
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": request.model,
        }
        pause = self.delay_ms / 1000 / len(words)
        for i, word in enumerate(words):
            await asyncio.sleep(pause)
            delta = {"role": "assistant", "content": word} if i == 0 else {"content": f" {word}"}
            yield chunk | {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        yield chunk | {"choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]}
        # Like OpenAI: usage comes in a last, choice-less chunk only when asked for.
        stream_options = (request.model_extra or {}).get("stream_options") or {}
        if stream_options.get("include_usage"):
            yield chunk | {"choices": [], "usage": _usage(request, len(words))}

    def _maybe_fail(self) -> None:
        if self._rng.random() < self.error_rate:
            raise ProviderError("mock: simulated failure", status_code=503)

    def _answer(self, request: ChatCompletionRequest) -> tuple[list[str], str]:
        cap = request.output_cap(self.output_tokens)
        count = min(self.output_tokens, cap)
        finish_reason = "length" if cap < self.output_tokens else "stop"
        return list(islice(cycle(WORDS), count)), finish_reason


def _completion_id() -> str:
    return f"chatcmpl-mock-{uuid.uuid4().hex}"


def _usage(request: ChatCompletionRequest, completion_tokens: int) -> dict[str, int]:
    prompt_tokens = estimate_prompt_tokens(request)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
