import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.security import hash_api_key
from app.main import app
from app.models import ApiKey
from tests.conftest import LIMITS, create_customer, create_key


def stored_key(db_engine: Engine, key_id: str) -> ApiKey:
    with Session(db_engine) as session:
        return session.scalars(select(ApiKey).where(ApiKey.id == uuid.UUID(key_id))).one()


# --- Customers ---


def test_create_customer(api: TestClient):
    customer = create_customer(api, name="Acme Inc", stripe_customer_id="cus_123")

    assert customer["name"] == "Acme Inc"
    assert customer["stripe_customer_id"] == "cus_123"
    assert uuid.UUID(customer["id"])
    assert customer["created_at"]


def test_customer_stripe_id_is_optional(api: TestClient):
    assert create_customer(api)["stripe_customer_id"] is None


def test_duplicate_stripe_customer_id_is_a_conflict(api: TestClient):
    stripe_id = f"cus_{uuid.uuid4().hex}"
    create_customer(api, stripe_customer_id=stripe_id)

    response = api.post("/admin/customers", json={"name": "Other", "stripe_customer_id": stripe_id})

    assert response.status_code == 409


@pytest.mark.parametrize("body", [{}, {"name": ""}, {"name": "x" * 201}])
def test_customer_needs_a_name(api: TestClient, body: dict):
    assert api.post("/admin/customers", json=body).status_code == 422


# --- Creating keys ---


def test_create_key_returns_the_full_key_once(api: TestClient, db_engine: Engine):
    created = create_key(api)

    assert created["key"].startswith("tg_live_")
    assert created["prefix"] == created["key"].removeprefix("tg_live_")[:8]
    assert created["is_active"] is True
    assert {name: created[name] for name in LIMITS} == LIMITS

    row = stored_key(db_engine, created["id"])
    assert row.key_hash == hash_api_key(created["key"])
    assert created["key"] not in {row.key_hash, row.prefix}


def test_key_responses_after_creation_never_include_the_key(api: TestClient):
    created = create_key(api)

    updated = api.patch(f"/admin/keys/{created['id']}", json={"rpm_limit": 5}).json()
    revoked = api.post(f"/admin/keys/{created['id']}/revoke").json()

    for body in (updated, revoked):
        assert "key" not in body
        assert created["key"] not in str(body)


def test_key_for_unknown_customer_is_404(api: TestClient):
    response = api.post(f"/admin/customers/{uuid.uuid4()}/keys", json=LIMITS)

    assert response.status_code == 404


@pytest.mark.parametrize(
    "limits",
    [
        {"rpm_limit": 0},
        {"tpm_limit": -1},
        {"monthly_budget_micros": -1},
        {"rpm_limit": 2**31},
        {"monthly_budget_micros": 2**63},
        {"monthly_budget_micros": 1.5},
    ],
)
def test_key_limits_are_validated(api: TestClient, limits: dict):
    customer = create_customer(api)

    response = api.post(f"/admin/customers/{customer['id']}/keys", json=LIMITS | limits)

    assert response.status_code == 422


def test_key_limits_are_required(api: TestClient):
    customer = create_customer(api)

    response = api.post(f"/admin/customers/{customer['id']}/keys", json={"rpm_limit": 60})

    assert response.status_code == 422


# --- Changing limits ---


def test_update_changes_only_the_limits_sent(api: TestClient, db_engine: Engine):
    created = create_key(api)

    response = api.patch(
        f"/admin/keys/{created['id']}", json={"rpm_limit": 5, "monthly_budget_micros": 0}
    )

    assert response.status_code == 200
    row = stored_key(db_engine, created["id"])
    assert (row.rpm_limit, row.tpm_limit, row.monthly_budget_micros) == (5, 100_000, 0)


@pytest.mark.parametrize("body", [{}, {"rpm_limit": None}, {"tpm_limit": 0}])
def test_update_rejects_empty_null_or_invalid_limits(api: TestClient, body: dict):
    created = create_key(api)

    assert api.patch(f"/admin/keys/{created['id']}", json=body).status_code == 422


def test_update_unknown_key_is_404(api: TestClient):
    assert api.patch(f"/admin/keys/{uuid.uuid4()}", json={"rpm_limit": 5}).status_code == 404


# --- Revoking ---


def test_revoke_deactivates_the_key(api: TestClient, db_engine: Engine):
    created = create_key(api)

    response = api.post(f"/admin/keys/{created['id']}/revoke")

    assert response.status_code == 200
    assert response.json()["is_active"] is False
    assert stored_key(db_engine, created["id"]).is_active is False


def test_revoke_twice_is_fine(api: TestClient):
    created = create_key(api)
    api.post(f"/admin/keys/{created['id']}/revoke")

    response = api.post(f"/admin/keys/{created['id']}/revoke")

    assert response.status_code == 200
    assert response.json()["is_active"] is False


def test_revoke_unknown_key_is_404(api: TestClient):
    assert api.post(f"/admin/keys/{uuid.uuid4()}/revoke").status_code == 404


# --- Admin auth ---

ADMIN_ROUTES = [
    ("post", "/admin/customers"),
    ("post", f"/admin/customers/{uuid.uuid4()}/keys"),
    ("patch", f"/admin/keys/{uuid.uuid4()}"),
    ("post", f"/admin/keys/{uuid.uuid4()}/revoke"),
]


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": ""},
        {"Authorization": "Bearer wrong-key"},
        {"Authorization": "Basic dXNlcjpwYXNz"},
    ],
)
def test_admin_routes_need_the_admin_key(api: TestClient, method: str, path: str, headers: dict):
    response = api.request(method, path, headers=headers, json={})

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_customer_key_is_refused_on_admin_routes(api: TestClient):
    customer_key = create_key(api)["key"]

    response = api.post(
        "/admin/customers",
        headers={"Authorization": f"Bearer {customer_key}"},
        json={"name": "Sneaky"},
    )

    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
def test_admin_routes_are_disabled_without_an_admin_key(api: TestClient, method: str, path: str):
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, admin_api_key=None)

    response = api.request(method, path, json={})

    assert response.status_code == 503
