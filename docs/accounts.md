# Accounts

Tollgate runs on free tiers only. None of these accounts needs a credit card. Free tiers
change, so read each signup page when you create the account.

Every value goes in `.env`, which git ignores. Never commit it, and never paste a key into
an issue, a commit or a log. `.env.example` lists the variables with blank values.

| Service                         | Used for                    | Variable in `.env`  |
| ------------------------------- | --------------------------- | ------------------- |
| [Neon](https://neon.tech)       | Hosted Postgres             | `NEON_DATABASE_URL` |
| [Upstash](https://upstash.com)  | Hosted Redis                | `UPSTASH_REDIS_URL` |
| [Google AI Studio][ai-studio]   | Gemini API                  | `GEMINI_API_KEY`    |
| [Groq](https://console.groq.com)| Groq API                    | `GROQ_API_KEY`      |
| [Stripe](https://stripe.com)    | Usage billing (test mode)   | `STRIPE_SECRET_KEY` |
| [Hugging Face][hf]              | Hosting (Docker Space)      | `HF_TOKEN`          |

[ai-studio]: https://aistudio.google.com
[hf]: https://huggingface.co

## Neon (Postgres)

1. Create a project. Pick the region closest to where the Space will run.
2. From the project dashboard, open **Connect** and copy the connection string.
3. Paste it as `NEON_DATABASE_URL`. Keep `?sslmode=require` on the end. Tollgate rewrites
   the `postgresql://` scheme to its psycopg driver, so paste it unchanged.

## Upstash (Redis)

1. Create a Redis database on the free plan.
2. Copy the connection URL that starts with **`rediss://`** (two s's: TLS). Upstash only
   accepts TLS connections, so the `redis://` URL shown next to `redis-cli --tls` fails
   without that flag.
3. Paste it as `UPSTASH_REDIS_URL`.

## Gemini (Google AI Studio)

1. Sign in to Google AI Studio and create an API key.
2. Paste it as `GEMINI_API_KEY`.

## Groq

1. Sign in to the Groq console and create an API key under **API Keys**.
2. Paste it as `GROQ_API_KEY`. Groq shows the key once, so copy it right away.

## Stripe (test mode)

1. Create an account. You don't need to activate payments; test mode works without it.
2. Turn on **Test mode**, open **Developers → API keys** and copy the **secret key**
   (`sk_test_...`).
3. Paste it as `STRIPE_SECRET_KEY`.

Tollgate refuses to start with a live key (`sk_live_` / `rk_live_`) unless
`ENVIRONMENT=production`, so a laptop or CI run can never bill a real card.

## Hugging Face (hosting)

1. Create an account.
2. Under **Settings → Access Tokens**, create a token with **write** access. Deploys push to
   the Space's git remote, which a read-only token can't do. A fine-grained token works too,
   as long as it has repo write permission on your account or on the Space.
3. Paste it as `HF_TOKEN`.

## Check them

```bash
uv run python -m scripts.check_accounts
```

Each check is a free, read-only call. None of them spends tokens or money:

| Service      | Check                                         |
| ------------ | --------------------------------------------- |
| Neon         | Connects and runs `SHOW server_version`       |
| Upstash      | `PING` over TLS                               |
| Gemini, Groq | Lists models on the OpenAI-compatible API     |
| Stripe       | Reads the balance and confirms it's test mode |
| Hugging Face | `whoami`, and the token can write to repos    |

Output looks like this. Keys are never printed.

```text
Neon Postgres  ok       Postgres 17.5
Upstash Redis  ok       PING ok
Gemini         ok       50 models
Groq           ok       20 models
Stripe         ok       test mode
Hugging Face   failed   HTTP 401 from huggingface.co (wrong key?)
```

Each check gives up after 30 seconds, so one unreachable service can't hold up the rest.
It exits with status 1 until every account works.
