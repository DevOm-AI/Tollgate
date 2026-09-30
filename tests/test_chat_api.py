import openai
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.http import http_client
from app.main import app
from app.providers.base import ProviderError
from app.providers.catalog import build_catalog, get_catalog
from tests.conftest import MODEL, chat, create_key


def test_openai_library_works_against_tollgate(api, provider, customer_key):
    client = openai.OpenAI(
        base_url="http://testserver/v1",
        api_key=customer_key,
        http_client=TestClient(app),
        max_retries=0,
    )

    completion = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "Say hello"}],
        max_tokens=16,
        temperature=0.2,
    )

    assert completion.choices[0].message.content == "Hello from fake"
    assert completion.usage.total_tokens == 7
    sent = provider.requests[0]
    assert sent.max_tokens == 16
    assert sent.model_extra == {"temperature": 0.2}


def test_openai_library_reads_tollgate_errors(api, provider):
    client = openai.OpenAI(
        base_url="http://testserver/v1",
        api_key="tg_live_not-a-real-key",
        http_client=TestClient(app),
        max_retries=0,
    )

    with pytest.raises(openai.AuthenticationError) as exc_info:
        client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "Hi"}])

    assert exc_info.value.code == "invalid_api_key"


def test_answer_is_passed_through_with_unknown_fields(api, provider, customer_key):
    response = chat(api, customer_key)

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "Hello from fake"
    assert body["system_fingerprint"] == "fp_fake"


def test_request_fields_tollgate_does_not_read_are_passed_on(api, provider, customer_key):
    chat(
        api,
        customer_key,
        messages=[{"role": "user", "content": [{"type": "text", "text": "Hi"}], "name": "om"}],
        tools=[{"type": "function", "function": {"name": "f"}}],
    )

    sent = provider.requests[0]
    assert sent.model_extra == {"tools": [{"type": "function", "function": {"name": "f"}}]}
    assert sent.messages[0].model_extra == {"name": "om"}


# --- Customer keys ---


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer tg_live_unknown"}, {"Authorization": "Basic dXNlcjpwYXNz"}],
)
def test_request_needs_a_valid_key(api, provider, headers: dict):
    # api's default headers carry the admin key; drop it so only `headers` is sent.
    client = TestClient(app, headers=headers)

    response = client.post(
        "/v1/chat/completions", json={"model": MODEL, "messages": [{"role": "user"}]}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert provider.requests == []


def test_admin_key_is_not_a_customer_key(api, provider):
    # The api client sends the admin key by default.
    response = api.post(
        "/v1/chat/completions", json={"model": MODEL, "messages": [{"role": "user"}]}
    )

    assert response.status_code == 401


def test_revoked_key_is_refused(api, provider):
    created = create_key(api)
    api.post(f"/admin/keys/{created['id']}/revoke")

    response = chat(api, created["key"])

    assert response.status_code == 401
    assert provider.requests == []


def test_bad_body_with_bad_key_is_401_not_400(api, provider):
    response = chat(api, "tg_live_unknown", messages=[])

    assert response.status_code == 401


# --- Validation ---


@pytest.mark.parametrize(
    ("body", "param"),
    [
        ({"model": ""}, "model"),
        ({"messages": []}, "messages"),
        ({"messages": [{"role": "robot", "content": "Hi"}]}, "messages.0.role"),
        ({"max_tokens": 0}, "max_tokens"),
        ({"n": 2}, "n"),
    ],
)
def test_invalid_request_is_400_in_openai_format(api, provider, customer_key, body, param):
    response = chat(api, customer_key, **body)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["param"] == param
    assert provider.requests == []


def test_validation_error_does_not_echo_the_prompt(api, provider, customer_key):
    response = chat(api, customer_key, messages=[{"role": "robot", "content": "my secret prompt"}])

    assert "my secret prompt" not in response.text


def test_malformed_content_part_is_not_a_server_error(api, provider, customer_key):
    content = [{"type": "text", "text": None}, {"type": "text", "text": "Hi"}]

    response = chat(api, customer_key, messages=[{"role": "user", "content": content}])

    assert response.status_code == 200


def test_unknown_model_is_404(api, provider, customer_key):
    response = chat(api, customer_key, model="no-such-model")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


def test_provider_failure_is_502_without_its_message(api, provider, customer_key):
    provider.error = ProviderError("fake: HTTP 500", status_code=500, upstream_message="prompt")

    response = chat(api, customer_key)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_error"
    assert "prompt" not in response.text


def test_provider_connection_failure_is_502(api, provider, customer_key):
    provider.error = ProviderError("fake: ConnectError")

    assert chat(api, customer_key).status_code == 502


@pytest.mark.parametrize("status_code", [400, 404, 422])
def test_provider_rejecting_the_request_is_400_with_its_reason(
    api, provider, customer_key, status_code
):
    provider.error = ProviderError(
        "fake: HTTP", status_code=status_code, upstream_message="model 'x' not found"
    )

    response = chat(api, customer_key)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "provider_rejected_request"
    assert "model 'x' not found" in error["message"]


# --- Models and token caps ---


def test_provider_gets_its_own_model_name(api, provider, customer_key):
    chat(api, customer_key)

    assert provider.requests[0].model == "test-model"


def test_default_max_tokens_is_applied(api, provider, customer_key):
    chat(api, customer_key)

    assert provider.requests[0].max_tokens == Settings(_env_file=None).default_max_tokens


def test_max_completion_tokens_becomes_the_cap(api, provider, customer_key):
    chat(api, customer_key, max_completion_tokens=50)

    sent = provider.requests[0]
    assert (sent.max_tokens, sent.max_completion_tokens) == (50, None)


def test_mock_model_answers_through_the_openai_library(api, customer_key):
    app.dependency_overrides[get_catalog] = lambda: build_catalog(
        Settings(_env_file=None, mock_output_tokens=5), http_client
    )
    client = openai.OpenAI(
        base_url="http://testserver/v1",
        api_key=customer_key,
        http_client=TestClient(app),
        max_retries=0,
    )

    completion = client.chat.completions.create(
        model="mock", messages=[{"role": "user", "content": "Say hello"}]
    )

    assert len(completion.choices[0].message.content.split()) == 5
    assert completion.usage.completion_tokens == 5


def test_admin_routes_keep_fastapi_validation_errors(api):
    response = api.post("/admin/customers", json={})

    assert response.status_code == 422
    assert "detail" in response.json()
