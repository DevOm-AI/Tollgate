from app.providers.base import ChatCompletionRequest

# Tokens a provider may add around each message (role, separators). Generous on purpose.
MESSAGE_OVERHEAD_TOKENS = 8


def cost_micros(
    input_tokens: int, output_tokens: int, input_micros_per_1k: int, output_micros_per_1k: int
) -> int:
    """Cost in micro-dollars, in whole-number math. Rounded up: a fraction is never free."""
    total = input_tokens * input_micros_per_1k + output_tokens * output_micros_per_1k
    # Integer ceiling division: floats would lose cents on large totals.
    return -(-total // 1000)


def max_prompt_tokens(request: ChatCompletionRequest) -> int:
    """An upper bound on the prompt's tokens, for reserving the worst case.

    Byte-level tokenizers never make more than one token per byte of text, so the UTF-8 byte
    count (plus per-message overhead) bounds the prompt. It over-reserves (English runs about
    4 bytes per token), which only holds more money for the seconds a request runs;
    under-reserving could let a key overspend. Content it can't see (e.g. images) is covered
    by settle, which never bills more than was reserved.
    """
    total = 0
    for message in request.messages:
        total += MESSAGE_OVERHEAD_TOKENS
        if isinstance(message.content, str):
            total += len(message.content.encode())
        elif message.content:
            texts = (part.get("text") for part in message.content)
            total += sum(len(text.encode()) for text in texts if isinstance(text, str))
    return total
