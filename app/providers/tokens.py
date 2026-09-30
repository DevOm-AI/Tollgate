"""Counting tokens ourselves, for providers that don't report usage.

Providers each have their own tokenizer; cl100k_base (OpenAI's) is a close, well-known stand-in
for billing when a provider's own count is missing. Loading it reads a vocabulary file
(downloaded once, then cached; the Docker image ships it). If it can't be loaded, counts fall
back to about 4 characters per token.
"""

import logging
import math
from functools import lru_cache

import anyio
import tiktoken

from app.providers.base import ChatCompletionRequest

logger = logging.getLogger(__name__)

ENCODING = "cl100k_base"
# OpenAI's chat format adds about 3 tokens per message and 3 to prime the answer.
TOKENS_PER_MESSAGE = 3
TOKENS_PER_REPLY = 3


@lru_cache(maxsize=1)
def _encoding() -> tiktoken.Encoding | None:
    try:
        return tiktoken.get_encoding(ENCODING)
    except Exception as exc:
        logger.warning("Tokenizer unavailable, estimating tokens from characters: %s", exc)
        return None


def count_text_tokens(text: str) -> int:
    if not text:
        return 0
    encoding = _encoding()
    if encoding is None:
        return math.ceil(len(text) / 4)
    # Text that looks like a special token (e.g. "<|endoftext|>") is counted as plain text.
    return len(encoding.encode(text, disallowed_special=()))


def count_prompt_tokens(request: ChatCompletionRequest) -> int:
    total = TOKENS_PER_REPLY
    for message in request.messages:
        total += TOKENS_PER_MESSAGE
        if isinstance(message.content, str):
            total += count_text_tokens(message.content)
        elif message.content:
            texts = (part.get("text") for part in message.content)
            total += sum(count_text_tokens(text) for text in texts if isinstance(text, str))
    return total


async def count_usage(request: ChatCompletionRequest, output: str) -> tuple[int, int]:
    """(prompt, output) tokens, counted in a worker thread: loading the tokenizer and
    encoding long text both block, and the event loop must keep serving other requests."""
    return await anyio.to_thread.run_sync(
        lambda: (count_prompt_tokens(request), count_text_tokens(output))
    )
