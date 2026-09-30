# Design decisions

Short records of choices where the trade-off matters, and why.

## Rate limiting fails open when Redis is down

**Decision.** If Redis can't be reached, a request skips the rate limits: it goes ahead, a
warning is logged (`Redis unavailable, rate limits not applied for key ...`), and the response
leaves out the `X-RateLimit-*` headers rather than report numbers Tollgate doesn't have. When
Redis comes back, limits apply again with no restart.

**Why.** Redis and Postgres guard different things:

- **Redis decides how fast** a key may send (requests and tokens per minute). Getting this wrong
  for a few minutes means a customer sends faster than agreed.
- **Postgres decides who may spend** (the monthly budget). Getting this wrong means real money.

Failing closed would turn a Redis outage into a full outage: every customer gets errors even
though nothing about their money is at risk. Failing open keeps the gateway serving, and the
budget check in Postgres still stops anyone from spending past their budget, so the worst case
is a burst of traffic that was paid for anyway.

**Costs we accept.**

- During an outage a key can exceed its per-minute limits, and so can hit the provider harder;
  the provider's own rate limits and Tollgate's budget still apply.
- If Redis hangs rather than refuses connections, requests slow down: each Redis call gives
  up after its 2 s connect or read timeout. A request stops at the first failure: if that's
  at admission, it goes ahead without limits and skips settling; if Redis fails later, only
  the settle waits.
- Tokens taken just before Redis failed aren't settled, so the key's tokens bucket may stay a
  little lower than it should for up to a minute after Redis returns. That errs strict.
- An oversized request (estimate above the whole tokens-per-minute limit) is let through too,
  rather than special-cased; the budget still bounds it.

**When to revisit.** If a customer contract makes the per-minute limit a hard guarantee (for
example, to protect a shared provider quota), fail closed for that key instead.
