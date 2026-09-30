import io
import json
import threading
import urllib.error
import urllib.request

import pytest

from scripts import check_accounts
from scripts.check_accounts import (
    AccountKeys,
    Check,
    CheckFailed,
    check_gemini,
    check_groq,
    check_hf,
    check_stripe,
    check_upstash,
    run_check,
)

ACCOUNT_VARS = (
    "NEON_DATABASE_URL",
    "UPSTASH_REDIS_URL",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "STRIPE_SECRET_KEY",
    "HF_TOKEN",
)


@pytest.fixture(autouse=True)
def no_account_env(monkeypatch):
    """Tests set only the keys they need; nothing comes from the real .env or shell."""
    for name in ACCOUNT_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def responses(monkeypatch) -> dict[str, object]:
    """Fake HTTP: map a URL to a JSON body, or to an int for that HTTP error status."""
    bodies: dict[str, object] = {}
    sent: list[urllib.request.Request] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float):
        sent.append(request)
        body = bodies[request.full_url]
        if isinstance(body, int):
            raise urllib.error.HTTPError(request.full_url, body, "error", {}, io.BytesIO())
        return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    bodies["_sent"] = sent
    return bodies


def keys(**values: str) -> AccountKeys:
    return AccountKeys(_env_file=None, **values)


def failing_probe(message: str):
    def probe(value: str) -> str:
        raise RuntimeError(message)

    return probe


def test_missing_key_is_reported_without_calling_the_service():
    def probe(value: str) -> str:
        raise AssertionError("must not be called")

    result = run_check(Check("Groq", "GROQ_API_KEY", probe), keys())

    assert result.status == "missing"
    assert result.detail == "set GROQ_API_KEY in .env"


def test_blank_key_counts_as_missing(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "")

    blank = AccountKeys(_env_file=None)

    result = run_check(Check("Groq", "GROQ_API_KEY", lambda value: "ok"), blank)

    assert result.status == "missing"


def test_working_key_is_ok():
    result = run_check(
        Check("Groq", "GROQ_API_KEY", lambda value: "20 models"), keys(groq_api_key="gsk_x")
    )

    assert result.status == "ok"
    assert result.detail == "20 models"


def test_failure_message_never_contains_the_key():
    check = Check("Groq", "GROQ_API_KEY", failing_probe("rejected key gsk_VALUE"))

    result = run_check(check, keys(groq_api_key="gsk_VALUE"))

    assert result.status == "failed"
    assert result.detail == "RuntimeError: rejected key ***"


def test_failure_message_never_contains_a_url_password():
    url = "postgresql://neondb_owner:PASSWORD@ep-x.neon.tech/neondb?sslmode=require"
    check = Check("Neon Postgres", "NEON_DATABASE_URL", failing_probe("auth failed: PASSWORD"))

    result = run_check(check, keys(neon_database_url=url))

    assert "PASSWORD" not in result.detail


def test_failure_message_keeps_only_the_first_line():
    check = Check("Groq", "GROQ_API_KEY", failing_probe("first line\nsecond line"))

    result = run_check(check, keys(groq_api_key="gsk_x"))

    assert result.detail == "RuntimeError: first line"


def test_probe_that_never_answers_fails_at_the_deadline():
    stuck = threading.Event()

    def probe(value: str) -> str:
        stuck.wait()  # Like a query on a connection that went silent.
        return "never"

    check = Check("Neon Postgres", "NEON_DATABASE_URL", probe)
    try:
        result = run_check(check, keys(neon_database_url="postgresql://x"), deadline_seconds=0.1)
    finally:
        stuck.set()

    assert result.status == "failed"
    assert result.detail == "no answer within 0.1s"


def test_main_still_runs_the_other_checks_after_a_stuck_one(monkeypatch, capsys):
    stuck = threading.Event()
    monkeypatch.setattr(check_accounts, "DEADLINE_SECONDS", 0.1)
    monkeypatch.setattr(
        check_accounts,
        "CHECKS",
        [
            Check("Neon Postgres", "NEON_DATABASE_URL", lambda value: stuck.wait() and "never"),
            Check("Groq", "GROQ_API_KEY", lambda value: "20 models"),
        ],
    )
    monkeypatch.setattr(
        check_accounts,
        "AccountKeys",
        lambda: keys(neon_database_url="postgresql://x", groq_api_key="gsk_x"),
    )
    try:
        assert check_accounts.main() == 1
    finally:
        stuck.set()

    output = capsys.readouterr().out
    assert "Neon Postgres  failed   no answer within 0.1s" in output
    assert "Groq           ok       20 models" in output


def test_gemini_and_groq_count_models(responses):
    responses[check_accounts.GEMINI_MODELS_URL] = {"data": [{"id": "a"}, {"id": "b"}]}
    responses[check_accounts.GROQ_MODELS_URL] = {"data": [{"id": "c"}]}

    assert check_gemini("gemini-key") == "2 models"
    assert check_groq("gsk_key") == "1 models"
    sent = responses["_sent"]
    assert [request.get_header("Authorization") for request in sent] == [
        "Bearer gemini-key",
        "Bearer gsk_key",
    ]
    assert all(request.get_header("User-agent") == "tollgate-check-accounts" for request in sent)


@pytest.mark.parametrize(
    ("status", "hint"), [(400, " (wrong key?)"), (401, " (wrong key?)"), (500, "")]
)
def test_http_error_reports_status_only(responses, status: int, hint: str):
    responses[check_accounts.GROQ_MODELS_URL] = status

    with pytest.raises(CheckFailed) as exc_info:
        check_groq("gsk_key")

    assert str(exc_info.value) == f"HTTP {status} from api.groq.com{hint}"


def test_stripe_test_key_passes(responses):
    responses[check_accounts.STRIPE_BALANCE_URL] = {"object": "balance", "livemode": False}

    assert check_stripe("sk_test_abc") == "test mode"


@pytest.mark.parametrize("key", ["sk_live_abc", "rk_live_abc"])
def test_stripe_live_key_fails_without_calling_stripe(responses, key: str):
    with pytest.raises(CheckFailed, match="live key"):
        check_stripe(key)

    assert responses["_sent"] == []


def test_stripe_fails_when_stripe_answers_in_live_mode(responses):
    responses[check_accounts.STRIPE_BALANCE_URL] = {"object": "balance", "livemode": True}

    with pytest.raises(CheckFailed, match="live mode"):
        check_stripe("sk_test_abc")


def test_hf_write_token_passes(responses):
    responses[check_accounts.HF_WHOAMI_URL] = {
        "name": "someone",
        "auth": {"accessToken": {"role": "write"}},
    }

    assert check_hf("hf_x") == "someone (write token)"


def test_hf_read_only_token_fails(responses):
    responses[check_accounts.HF_WHOAMI_URL] = {
        "name": "someone",
        "auth": {"accessToken": {"role": "read"}},
    }

    with pytest.raises(CheckFailed, match="read-only"):
        check_hf("hf_x")


def hf_fine_grained(*scoped: dict) -> dict:
    return {
        "name": "someone",
        "auth": {
            "accessToken": {
                "role": "fineGrained",
                "fineGrained": {"global": ["discussion.write"], "scoped": list(scoped)},
            }
        },
    }


def test_hf_fine_grained_token_with_repo_write_passes(responses):
    responses[check_accounts.HF_WHOAMI_URL] = hf_fine_grained(
        {"entity": {"type": "user", "name": "someone"}, "permissions": ["repo.content.read"]},
        {
            "entity": {"type": "space", "name": "someone/tollgate"},
            "permissions": ["repo.content.read", "repo.write"],
        },
    )

    assert check_hf("hf_x") == "someone (fine-grained, can write to space someone/tollgate)"


def test_hf_fine_grained_token_with_read_only_permissions_fails(responses):
    responses[check_accounts.HF_WHOAMI_URL] = hf_fine_grained(
        {"entity": {"type": "user", "name": "someone"}, "permissions": ["repo.content.read"]},
    )

    with pytest.raises(CheckFailed, match="can't write to any repo"):
        check_hf("hf_x")


def test_hf_fine_grained_token_without_scopes_fails(responses):
    responses[check_accounts.HF_WHOAMI_URL] = hf_fine_grained()

    with pytest.raises(CheckFailed, match="can't write to any repo"):
        check_hf("hf_x")


@pytest.mark.parametrize("access_token", [{}, {"role": "admin"}])
def test_hf_token_of_unknown_kind_fails(responses, access_token: dict):
    responses[check_accounts.HF_WHOAMI_URL] = {
        "name": "someone",
        "auth": {"accessToken": access_token},
    }

    with pytest.raises(CheckFailed, match="can't confirm"):
        check_hf("hf_x")


def test_upstash_needs_the_tls_url():
    with pytest.raises(CheckFailed, match="rediss://"):
        check_upstash("redis://default:pw@x.upstash.io:6379")


def test_main_exits_1_until_every_account_works(monkeypatch, capsys):
    monkeypatch.setattr(
        check_accounts,
        "CHECKS",
        [
            Check("Groq", "GROQ_API_KEY", lambda value: "20 models"),
            Check("Gemini", "GEMINI_API_KEY", lambda value: "50 models"),
        ],
    )
    monkeypatch.setattr(check_accounts, "AccountKeys", lambda: keys(groq_api_key="gsk_VALUE"))

    assert check_accounts.main() == 1

    output = capsys.readouterr().out
    assert "Groq    ok       20 models" in output
    assert "Gemini  missing  set GEMINI_API_KEY in .env" in output
    assert "gsk_VALUE" not in output


def test_main_exits_0_when_every_account_works(monkeypatch, capsys):
    monkeypatch.setattr(
        check_accounts, "CHECKS", [Check("Groq", "GROQ_API_KEY", lambda value: "20 models")]
    )
    monkeypatch.setattr(check_accounts, "AccountKeys", lambda: keys(groq_api_key="gsk_VALUE"))

    assert check_accounts.main() == 0
    assert "All accounts work." in capsys.readouterr().out
