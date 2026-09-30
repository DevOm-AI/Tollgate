import uuid

import anyio
import httpx2
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.main import app
from app.models import KeySpend, RequestLog, Reservation, UsageOutbox
from app.providers.base import ProviderError
from tests.conftest import MODEL, chat, create_key


def spend(db_engine: Engine, key_id: str) -> tuple[int, int]:
    with Session(db_engine) as session:
        row = session.scalars(select(KeySpend).where(KeySpend.key_id == uuid.UUID(key_id))).one()
        return row.spent_micros, row.reserved_micros


def logged(db_engine: Engine, key_id: str) -> list[RequestLog]:
    with Session(db_engine) as session:
        return list(
            session.scalars(select(RequestLog).where(RequestLog.key_id == uuid.UUID(key_id)))
        )


def open_reservations(db_engine: Engine, key_id: str) -> int:
    with Session(db_engine) as session:
        return len(
            session.scalars(
                select(Reservation).where(
                    Reservation.key_id == uuid.UUID(key_id), Reservation.settled_at.is_(None)
                )
            ).all()
        )


def test_provider_error_releases_the_hold_and_bills_nothing(api, provider, db_engine):
    created = create_key(api)
    provider.error = ProviderError("fake: ConnectTimeout")

    assert chat(api, created["key"]).status_code == 502

    assert spend(db_engine, created["id"]) == (0, 0)
    assert open_reservations(db_engine, created["id"]) == 0
    [request] = logged(db_engine, created["id"])
    assert (request.status, request.cost_micros) == ("provider_error", 0)


def test_rejected_request_releases_the_hold_and_bills_nothing(api, provider, db_engine):
    created = create_key(api)
    provider.error = ProviderError("fake: HTTP 400", status_code=400, upstream_message="bad")

    assert chat(api, created["key"]).status_code == 400

    assert spend(db_engine, created["id"]) == (0, 0)


def test_unexpected_error_releases_the_hold_and_bills_nothing(api, provider, db_engine):
    created = create_key(api)
    provider.error = RuntimeError("bug in an adapter")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "Hi"}]},
        headers={"Authorization": f"Bearer {created['key']}"},
    )

    assert response.status_code == 500
    assert spend(db_engine, created["id"]) == (0, 0)
    assert open_reservations(db_engine, created["id"]) == 0
    [request] = logged(db_engine, created["id"])
    assert (request.status, request.cost_micros) == ("error", 0)


def test_cancelled_request_releases_the_hold_at_once(api, provider, db_engine):
    # E.g. a server shutting down mid-request. AnyIO keeps re-raising the cancellation at
    # every await inside the cancelled scope, so cleanup that isn't shielded never finishes
    # and the money stays held until the sweep (10 minutes).
    created = create_key(api)
    provider.delay_s = 5

    async def send_and_cancel() -> None:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            with anyio.move_on_after(0.3):
                await client.post(
                    "/v1/chat/completions",
                    json={"model": MODEL, "messages": [{"role": "user", "content": "Hi"}]},
                    headers={"Authorization": f"Bearer {created['key']}"},
                )

    anyio.run(send_and_cancel)

    assert spend(db_engine, created["id"]) == (0, 0)
    assert open_reservations(db_engine, created["id"]) == 0
    [request] = logged(db_engine, created["id"])
    assert (request.status, request.cost_micros) == ("cancelled", 0)


def test_failed_requests_send_nothing_to_stripe(api, provider, db_engine):
    created = create_key(api)
    provider.error = ProviderError("fake: HTTP 503", status_code=503)
    chat(api, created["key"])

    with Session(db_engine) as session:
        usage = session.scalars(
            select(UsageOutbox).where(UsageOutbox.customer_id == uuid.UUID(created["customer_id"]))
        ).all()
    assert usage == []
