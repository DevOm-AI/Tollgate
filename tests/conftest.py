import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import Engine, create_engine, make_url, pool, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.core.redis import get_redis
from app.main import app
from app.providers.base import ChatCompletion, ChatCompletionRequest, ProviderError
from app.providers.catalog import Catalog, get_catalog

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def alembic_config(connection) -> Config:
    config = Config(ALEMBIC_INI)
    config.attributes["connection"] = connection
    return config


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    """A throwaway database, migrated to head, dropped after the test run.

    The server comes from DATABASE_URL, else the docker compose Postgres as seen from the
    host. .env is skipped: its DATABASE_URL names the compose service, which only resolves
    inside the compose network.
    """
    server_url = make_url(Settings(_env_file=None).database_url)
    name = f"tollgate_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(server_url, isolation_level="AUTOCOMMIT", poolclass=pool.NullPool)
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))

    engine = create_engine(server_url.set(database=name), poolclass=pool.NullPool)
    try:
        with engine.begin() as connection:
            command.upgrade(alembic_config(connection), "head")
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def db(db_engine: Engine) -> Iterator[Session]:
    """A session whose work is rolled back after the test, commits included."""
    with db_engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            session.close()
            transaction.rollback()


ADMIN_KEY = "test-admin-key-0123456789abcdefghijklmnop"


async def reachable_redis() -> AsyncIterator[Redis]:
    """The test Redis (REDIS_URL, else the compose Redis), as a get_redis override.

    A client per request: each TestClient request runs on its own event loop, and a pooled
    connection can't move between loops.
    """
    redis = Redis.from_url(Settings(_env_file=None).redis_url)
    try:
        yield redis
    finally:
        await redis.aclose()


@pytest.fixture
def api(db_engine: Engine) -> Iterator[TestClient]:
    """The app on the scratch database and the test Redis, with ADMIN_KEY as its admin key.

    Requests commit for real (the scratch database is dropped at the end), so tests make
    their own rows instead of assuming empty tables.
    """
    async_engine = create_async_engine(db_engine.url, poolclass=pool.NullPool)
    sessions = async_sessionmaker(bind=async_engine, autoflush=False, expire_on_commit=False)

    async def get_test_db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = get_test_db
    app.dependency_overrides[get_redis] = reachable_redis
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, admin_api_key=ADMIN_KEY
    )
    try:
        # Not entered as a context manager: that would run the lifespan, whose shutdown
        # closes the app's shared Redis client and engine for the tests that follow.
        yield TestClient(app, headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    finally:
        app.dependency_overrides.clear()


LIMITS = {"rpm_limit": 60, "tpm_limit": 100_000, "monthly_budget_micros": 1_000_000}


def create_customer(api: TestClient, **body) -> dict:
    response = api.post("/admin/customers", json={"name": "Acme"} | body)
    assert response.status_code == 201, response.text
    return response.json()


def create_key(api: TestClient, **limits) -> dict:
    customer = create_customer(api)
    response = api.post(f"/admin/customers/{customer['id']}/keys", json=LIMITS | limits)
    assert response.status_code == 201, response.text
    return response.json()


MODEL = "fake/test-model"
# What the fake provider streams, one chunk per word.
STREAM_WORDS = ["Hello", " from", " fake", " stream"]
# Micro-dollars per 1,000 tokens: $1 per million input tokens, $2 per million output tokens.
FAKE_PRICE = {
    "provider": "fake",
    "model": "test-model",
    "input_micros_per_1k": 1000,
    "output_micros_per_1k": 2000,
}


class FakeProvider:
    """Answers every request with 3 prompt tokens and `completion_tokens` output tokens."""

    name = "fake"

    def __init__(self) -> None:
        self.error: ProviderError | None = None
        self.completion_tokens = 4
        self.delay_s = 0.0
        # Streaming: pause between chunks, whether usage is sent when asked for, and the
        # chunk before which the stream breaks (None: it doesn't).
        self.chunk_delay_s = 0.0
        self.send_usage = True
        self.break_before_chunk: int | None = None
        # Open with a role-only chunk, then pause this long before the first word.
        self.role_chunk_then_pause_s: float | None = None
        # How far the last stream got: chunks sent, and whether it ran to the end.
        self.streamed_chunks = 0
        self.stream_completed = False
        self.requests: list[ChatCompletionRequest] = []

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletion:
        self.requests.append(request)
        await asyncio.sleep(self.delay_s)
        if self.error:
            raise self.error
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
            usage={
                "prompt_tokens": 3,
                "completion_tokens": self.completion_tokens,
                "total_tokens": 3 + self.completion_tokens,
            },
            system_fingerprint="fp_fake",
        )

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[dict]:
        """Streams STREAM_WORDS one chunk each, then a finish chunk, then usage if asked."""
        self.requests.append(request)
        await asyncio.sleep(self.delay_s)
        if self.error:
            raise self.error
        base = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1}
        base["model"] = request.model
        self.streamed_chunks = 0
        self.stream_completed = False
        if self.role_chunk_then_pause_s is not None:
            role_only = {"role": "assistant", "content": ""}
            yield base | {"choices": [{"index": 0, "delta": role_only, "finish_reason": None}]}
            await asyncio.sleep(self.role_chunk_then_pause_s)
        for i, word in enumerate(STREAM_WORDS):
            if i == self.break_before_chunk:
                raise ProviderError("fake: ReadError")
            delta = {"content": word} | ({"role": "assistant"} if i == 0 else {})
            yield base | {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
            self.streamed_chunks += 1
            await asyncio.sleep(self.chunk_delay_s)
        yield base | {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        options = (request.model_extra or {}).get("stream_options") or {}
        if self.send_usage and options.get("include_usage"):
            usage = {
                "prompt_tokens": 3,
                "completion_tokens": self.completion_tokens,
                "total_tokens": 3 + self.completion_tokens,
            }
            yield base | {"choices": [], "usage": usage}
        self.stream_completed = True


@pytest.fixture
def provider(api: TestClient) -> FakeProvider:
    fake = FakeProvider()
    app.dependency_overrides[get_catalog] = lambda: Catalog([fake])
    response = api.put("/admin/prices", json=FAKE_PRICE)
    assert response.status_code == 200, response.text
    return fake


@pytest.fixture
def customer_key(api: TestClient) -> str:
    return create_key(api)["key"]


def chat(api: TestClient, key: str, **body) -> dict:
    payload = {"model": MODEL, "messages": [{"role": "user", "content": "Hi"}]} | body
    return api.post(
        "/v1/chat/completions", json=payload, headers={"Authorization": f"Bearer {key}"}
    )
