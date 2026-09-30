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

Streaming works the same way, and words arrive as the provider sends them:

```python
for chunk in client.chat.completions.create(
    model="mock", messages=[{"role": "user", "content": "Say hello"}], stream=True
):
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

Tollgate asks the provider for token usage at the end of every stream and bills from it
(passing it on only if you ask with `stream_options={"include_usage": True}`). If a provider
doesn't send usage, Tollgate counts the prompt and the streamed answer itself with a tokenizer
(`cl100k_base`, baked into the Docker image), never billing more output than `max_tokens`. If a provider
fails before sending anything you get a normal error; if it fails mid-answer the stream ends
with an error event and you're billed for what was sent.

Errors come back in OpenAI's format too, so the library raises its usual exceptions
(`AuthenticationError` for a wrong or revoked key, `NotFoundError` for an unknown model).

## Budgets

Each key has a monthly budget (`monthly_budget_micros`, in micro-dollars: 1 USD = 1,000,000).
Months are calendar months in UTC: each starts with the full budget, and a request that
straddles midnight on the 1st is charged to the month it started in.
Money is always whole micro-dollars, never floats, and costs round up.

Before a request reaches a provider, Tollgate reserves its worst-case cost (a byte-count
upper bound on the prompt plus `max_tokens`, at the model's price) with one conditional
`UPDATE` in Postgres. If the reservation doesn't fit the budget, the answer is `402` with
code `budget_exceeded`. After the answer, one transaction charges the real cost, frees the
rest, and records the request and its usage for Stripe. A failed call is charged nothing.

If Tollgate crashes between reserving and settling, the money stays held until the
reservation expires (10 minutes); a job then releases it and bills nothing, since the request
never finished. Locally the `jobs` service runs it every minute
(`python -m app.jobs.sweep_reservations`).

Requests are only served for priced models. The mock is priced by the migrations; set the
rest (per 1,000 tokens, in micro-dollars) with the admin API:

```bash
curl -X PUT -H "Authorization: Bearer $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"provider": "groq", "model": "llama-3.1-8b-instant",
       "input_micros_per_1k": 50, "output_micros_per_1k": 80}' \
  http://localhost:8001/admin/prices
```

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

If Redis is down, rate limits fail open: requests go ahead without limits and a warning is
logged, because the budget in Postgres, not Redis, protects the money. The reasoning is in
[docs/decisions.md](docs/decisions.md).

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
