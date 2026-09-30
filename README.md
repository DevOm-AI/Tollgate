# Tollgate

An LLM API gateway with rate limits, budgets and usage billing.

> Work in progress. The full README (OpenAI library example, overspend test, architecture,
> design decisions) comes later.

## Use it with the OpenAI library

Tollgate speaks OpenAI's chat completions API, so the official `openai` library works by
changing `base_url` and using a Tollgate key:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8001/v1", api_key="tg_live_...")

completion = client.chat.completions.create(
    model="<model>",
    messages=[{"role": "user", "content": "Say hello"}],
    max_tokens=64,
)
print(completion.choices[0].message.content)
```

`model` must be one Tollgate serves. No provider adapters are registered yet, so for now
every model answers 404 `model_not_found`.

Errors come back in OpenAI's format too, so the library raises its usual exceptions
(`AuthenticationError` for a wrong or revoked key, `NotFoundError` for an unknown model).

## Layout

| Path            | What lives there                               |
| --------------- | ---------------------------------------------- |
| `app/api`       | HTTP routes                                    |
| `app/providers` | LLM provider adapters                          |
| `app/limits`    | Rate limiting (Redis)                          |
| `app/billing`   | Budgets, metering and Stripe                   |
| `app/jobs`      | Background jobs                                |
| `app/core`      | Settings, database and Redis clients           |
| `app/models`    | SQLAlchemy models (migrations in `alembic/`)   |
| `web/`          | Dashboard (React)                              |

## Run locally

Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env
docker compose up --build
```

API docs: http://localhost:8001/docs · Health: http://localhost:8001/health

## Accounts and keys

Tollgate uses free tiers only: Neon, Upstash, Hugging Face, Gemini, Groq and Stripe in test
mode. [docs/accounts.md](docs/accounts.md) says where to get each key and which `.env`
variable it goes in. Then check them all without printing any:

```bash
uv run python -m scripts.check_accounts
```

## Admin API

Customers and their keys are managed through `/admin` routes, guarded by `ADMIN_API_KEY` (at
least 32 characters; unset disables them). Customer keys never work there.

```bash
curl -H "Authorization: Bearer $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"name": "Acme"}' http://localhost:8001/admin/customers
```

Creating a key returns the full `tg_live_...` key once. Tollgate stores only its SHA-256 hash
and the first 8 characters after `tg_live_`, for display. Limits can be changed later with
`PATCH /admin/keys/{id}`, and `POST /admin/keys/{id}/revoke` turns a key off for good.

## Tests and lint

Database tests create (and drop) their own scratch database on the compose Postgres, or on
the server in `DATABASE_URL` if set.

```bash
docker compose up -d postgres redis
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest
```
