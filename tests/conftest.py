import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, make_url, pool, text
from sqlalchemy.orm import Session

from app.core.config import Settings

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
