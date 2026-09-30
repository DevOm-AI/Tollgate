# Tollgate

An LLM API gateway with rate limits, budgets and usage billing.

> Work in progress. The full README (OpenAI library example, overspend test, architecture,
> design decisions) comes later.

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

## Tests and lint

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest
```
