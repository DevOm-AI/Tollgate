import json
import uuid

import httpx2
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import DEFAULT_ROUTES, Settings
from app.main import app
from app.models import RequestLog
from app.providers.catalog import Catalog, build_catalog, get_catalog
from tests.conftest import FAKE_PRICE, FakeProvider, chat, create_key


def settings(**overrides) -> Settings:
    keys = {"groq_api_key": None, "gemini_api_key": None}
    return Settings(_env_file=None, **(keys | overrides))


# --- Settings ---


def test_default_route_is_fast_chat_groq_then_gemini():
    assert settings().routes == DEFAULT_ROUTES
    assert DEFAULT_ROUTES["fast-chat"][0].startswith("groq/")
    assert DEFAULT_ROUTES["fast-chat"][1].startswith("gemini/")


def test_routes_are_read_from_the_environment_as_json(monkeypatch):
    monkeypatch.setenv("ROUTES", json.dumps({"cheap": ["mock"], "smart": ["gemini/pro"]}))

    assert Settings(_env_file=None).routes == {"cheap": ["mock"], "smart": ["gemini/pro"]}


@pytest.mark.parametrize(
    "routes",
    [
        {"groq/x": ["mock"]},  # looks like a model
        {"mock": ["groq/x"]},
        {"": ["mock"]},
        {"empty": []},
        {"chained": ["fast-chat"]},  # routes can't point at routes
    ],
)
def test_invalid_routes_are_refused(routes):
    with pytest.raises(ValidationError):
        settings(routes=routes)


# --- Catalog ---


def test_route_resolves_to_its_models_in_order():
    catalog = build_catalog(
        settings(groq_api_key=SecretStr("g"), gemini_api_key=SecretStr("m")), httpx2.AsyncClient()
    )

    candidates = catalog.candidates("fast-chat")

    assert [(c.provider.name, c.upstream_model) for c in candidates] == [
        ("groq", "openai/gpt-oss-20b"),
        ("gemini", "gemini-flash-latest"),
    ]


def test_route_skips_providers_that_are_not_set_up():
    catalog = build_catalog(settings(gemini_api_key=SecretStr("m")), httpx2.AsyncClient())

    assert [c.provider.name for c in catalog.candidates("fast-chat")] == ["gemini"]


def test_route_with_no_provider_set_up_has_no_candidates():
    catalog = build_catalog(settings(), httpx2.AsyncClient())

    assert catalog.candidates("fast-chat") == []
    assert catalog.is_route("fast-chat")


def test_plain_model_names_still_work():
    catalog = build_catalog(settings(), httpx2.AsyncClient())

    assert [c.provider.name for c in catalog.candidates("mock")] == ["mock"]
    assert catalog.candidates("unknown/model") == []
    assert not catalog.is_route("mock")


# --- Endpoint ---


@pytest.fixture
def two_fakes(api):
    primary, fallback = FakeProvider(), FakeProvider()
    primary.name, fallback.name = "fake", "fake2"
    for provider in (primary, fallback):
        response = api.put("/admin/prices", json=FAKE_PRICE | {"provider": provider.name})
        assert response.status_code == 200
    app.dependency_overrides[get_catalog] = lambda: Catalog(
        [primary, fallback], {"fake-chat": ["fake/test-model", "fake2/test-model"]}
    )
    return primary, fallback


def test_customer_asks_for_a_route_and_the_primary_answers(api, two_fakes, db_engine):
    primary, fallback = two_fakes
    created = create_key(api)

    response = chat(api, created["key"], model="fake-chat")

    assert response.status_code == 200
    assert primary.requests[0].model == "test-model"
    assert fallback.requests == []
    with Session(db_engine) as session:
        request = session.scalars(
            select(RequestLog).where(RequestLog.key_id == uuid.UUID(created["id"]))
        ).one()
    assert (request.model, request.provider) == ("fake-chat", "fake")


def test_route_with_no_provider_set_up_is_503(api):
    app.dependency_overrides[get_catalog] = lambda: Catalog([], {"fast-chat": ["groq/x"]})
    key = create_key(api)["key"]

    response = chat(api, key, model="fast-chat")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_unavailable"


def test_unknown_name_is_still_404(api, two_fakes):
    key = create_key(api)["key"]

    assert chat(api, key, model="slow-chat").status_code == 404
