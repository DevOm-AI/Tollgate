import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, make_url, pool, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.main import app

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


@pytest.fixture
def api(db_engine: Engine) -> Iterator[TestClient]:
    """The app on the scratch database, with ADMIN_KEY as its admin key.

    Requests commit for real (the scratch database is dropped at the end), so tests make
    their own rows instead of assuming empty tables.
    """
    async_engine = create_async_engine(db_engine.url, poolclass=pool.NullPool)
    sessions = async_sessionmaker(bind=async_engine, autoflush=False, expire_on_commit=False)

    async def get_test_db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = get_test_db
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, admin_api_key=ADMIN_KEY
    )
    try:
        # Not entered as a context manager: that would run the lifespan, whose shutdown
        # closes the app's shared Redis client and engine for the tests that follow.
        yield TestClient(app, headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    finally:
        app.dependency_overrides.clear()
