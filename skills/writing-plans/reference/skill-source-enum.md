# Skill Source Enum

Every skill recorded against a plan has a `source` field explaining WHY that skill activated. The enum has exactly four values, enforced by a `CHECK` constraint in migration `005_skill_activations.sql`.

| source             | Meaning                                                                       |
|--------------------|-------------------------------------------------------------------------------|
| `iron-law`         | Mandatory trigger — writing-plans, executing-plans, TDD on coding, etc.       |
| `auto-search`      | Matched the task when you checked the available skills (the Skill tool's list) |
| `explicit-mention` | The user named the skill (or its concept) in their prompt                     |
| `deferred-load`    | Loaded mid-flight when the task evolved — record via `record-skill` not init  |

## Always-on iron-law skills

Include these in EVERY `skills` array in plan-input — they apply to every plan that involves real work:

- `writing-plans` — you are using it right now to author the plan
- `executing-plans` — the next handoff after publish-plan.sh returns
- `test-driven-development` — if any step writes code/schema/config/SQL/YAML
- `verification-before-completion` — if you'll claim done at the end

## Skills activated mid-flight

If a skill activates AFTER `publish-plan.sh` has already written the plan, do NOT re-publish. Record it with the executing-plans script — like every write after publish, it goes through a script, never the CLI:

```bash
cat > /tmp/skill.json <<'EOF'
{"plan_id": "<plan_id>", "name": "<skill>", "source": "deferred-load", "step_id": "<step where it surfaced>", "reason": "<why>"}
EOF
bash ${CLAUDE_PLUGIN_ROOT}/skills/executing-plans/scripts/record-skill.sh /tmp/skill.json
```

Source for these is almost always `deferred-load` (or `auto-search` if checking the skill list at that moment surfaced it).

## Inspection

```bash
sqlite3 "${ORCH_DB:-$HOME/skill-workspace/orchestrator.db}" \
  "SELECT skill_name, source, COUNT(*) FROM SkillActivations \
   WHERE plan_id='<plan_id>' GROUP BY skill_name, source;"
```

The dashboard renders these as colored pills with source badges (⚖️ iron-law, 🔍 auto-search, 💬 explicit-mention, ⏳ deferred-load).
