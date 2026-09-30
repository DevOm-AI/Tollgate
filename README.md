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
| `fast-chat`          | A route: Groq's `llama-3.1-8b-instant`, then Gemini's `gemini-2.5-flash` |

Routes let customers ask for a name while Tollgate picks the provider. Set them with
`ROUTES` (JSON, route name -> models in order); models whose provider has no key are skipped.

If a provider times out, can't be reached, or answers `429` or `5xx`, Tollgate retries the
request on the next model in the route, each with its own timeouts. The budget hold is the
worst case of the costliest provider in the route (any of them may end up answering), and the
bill is at the price of the provider that actually answered. A `400`-type rejection isn't retried: the request itself is the
problem. **Fallback only happens before the first byte reaches the client.** Once half an
answer has been streamed, switching providers would glue two different answers together, so a
failure mid-stream ends the stream with an error event instead.

Each provider has a circuit breaker: after 5 failures (timeouts, connection errors, `429`,
`5xx`) within 30 s it opens and the provider gets no requests for 30 s, so requests go straight
to the fallback instead of each waiting out a timeout. Then one test request goes through; a
success closes the breaker again. If every provider in a route is open, the answer is `503`
with `Retry-After`. `GET /health` shows each provider's breaker state.

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
with an error event and you're billed for what was sent. If you hang up mid-stream,
Tollgate cancels the provider's request at once (no paying for tokens nobody reads) and bills
only what was generated until then.

Every provider call has three timeouts (`PROVIDER_*_TIMEOUT_S`): 5 s to connect, 30 s to the
first token of a stream (a role-only opening chunk doesn't count), and 120 s for the whole
answer, so a provider that connects and then goes silent can't hang a request. A non-streaming
answer arrives all at once, so it has no first token to wait for: the total limit bounds it. A timeout before any output is a `504` (`provider_timeout`) and costs
nothing; mid-stream it ends the stream with an error event.

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
never finished. Locally the `jobs` service runs it every minute (`python -m app.jobs`).

Requests are only served for priced models. The mock is priced by the migrations; set the
rest (per 1,000 tokens, in micro-dollars) with the admin API:

```bash
curl -X PUT -H "Authorization: Bearer $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"provider": "groq", "model": "llama-3.1-8b-instant",
       "input_micros_per_1k": 50, "output_micros_per_1k": 80}' \
  http://localhost:8001/admin/prices
```

## Billing with Stripe

Usage is billed through Stripe (test mode) with a billing meter that sums micro-dollars per
customer, and a monthly metered price of 0.0001 US cents (one micro-dollar) per unit, so an
invoice is exactly the usage Tollgate recorded, to the cent. With `STRIPE_SECRET_KEY` set:

```bash
uv run python -m scripts.setup_stripe          # create (or find) the meter and the price
curl -X POST -H "Authorization: Bearer $ADMIN_API_KEY" \
  http://localhost:8001/admin/customers/<customer-id>/billing   # Stripe customer + subscription
```

Both are safe to run again: they find what already exists.

Usage reaches Stripe through an outbox. Settle writes a `usage_outbox` row in the same
transaction that charges the budget, so usage can't be lost between Tollgate and Stripe. Every
minute a job groups unsent rows by customer and minute, sends one meter event per group with a
fixed identifier (customer + minute), and marks the rows sent only after Stripe answers OK.
Stripe drops an identifier it has already seen, so a retry after a crash can't bill twice.
Minutes are sent once they're over a minute old, so a settle committing just after the minute
ends still makes it into that minute's event.

Once a day (00:30 UTC) a reconciliation job compares each billed customer's usage for the
previous UTC day in Postgres with Stripe's meter summary for that day, and logs any difference
with both totals and how much of Tollgate's was sent.

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

## Dashboard

`web/` is a React dashboard: per key, spend against budget, requests and tokens per day,
p50/p95 latency, error rate by provider and the request log (metadata only), plus an admin
view to create keys, change limits and revoke them. It uses the `/admin` API with the admin
key; allow its origin with `CORS_ORIGINS`. See [web/README.md](web/README.md).

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
