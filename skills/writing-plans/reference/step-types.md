# Step Types Reference

Every step in a plan has a `step_type` that powers the dashboard's per-row icon and audit analytics. Pick the most accurate of these six values.

| Type            | Icon | When to use                                                                                  |
|-----------------|------|----------------------------------------------------------------------------------------------|
| `THINKING`      | 🧠   | Pure reasoning / planning / design. No tool use yet.                                         |
| `ANALYSIS`      | 🔍   | Reading files / grepping / inspecting state to understand current reality.                   |
| `CODE`          | 💻   | Writing or modifying code/templates/config (Write / Edit, or deleting files).                |
| `COMMAND`       | ⚡   | Running shell commands (Bash): tests, migrations, git, sync. Executed through `run-step.sh`.  |
| `DOCUMENTATION` | 📝   | Writing/updating README / SKILL.md / spec / comment-heavy files.                             |
| `SUB_AGENT`     | 🤖   | Dispatching a sub-agent (Agent tool) to delegate the work (input + output captured).         |

## How the type is set

In the plan-input file, attach `type` to a step object:

```json
{ "description": "CODE: implement deep_health()", "type": "CODE" }
```

Or use the bare-string convention (executing-plans will infer the type from the prefix):

```json
"CODE: implement deep_health()"
```

`SUB_AGENT` steps **MUST** also capture `agent_input` and `agent_output` at run time — the executing-plans skill enforces this.

Untyped steps render as `— untyped` in the dashboard. Allowed for legacy plans, discouraged for new ones.
