import openai
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.providers.base import ChatCompletion, ChatCompletionRequest, ProviderError
from app.providers.catalog import get_catalog
from tests.test_admin_api import create_key

MODEL = "test-model"


class FakeProvider:
    name = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.requests: list[ChatCompletionRequest] = []

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletion:
        self.requests.append(request)
        if self.fail:
            raise ProviderError("upstream said: <the user's prompt>")
        return ChatCompletion(
            id="chatcmpl-1",
            created=1_790_000_000,
            model=request.model,
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello from fake"},
                    "finish_reason": "stop",
                }
            ],
            usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            system_fingerprint="fp_fake",
        )


@pytest.fixture
def provider(api: TestClient) -> FakeProvider:
    fake = FakeProvider()
    app.dependency_overrides[get_catalog] = lambda: {MODEL: fake}
    return fake


@pytest.fixture
def customer_key(api: TestClient) -> str:
    return create_key(api)["key"]


def chat(api: TestClient, key: str, **body) -> dict:
    payload = {"model": MODEL, "messages": [{"role": "user", "content": "Hi"}]} | body
    return api.post(
        "/v1/chat/completions", json=payload, headers={"Authorization": f"Bearer {key}"}
    )


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


def test_unknown_model_is_404(api, provider, customer_key):
    response = chat(api, customer_key, model="no-such-model")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


def test_stream_is_refused_for_now(api, provider, customer_key):
    response = chat(api, customer_key, stream=True)

    assert response.status_code == 400
    assert response.json()["error"]["param"] == "stream"


def test_provider_failure_is_502_without_its_message(api, provider, customer_key):
    provider.fail = True

    response = chat(api, customer_key)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_error"
    assert "prompt" not in response.text


def test_admin_routes_keep_fastapi_validation_errors(api):
    response = api.post("/admin/customers", json={})

    assert response.status_code == 422
    assert "detail" in response.json()
