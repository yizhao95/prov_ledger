#!/usr/bin/env bash
# demo-provenance.sh — the product's claim, in miniature, on real plumbing.
#
#   Task A (a person)  removes orders.discount from the rollup. The reason is
#                      their own sentence, carried by an email:
#                      "收到上游通知，v2 以后不再有 discount 列"
#   Task B (an agent)  plans to compute a discount rate from that same column.
#                      At publish, the heads-up finds the removal AND the words
#                      behind it, the agent revises and CITES the record, and
#                      that citation becomes one influence row.
#
# Everything is built with the plugin's own scripts — project-state-graph's
# init_project.sh, provledger note, publish-plan.sh, headline-respond.sh — on a
# scratch ORCH_DB and a scratch registry. No hand-written SQL, and nothing
# touches ~/skill-workspace: a demo that corrupts the ledger it demonstrates is
# not a demo.
#
#   DEMO_HOME=/tmp/x bash examples/phantom-uplift/demo-provenance.sh
#
# Idempotent: re-running finds the work already done and changes no counts.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEMO_HOME="${DEMO_HOME:-${TMPDIR:-/tmp}/provledger-demo}"
export ORCH_DB="${ORCH_DB:-$DEMO_HOME/orchestrator.db}"
export PSG_REGISTRY_PATH="${PSG_REGISTRY_PATH:-$DEMO_HOME/projects.json}"
PROJECT="${DEMO_PROJECT:-phantom-uplift-demo}"
SRC="$DEMO_HOME/repo"
OUT="$DEMO_HOME/graph"
STAMP="$DEMO_HOME/.done"

PY="${PYBIN:-$HOME/skill-workspace/.venv/bin/python}"
[[ -x "$PY" ]] || PY="$(command -v python3)"
WP="$REPO_ROOT/skills/writing-plans/scripts"
EP="$REPO_ROOT/skills/executing-plans/scripts"
PSG="$REPO_ROOT/skills/project-state-graph/scripts"

VERBATIM='Drop orders.discount from the rollup — upstream said the v2 feed no longer carries it'
DECLARED_WORDS='EMEA is excluded from the Q3 rollup — the steering group decided that on 2026-03-14'
EMAIL_LABEL='Re: orders feed v2 schema (demo)'

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

if [[ -f "$STAMP" ]]; then
  say "already built — nothing to do (idempotent)"
  cat "$DEMO_HOME/urls.txt"
  exit 0
fi

mkdir -p "$DEMO_HOME" "$SRC" "$OUT"

# ── the repo the demo talks about ───────────────────────────────────────────
say "1/8  a small repo: the rollup reads orders.discount; another team's tile leans on it"
mkdir -p "$SRC/pkg"
cat > "$SRC/pkg/__init__.py" <<'PYEOF'
PYEOF
cat > "$SRC/pkg/rollup.py" <<'PYEOF'
"""Weekly revenue rollup for the demo checkout feed."""


def load_orders(rows):
    """Read the orders feed. v1 carries `discount` on every row."""
    return [{"order_id": r["order_id"], "amount": r["amount"], "discount": r["discount"]} for r in rows]


def discount_rate(orders):
    """What fraction of gross was given away this week. Reads orders.discount."""
    gross = sum(o["amount"] for o in orders) or 1
    return sum(o["discount"] for o in orders) / gross


def weekly_report(rows):
    """The number the weekly report prints — it leans on discount_rate."""
    orders = load_orders(rows)
    return {"revenue": sum(o["amount"] for o in orders), "discount_rate": discount_rate(orders)}
PYEOF
cat > "$SRC/pkg/tiles.py" <<'PYEOF'
"""The exec dashboard's tiles. Owned by another team — which is the point."""
from pkg.rollup import discount_rate, load_orders


def dashboard_tile(rows):
    """The promo tile on the exec dashboard, still leaning on discount_rate."""
    orders = load_orders(rows)
    return {"label": "promo", "value": discount_rate(orders)}
PYEOF
( cd "$SRC" && git init -q . && git add -A && git -c user.email=demo@example.com -c user.name=demo commit -qm "v1: the rollup reads discount" )

say "2/8  build the state graph (the plugin's own initializer)"
bash "$PSG/init_project.sh" --name "$PROJECT" --repo "$SRC" --out-dir "$OUT" >"$DEMO_HOME/init1.log" 2>&1 \
  || { tail -20 "$DEMO_HOME/init1.log"; exit 1; }

# ── Task A: a person removes the column, and says why ───────────────────────
say "3/8  task A — a person removes orders.discount and records WHY, with the email"
cat > "$DEMO_HOME/plan-a.json" <<JSONEOF
{
  "goal": "Drop orders.discount: the v2 upstream feed no longer provides it",
  "prefix": "demo-task-a",
  "project": "$PROJECT",
  "user_query": "$VERBATIM",
  "declared_targets": ["pkg.rollup.load_orders", "pkg.rollup.discount_rate", "pkg.rollup.weekly_report"],
  "skills": [{"name": "writing-plans", "source": "iron-law"}],
  "steps": [{"description": "CODE: drop the discount column from load_orders and delete discount_rate", "type": "CODE"}]
}
JSONEOF
PLAN_A="$(bash "$WP/publish-plan.sh" "$DEMO_HOME/plan-a.json" 2>/dev/null | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["plan_id"])')"
echo "    plan A = $PLAN_A"

# the sentence itself, with the email that carried it — `provledger note`, not SQL
PYTHONPATH="$REPO_ROOT/orchestrator-backend" "$PY" -m orchestrator.cli note "$VERBATIM" \
  --at "$(date -u +'%Y-%m-%d %H:%M')" --project "$PROJECT" --plan "$PLAN_A" \
  --node "pkg.rollup.discount_rate" --kind technical \
  --ref "kind=email,label=$EMAIL_LABEL,uri=mailto:data-platform@example.com" >/dev/null

# the change itself
cat > "$SRC/pkg/rollup.py" <<'PYEOF'
"""Weekly revenue rollup for the demo checkout feed."""


def load_orders(rows):
    """Read the orders feed. v2 no longer carries `discount`."""
    return [{"order_id": r["order_id"], "amount": r["amount"]} for r in rows]


def weekly_report(rows):
    """discount_rate is gone: the report can no longer print a discount rate."""
    orders = load_orders(rows)
    return {"revenue": sum(o["amount"] for o in orders)}
PYEOF
# The rule itself. `provledger note` records WHY something happened; a rule is
# what the NEXT person gets told, so it goes in through the plugin's own
# constraints API (the same call ledger_store.add_entry makes). It is anchored on
# the other team's tile as well as the rollup — that is the whole point.
PYTHONPATH="$REPO_ROOT/orchestrator-backend" "$PY" - "$ORCH_DB" "$PROJECT" "$EMAIL_LABEL" <<'PYX'
import sys
from orchestrator import constraints as oc, db as odb
conn = odb.open_db(sys.argv[1]); odb.run_migrations(conn)
oc.record_constraint(conn, project=sys.argv[2],
                     subjects=["pkg.tiles.dashboard_tile", "pkg.rollup.load_orders"],
                     statement="Do not depend on orders.discount: the v2 upstream feed no longer provides it",
                     rationale="Upstream notified by email; the v2 schema is published",
                     why_ref=sys.argv[3], why_visibility="shared")
conn.commit(); conn.close()
PYX

# NOTE: pkg/tiles.py is deliberately NOT updated. That is the failure class this
# demo reproduces: the person who removed the function did not know about the
# other team's caller, and nothing at the time told them.
( cd "$SRC" && git add -A && git -c user.email=demo@example.com -c user.name=demo commit -qm "v2: discount is gone upstream" )

say "4/8  refresh the graph — the removal becomes an observation"
bash "$PSG/init_project.sh" --name "$PROJECT" --repo "$SRC" --out-dir "$OUT" >"$DEMO_HOME/init2.log" 2>&1 \
  || { tail -20 "$DEMO_HOME/init2.log"; exit 1; }
STEP_A="$("$PY" - "$ORCH_DB" "$PLAN_A" <<'PYEOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
r = c.execute("SELECT step_id FROM Steps WHERE plan_id = ? ORDER BY execution_order LIMIT 1", (sys.argv[2],)).fetchone()
print(r[0] if r else "")
PYEOF
)"
cat > "$DEMO_HOME/done-a.json" <<JSONEOF
{"step_id": "$STEP_A", "type": "CODE", "summary": "discount column dropped", "log_context": "v2: discount is gone upstream"}
JSONEOF
bash "$EP/complete-step.sh" "$DEMO_HOME/done-a.json" >/dev/null 2>&1 || true

# ── the third core: a sentence that is not in the code at all ───────────────
say "5/8  declare the world outside the code — a steering-group decision, in one sentence"
DECLARE_OUT="$(PYTHONPATH="$REPO_ROOT/orchestrator-backend" "$PY" -m orchestrator.cli node declare \
  "EMEA excluded from Q3 rollup" --type stakeholder_decision \
  --links-to "pkg.rollup.weekly_report" --link-kind declared_constrains \
  --attr "decided_on=2026-03-14" --attr "scope=Q3" --project "$PROJECT" --json)"
DRAFT_ID="$(printf '%s' "$DECLARE_OUT" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
echo "    draft $DRAFT_ID — nothing is in the graph yet"
PYTHONPATH="$REPO_ROOT/orchestrator-backend" "$PY" -m orchestrator.cli node declare \
  --confirm "$DRAFT_ID" --words "$DECLARED_WORDS" --at "2026-03-14 09:00" \
  --project "$PROJECT" --json >"$DEMO_HOME/declare.json"
echo "    confirmed in the user's own words — the row is stated, and the rule is anchored on what it constrains"

say "6/8  refresh again — the declaration becomes a node like any other"
bash "$PSG/init_project.sh" --name "$PROJECT" --repo "$SRC" --out-dir "$OUT" >"$DEMO_HOME/init3.log" 2>&1 \
  || { tail -20 "$DEMO_HOME/init3.log"; exit 1; }

# ── Task B: an agent plans to use the column that is gone ───────────────────
say "7/8  task B — an agent plans to use it again; the heads-up carries the rule, the words and the decision"
cat > "$DEMO_HOME/plan-b.json" <<JSONEOF
{
  "goal": "Add a discount-rate metric to the weekly report",
  "prefix": "demo-task-b",
  "project": "$PROJECT",
  "user_query": "Add a discount rate to the weekly report: orders.discount over amount",
  "declared_targets": ["pkg.tiles.dashboard_tile", "pkg.rollup.weekly_report"],
  "skills": [{"name": "writing-plans", "source": "iron-law"}],
  "steps": [{"description": "CODE: add discount_rate back onto the weekly rollup", "type": "CODE"}]
}
JSONEOF
PUBLISH_B="$(bash "$WP/publish-plan.sh" "$DEMO_HOME/plan-b.json" 2>"$DEMO_HOME/headline-b.txt")"
PLAN_B="$(printf '%s' "$PUBLISH_B" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["plan_id"])')"
echo "    plan B = $PLAN_B"
echo "    ── the heads-up task B got ──"
sed 's/^/    /' "$DEMO_HOME/headline-b.txt" | head -20

# the agent answers the blocking finding: revise, citing the record it was shown
FINDING="$("$PY" - "$ORCH_DB" "$PLAN_B" <<'PYEOF'
import json, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
row = c.execute("SELECT findings_json FROM headline WHERE plan_id = ? ORDER BY id DESC LIMIT 1", (sys.argv[2],)).fetchone()
if not row:
    print(""); raise SystemExit
for f in json.loads(row[0])["findings"]:
    if f["kind"] in ("removed_upstream", "active_constraint"):
        print(json.dumps({"id": f["id"], "cites": [f["evidence"]["reason_id"]] if "reason_id" in f["evidence"] else []}))
        break
else:
    print("")
PYEOF
)"
if [[ -n "$FINDING" ]]; then
  say "8/8  the agent revises, citing the record — that citation is the influence row"
  "$PY" - "$FINDING" "$PLAN_B" "$DEMO_HOME/respond.json" <<'PYEOF'
import json, sys
f = json.loads(sys.argv[1])
json.dump({"plan_id": sys.argv[2], "finding_id": f["id"], "action": "revise",
           "rationale": "discount is gone upstream; estimate from list_price - amount instead and hold the metric",
           "cites": f["cites"], "by": "agent"}, open(sys.argv[3], "w"), ensure_ascii=False)
PYEOF
  bash "$EP/headline-respond.sh" "$DEMO_HOME/respond.json" >/dev/null
else
  echo "    !! no finding reached task B — the scenario did not reproduce" >&2
  exit 1
fi

# ── what to look at ─────────────────────────────────────────────────────────
NODE="pkg.tiles.dashboard_tile"
REASON="$("$PY" - "$ORCH_DB" <<'PYEOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
r = c.execute("SELECT reason_id FROM influence ORDER BY id DESC LIMIT 1").fetchone()
print(r[0] if r else "")
PYEOF
)"
cat > "$DEMO_HOME/urls.txt" <<URLEOF

Read in this order (the dashboard reads \$ORCH_DB):

  1. Task B — the agent's plan, and the decisions it relied on
     /plan/$PLAN_B?node=$NODE&at=reason:$REASON
  2. Node — when the column went, and the reason recorded at the time
     /node/$PROJECT/$NODE?at=reason:$REASON
  3. Task A — the person's plan, their words and the email
     /plan/$PLAN_A?node=$NODE&at=reason:$REASON
  4. Graph — reduced view, nodes with records
     /graph/$PROJECT?mode=story

  ORCH_DB=$ORCH_DB
  PSG_REGISTRY_PATH=$PSG_REGISTRY_PATH
URLEOF
cat "$DEMO_HOME/urls.txt"
touch "$STAMP"
