# Step Quality Bar

Every step description must let a stranger (or future-you) execute it without re-reading the whole conversation. This file defines the bar.

## 1. Specificity — WHO, WHAT, HOW

Each step description must answer:

- **WHO** — what tool or actor performs the step (the agent? a sub-agent? a CLI command?)
- **WHAT** — the concrete artifact that changes (file path, table name, endpoint, etc.)
- **HOW** — what makes the step succeed (the assertion, the expected output, the file existing)

### ❌ Vague

> "Set up the auth module"

What does "set up" mean? Which files? How do we know it's done?

### ✅ Specific

> "CODE: create app/auth/oauth.py with OAuthClient class — `__init__(client_id, secret)`, `async def fetch_token() -> Token`. Imports: httpx, pydantic. Done when `from app.auth.oauth import OAuthClient` succeeds in REPL."

## 2. Bite-Sized Granularity

Each step is one action that takes 2-5 minutes of focused work. Break compound tasks down.

### ❌ Compound

> "Implement and test the rate limiter and update the docs"

### ✅ Bite-sized (3 separate steps)

```
TEST: write tests/test_rate_limiter.py — 4 cases: under-limit ok, at-limit ok, over-limit 429, window-resets-after-60s
CODE: implement app/middleware/rate_limit.py — TokenBucket class with refill rate + capacity, FastAPI middleware wrapper
DOCUMENTATION: append rate-limit section to docs/middleware.md with config example + 429 response shape
```

For trivial single-action tasks (e.g. "what's my IP?") the single step IS the whole task — that's fine. The audit trail is still valuable.

## 3. No Placeholders

These are **plan failures** — never write them in a step description:

- "TBD", "TODO", "implement later", "fill in details"
- "Add appropriate error handling" / "add validation" / "handle edge cases"
- "Write tests for the above" (without specifying WHAT they test — see `tdd-test-spec.md`)
- "Similar to step N" (repeat the detail — execution may read out of order)
- Steps that describe what to do without showing how
- References to types, functions, or methods not defined in any other step

### ❌ Placeholder

> "Add error handling to the new endpoint"

### ✅ Concrete

> "CODE: add try/except around DB call in /api/users — catch sqlalchemy.exc.OperationalError, log to structlog with request_id, return 503 JSON `{error: 'db unavailable', retry_after: 5}`"

## 4. Self-Review checklist (before publish-plan.sh)

Run through these before invoking `publish-plan.sh`:

1. **Spec coverage** — for every requirement in the user query, can you point to a step that implements it?
2. **Placeholder scan** — search the plan for the red flags above; fix them.
3. **Type consistency** — do types/method names in later steps match those introduced earlier?
4. **TDD pairing** — every CODE step preceded by a TEST step? (See `tdd-test-spec.md`.)
5. **Skills declared** — every iron-law skill in the `skills` array? Topic skills too?

If issues → fix inline. Then publish. Re-review is wasted work.
