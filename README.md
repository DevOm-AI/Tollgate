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
    model="mock",
    messages=[{"role": "user", "content": "Say hello"}],
    max_tokens=64,
)
print(completion.choices[0].message.content)
```

Models:

| `model`              | Served by                                                   |
| -------------------- | ----------------------------------------------------------- |
| `mock`               | The mock provider: fake text, costs nothing (see `MOCK_*`)  |
| `groq/<model>`       | Groq, e.g. `groq/llama-3.1-8b-instant` (needs `GROQ_API_KEY`) |
| `gemini/<model>`     | Gemini, e.g. `gemini/gemini-2.5-flash` (needs `GEMINI_API_KEY`) |

Requests without `max_tokens` (or `max_completion_tokens`) get `DEFAULT_MAX_TOKENS` (1,024), so
every request has a known worst-case cost.

Errors come back in OpenAI's format too, so the library raises its usual exceptions
(`AuthenticationError` for a wrong or revoked key, `NotFoundError` for an unknown model).

## Rate limits

Each key has two limits, both token buckets in Redis that refill every minute:

- **Requests per minute** (`rpm_limit`): each request takes 1.
- **Tokens per minute** (`tpm_limit`): a request takes its worst case up front (input tokens +
  `max_tokens`), and the difference is settled once the real count is known.

Over a limit, Tollgate answers `429` in OpenAI's format with `Retry-After` in seconds (left out
when the request is bigger than the whole limit, since retrying can't help). Every response
that counts against the limits reports both, like OpenAI does:

| Header                                  | Meaning                                      |
| --------------------------------------- | -------------------------------------------- |
| `X-RateLimit-Limit-{Requests,Tokens}`     | The key's limit per minute                   |
| `X-RateLimit-Remaining-{Requests,Tokens}` | What's left right now                        |
| `X-RateLimit-Reset-{Requests,Tokens}`     | Seconds until the limit is fully refilled    |

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
