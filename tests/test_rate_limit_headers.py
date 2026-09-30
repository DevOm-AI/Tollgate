import pytest

from app.limits.bucket import TakeResult
from app.limits.rate_limit import RateLimited, rate_limit_headers, retry_after
from app.providers.base import ProviderError
from tests.conftest import chat, create_key

HEADER_NAMES = [
    f"X-RateLimit-{kind}-{limit}"
    for kind in ("Limit", "Remaining", "Reset")
    for limit in ("Requests", "Tokens")
]


def take(**overrides) -> TakeResult:
    values = {"allowed": True, "limit": 10, "remaining": 9, "retry_after_ms": 0, "reset_ms": 0}
    return TakeResult(**(values | overrides))


# --- Header values ---


def test_headers_report_both_limits():
    headers = rate_limit_headers(
        take(limit=5, remaining=4, reset_ms=12_000),
        take(limit=1000, remaining=939, reset_ms=3_660),
    )

    assert headers == {
        "X-RateLimit-Limit-Requests": "5",
        "X-RateLimit-Remaining-Requests": "4",
        "X-RateLimit-Reset-Requests": "12",
        "X-RateLimit-Limit-Tokens": "1000",
        "X-RateLimit-Remaining-Tokens": "939",
        "X-RateLimit-Reset-Tokens": "4",
    }


@pytest.mark.parametrize(("ms", "seconds"), [(1, 1), (999, 1), (1000, 1), (1001, 2)])
def test_retry_after_rounds_up_to_whole_seconds(ms: int, seconds: int):
    denied = take(allowed=False, retry_after_ms=ms)

    assert retry_after(RateLimited("requests", denied, take(), cost=1)) == seconds


def test_retry_after_is_none_when_retrying_cannot_help():
    too_large = take(allowed=False, retry_after_ms=None)

    assert retry_after(RateLimited("tokens", take(), too_large, cost=500)) is None


# --- Responses ---


def test_successful_response_carries_both_limits(api, provider):
    key = create_key(api, rpm_limit=5, tpm_limit=1000)["key"]

    response = chat(api, key, max_tokens=50)

    assert response.status_code == 200
    headers = response.headers
    assert headers["X-RateLimit-Limit-Requests"] == "5"
    assert headers["X-RateLimit-Remaining-Requests"] == "4"
    assert headers["X-RateLimit-Limit-Tokens"] == "1000"
    # The fake's answer uses 7 tokens; the rest of the estimate was given back (plus what
    # refilled while the test ran: 1000 per minute is about 1 every 60 ms).
    assert 993 <= int(headers["X-RateLimit-Remaining-Tokens"]) <= 1000
    assert int(headers["X-RateLimit-Reset-Requests"]) > 0
    assert "Retry-After" not in headers


def test_remaining_requests_count_down(api, provider):
    key = create_key(api, rpm_limit=3)["key"]

    remaining = [chat(api, key).headers["X-RateLimit-Remaining-Requests"] for _ in range(3)]

    assert remaining == ["2", "1", "0"]


def test_rpm_429_has_retry_after_and_both_limits(api, provider):
    # 2 per minute refills one request every 30 s.
    key = create_key(api, rpm_limit=2)["key"]
    chat(api, key)
    chat(api, key)

    response = chat(api, key)

    assert response.status_code == 429
    assert 1 <= int(response.headers["Retry-After"]) <= 30
    assert response.headers["X-RateLimit-Remaining-Requests"] == "0"
    assert all(name in response.headers for name in HEADER_NAMES)


def test_tpm_429_has_retry_after_and_keeps_the_request(api, provider):
    key = create_key(api, rpm_limit=5, tpm_limit=100)["key"]
    provider.completion_tokens = 95
    chat(api, key, max_tokens=90)

    response = chat(api, key, max_tokens=10)

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1
    # The refused request gave its request back: only the first one counts.
    assert response.headers["X-RateLimit-Remaining-Requests"] == "4"


def test_too_large_429_has_no_retry_after(api, provider):
    key = create_key(api, tpm_limit=100)["key"]

    response = chat(api, key, max_tokens=500)

    assert response.status_code == 429
    assert "Retry-After" not in response.headers
    assert response.headers["X-RateLimit-Remaining-Tokens"] == "100"


def test_too_large_429_has_no_retry_after_even_with_requests_used_up(api, provider):
    key = create_key(api, rpm_limit=1, tpm_limit=100)["key"]
    chat(api, key, max_tokens=10)

    response = chat(api, key, max_tokens=500)

    assert response.status_code == 429
    assert response.json()["error"]["type"] == "tokens"
    assert "Request too large" in response.json()["error"]["message"]
    assert "Retry-After" not in response.headers
    assert response.headers["X-RateLimit-Remaining-Requests"] == "0"
    assert all(name in response.headers for name in HEADER_NAMES)


def test_provider_failure_still_reports_the_limits(api, provider):
    key = create_key(api, tpm_limit=1000)["key"]
    provider.error = ProviderError("fake: HTTP 500", status_code=500)

    response = chat(api, key, max_tokens=50)

    assert response.status_code == 502
    assert response.headers["X-RateLimit-Remaining-Tokens"] == "1000"
    assert all(name in response.headers for name in HEADER_NAMES)


def test_unauthenticated_response_has_no_limits(api, provider):
    response = chat(api, "tg_live_unknown")

    assert response.status_code == 401
    assert not any(name in response.headers for name in HEADER_NAMES)
