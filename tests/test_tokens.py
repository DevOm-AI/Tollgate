import anyio
import pytest

from app.providers import tokens
from app.providers.base import ChatCompletionRequest
from app.providers.tokens import count_prompt_tokens, count_text_tokens, count_usage


def request(*contents) -> ChatCompletionRequest:
    return ChatCompletionRequest.model_validate(
        {"model": "m", "messages": [{"role": "user", "content": c} for c in contents]}
    )


@pytest.mark.parametrize(
    ("text", "count"),
    [("", 0), ("Hello from fake stream", 4), ("tollgate", 3)],
)
def test_text_is_counted_with_the_tokenizer(text: str, count: int):
    assert count_text_tokens(text) == count


def test_special_token_text_is_counted_as_plain_text():
    # Must not raise, and must count more than the single special token it resembles.
    assert count_text_tokens("<|endoftext|>") > 1


def test_prompt_adds_the_chat_format_overhead():
    parts = [{"type": "text", "text": "Hello from"}, {"type": "image_url", "image_url": {}}]

    assert count_prompt_tokens(request("Hi")) == 3 + 3 + count_text_tokens("Hi")
    assert count_prompt_tokens(request(parts, "Hi")) == 3 + 6 + 2 + 1


def test_counts_fall_back_to_characters_without_the_tokenizer(monkeypatch):
    monkeypatch.setattr(tokens, "_encoding", lambda: None)

    assert count_text_tokens("a" * 10) == 3
    assert count_text_tokens("") == 0


def test_usage_is_counted_off_the_event_loop():
    assert anyio.run(count_usage, request("Hi"), "Hello from fake stream") == (7, 4)
