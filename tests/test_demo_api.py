import json
import uuid

import pytest
from pydantic import SecretStr

from app.core.config import Settings, get_settings
from app.main import app
from app.providers.catalog import Catalog, get_catalog
from app.providers.mock import MockProvider
from tests.conftest import ADMIN_KEY, create_key


@pytest.fixture
def demo(api):
    """A demo key ($0.10, 5/min) wired up as DEMO_API_KEY, with the mock as its model."""
    created = create_key(api, rpm_limit=5, tpm_limit=20_000, monthly_budget_micros=100_000)
    mock = MockProvider(output_tokens=8)
    app.dependency_overrides[get_catalog] = lambda: Catalog([mock])
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, admin_api_key=ADMIN_KEY, demo_api_key=SecretStr(created["key"])
    )
    return created


def ask(api, prompt: str = "Say hello"):
    # No Authorization header: the playground is public.
    return api.post("/demo/chat", json={"prompt": prompt}, headers={"Authorization": ""})


def events(response) -> list:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.split("\n\n")
        if line and line != "data: [DONE]"
    ]


def test_status_shows_budget_limits_and_price(api, demo):
    status = api.get("/demo/status", headers={"Authorization": ""}).json()

    assert status["model"] == "mock"
    assert status["budget_micros"] == 100_000
    assert status["remaining_micros"] == 100_000
    assert (status["rpm_limit"], status["requests_left"]) == (5, 5)
    assert (status["input_micros_per_1k"], status["output_micros_per_1k"]) == (100, 400)


def test_prompt_streams_an_answer_with_usage(api, demo):
    response = ask(api)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    chunks = events(response)
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert len(text.split()) == 8
    assert chunks[-1]["usage"]["completion_tokens"] == 8
    assert response.headers["X-RateLimit-Remaining-Requests"] == "4"


def test_answer_is_charged_to_the_demo_budget(api, demo):
    ask(api)

    status = api.get("/demo/status", headers={"Authorization": ""}).json()

    assert 0 < status["spent_micros"] < 100_000
    assert status["remaining_micros"] == 100_000 - status["spent_micros"]
    assert status["requests_left"] == 4


def test_demo_rate_limit_applies(api, demo):
    codes = [ask(api).status_code for _ in range(6)]

    assert codes == [200] * 5 + [429]


def test_empty_demo_budget_is_402(api):
    created = create_key(api, monthly_budget_micros=0)
    app.dependency_overrides[get_catalog] = lambda: Catalog([MockProvider()])
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, admin_api_key=ADMIN_KEY, demo_api_key=SecretStr(created["key"])
    )

    response = ask(api)

    assert response.status_code == 402
    assert response.json()["error"]["code"] == "budget_exceeded"


def test_visitor_cannot_pick_the_model_or_the_length(api, demo):
    response = api.post(
        "/demo/chat",
        json={"prompt": "Hi", "model": "groq/expensive", "max_tokens": 100_000},
        headers={"Authorization": ""},
    )

    assert response.status_code == 200
    usage = events(response)[-1]["usage"]
    assert usage["completion_tokens"] == 8


@pytest.mark.parametrize("prompt", ["", "x" * 2001])
def test_prompt_size_is_limited(api, demo, prompt):
    assert ask(api, prompt).status_code == 422


def test_playground_is_off_without_a_demo_key(api):
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, admin_api_key=ADMIN_KEY, demo_api_key=None
    )

    assert api.get("/demo/status").status_code == 404
    assert ask(api).status_code == 404


def test_revoked_demo_key_turns_the_playground_off(api, demo):
    api.post(f"/admin/keys/{demo['id']}/revoke")

    assert ask(api).status_code == 503


def test_unknown_demo_key_is_503(api):
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        admin_api_key=ADMIN_KEY,
        demo_api_key=SecretStr(f"tg_live_{uuid.uuid4().hex}"),
    )

    assert api.get("/demo/status").status_code == 503
