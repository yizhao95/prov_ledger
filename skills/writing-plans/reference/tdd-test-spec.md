# TDD Test-Specification Requirements

The test-driven-development skill is iron-law for any plan that touches code, schema, config, SQL, YAML, or migrations. This file defines what that means at PLANNING time.

## The Pairing Rule

**Every `CODE` step MUST be preceded by a `TEST` step.** No exceptions.

The TEST step writes a failing test (RED). The CODE step writes the minimum implementation that makes it pass (GREEN). Refactors land as their own additional CODE step with their own preceding TEST step (or expansion of the existing test).

## Test-Step Description Requirements

A TEST step description that reads "write tests for the above" or "test the function" is a plan failure. The description **MUST** enumerate, in one sentence per item:

1. **Behavior under test** — the externally-observable behavior (not the internal algorithm)
2. **Inputs** — concrete values the test will use
3. **Edge cases covered** — explicitly list each one
4. **Expected assertions** — what the test asserts about the output

This is non-negotiable because:
- The TEST step description IS the test plan. If it's vague, the test ends up vague.
- Future-you reading the plan needs to know if a missing test case is a gap or intentional.
- Reviewers need to evaluate test sufficiency without reading the test code.

## Worked Example 1 — Pure Function

```json
{
  "description": "TEST: tests/test_normalize_email.py — covering: (a) lowercase conversion ('A@B.com' → 'a@b.com'), (b) leading/trailing whitespace stripped, (c) empty string → ValueError, (d) None → TypeError, (e) string with no @ → ValueError, (f) gmail dot-folding ('john.doe@gmail.com' → 'johndoe@gmail.com'), (g) other domains keep dots verbatim. Asserts on return value for happy paths, pytest.raises for failure paths.",
  "type": "CODE"
}
```

## Worked Example 2 — CLI Command

```json
{
  "description": "TEST: tests/test_publish_plan.py — covering publish-plan.sh: (a) valid JSON input → exit 0 + JSON payload with plan_id, (b) valid YAML input → equivalent result, (c) missing 'goal' field → exit ≠ 0 + stderr names 'goal', (d) skill source not in enum → exit ≠ 0 + stderr lists valid choices, (e) empty steps array → exit ≠ 0, (f) two consecutive publishes → 2 distinct plan_ids in DB, (g) unknown file extension → exit ≠ 0. Each test uses an ephemeral SQLite DB via ORCH_DB env var to avoid touching ~/skill-workspace/orchestrator.db.",
  "type": "CODE"
}
```

## Worked Example 3 — DB Migration

```json
{
  "description": "TEST: tests/test_migration_006.py — covering migration 006_add_priority_column.sql: (a) fresh DB → migration applies cleanly + Steps.priority column exists with default=0, (b) DB pre-populated with 3 rows from migration 005 → migration applies + existing rows get priority=0, (c) re-running migration on already-migrated DB → no-op exit 0 (idempotent via schema_version), (d) priority CHECK constraint rejects values outside 0-9. Uses sqlite3.Connection in-memory + reads migration SQL file directly.",
  "type": "CODE"
}
```

## Anti-patterns

| ❌ Wrong | ✅ Right |
|---|---|
| `"TEST: test the new feature"` | Enumerate behaviors + edge cases (3+ items minimum) |
| `"TEST: unit tests for AuthService"` | List each method + each branch + each error path |
| `"TEST: pytest tests/"` (no description of what's covered) | Description IS the test plan — write it out |
| Skip the TEST step because "the function is trivial" | Even trivial functions get a 1-line test (return value matches) |
| One mega-TEST step covering 5 unrelated CODE steps | One TEST step per CODE step — keeps RED/GREEN tight |

## What the executing-plans skill enforces

When executing-plans encounters a `CODE` step without a preceding `TEST` step, it deviates the plan via `$CLI evaluate --deviation` and inserts a TEST sub-step before continuing. This is mid-flight enforcement of the planning-time rule.
