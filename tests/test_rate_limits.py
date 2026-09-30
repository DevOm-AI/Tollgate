import asyncio

import httpx2
from fastapi.testclient import TestClient

from app.main import app
from app.providers.base import ProviderError
from tests.conftest import MODEL, chat, create_key


def statuses(api: TestClient, key: str, count: int, **body) -> list[int]:
    return [chat(api, key, **body).status_code for _ in range(count)]


# --- Requests per minute ---


def test_rpm_limit_allows_exactly_the_limit_when_requests_arrive_at_once(api, provider):
    key = create_key(api, rpm_limit=5)["key"]

    async def send_all() -> list[int]:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/v1/chat/completions",
                        json={"model": MODEL, "messages": [{"role": "user", "content": "Hi"}]},
                        headers={"Authorization": f"Bearer {key}"},
                    )
                    for _ in range(20)
                )
            )
        return [response.status_code for response in responses]

    codes = asyncio.run(send_all())

    assert codes.count(200) == 5
    assert codes.count(429) == 15
    assert len(provider.requests) == 5


def test_rpm_limit_answers_429_in_openai_format(api, provider):
    key = create_key(api, rpm_limit=2)["key"]
    statuses(api, key, 2)

    response = chat(api, key)

    assert response.status_code == 429
    error = response.json()["error"]
    assert error["code"] == "rate_limit_exceeded"
    assert error["type"] == "requests"
    assert "2 requests per minute" in error["message"]


def test_limits_are_per_key(api, provider):
    first = create_key(api, rpm_limit=1)["key"]
    second = create_key(api, rpm_limit=1)["key"]

    assert statuses(api, first, 2) == [200, 429]
    assert statuses(api, second, 1) == [200]


def test_lowered_limit_applies_at_once(api, provider):
    created = create_key(api, rpm_limit=5)
    statuses(api, created["key"], 1)

    api.patch(f"/admin/keys/{created['id']}", json={"rpm_limit": 2})

    # 4 were left, but the bucket now holds at most 2.
    assert statuses(api, created["key"], 3) == [200, 200, 429]


def test_unknown_model_does_not_use_the_limit(api, provider):
    key = create_key(api, rpm_limit=1)["key"]

    chat(api, key, model="no-such-model")

    assert statuses(api, key, 1) == [200]


# --- Tokens per minute ---


def test_unused_estimate_is_given_back(api, provider):
    # Each request takes about 61 tokens up front (1 prompt + 60 max_tokens) but uses 7.
    # Without the give-back, the second request wouldn't fit in 100.
    key = create_key(api, tpm_limit=100)["key"]

    assert statuses(api, key, 3, max_tokens=60) == [200, 200, 200]


def test_tpm_limit_counts_the_estimate_before_the_answer(api, provider):
    key = create_key(api, tpm_limit=100)["key"]
    # Slow answers, so the first request still holds its estimate when the second arrives.
    provider.delay_s = 0.3

    async def send_two() -> list[int]:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/v1/chat/completions",
                        json={
                            "model": MODEL,
                            "messages": [{"role": "user", "content": "Hi"}],
                            "max_tokens": 60,
                        },
                        headers={"Authorization": f"Bearer {key}"},
                    )
                    for _ in range(2)
                )
            )
        return sorted(response.status_code for response in responses)

    # Both arrive before either answers: 61 + 61 > 100, so one must wait.
    assert asyncio.run(send_two()) == [200, 429]


def test_answer_longer_than_estimated_takes_the_difference(api, provider):
    key = create_key(api, tpm_limit=100)["key"]
    # Estimated 1 prompt + 90 max_tokens = 91; the answer really used 3 + 95 = 98.
    provider.completion_tokens = 95
    assert statuses(api, key, 1, max_tokens=90) == [200]

    # About 2 tokens are left: an estimate of 1 + 5 no longer fits.
    response = chat(api, key, max_tokens=5)

    assert response.status_code == 429
    assert response.json()["error"]["type"] == "tokens"


def test_request_larger_than_the_tpm_limit_is_refused_as_too_large(api, provider):
    key = create_key(api, tpm_limit=100, rpm_limit=1)["key"]

    response = chat(api, key, max_tokens=200)

    assert response.status_code == 429
    assert "Request too large" in response.json()["error"]["message"]
    assert provider.requests == []
    # Refused requests don't count against requests per minute.
    assert statuses(api, key, 1, max_tokens=10) == [200]


def test_failed_provider_call_gives_the_whole_estimate_back(api, provider):
    key = create_key(api, tpm_limit=100)["key"]
    provider.error = ProviderError("fake: HTTP 500", status_code=500)

    assert statuses(api, key, 3, max_tokens=90) == [502, 502, 502]

    provider.error = None
    assert statuses(api, key, 1, max_tokens=90) == [200]
