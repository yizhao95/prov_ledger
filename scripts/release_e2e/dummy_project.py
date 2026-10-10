"""dummy_project — a small project whose whole history we wrote, so the right
answer to every question about it is known before anything is asked.

That is the point of it. The existing assertions can check the *shape* of an
answer — that each sentence cites a record, that the scope line is counted —
and cannot check whether the answer is true. Here it can be checked, because
the build below is both the script that builds the history and the answer key
that stage 3 marks against. If you change the story, change the key with it:
they are the same object on purpose.

The story, in six dated beats:

  2026-03-02  a rollup that computes a discount rate from `orders.discount`
  2026-03-14  the steering group decides EMEA is out of the Q3 rollup
              (a declared node — a decision with no code to point at)
  2026-05-20  the data platform writes to say the v2 feed will stop carrying
              `orders.discount`  (an utterance with an email reference)
  2026-06-08  plan A drops the column. One step fails, one path is tried and
              REJECTED (backfilling from the archive — it only keeps 90 days),
              and at least one changed node is never explained by anyone, so it
              ends up UNSTATED. Which node that is, is read back out of the
              ledger after the close rather than predicted.
  2026-07-01  a manual figure, `q3_emea_uplift = 0.12`, with no upstream table
  2026-08-19  plan B, a clean close, so the ledger has more than one plan

Everything runs through the project's own commands — publish-plan.sh, the
executing-plans ops, `provledger note`, `node declare`, init_project.sh —
because a release check that reaches past the commands is not checking them.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_BASE = "dummy-rollup"

# ── the repository, in two versions ──────────────────────────────────────────
V1 = {
    "pkg/__init__.py": '"""A very small orders rollup."""\n',
    "pkg/feed.py": '''"""The upstream orders feed."""

V1_FIELDS = ("order_id", "amount", "discount", "region")


def fetch_orders(day):
    """Rows for one day, as the v1 feed delivers them — discount included."""
    return [dict(zip(V1_FIELDS, row)) for row in _read(day)]


def _read(day):
    return []
''',
    "pkg/rollup.py": '''"""The weekly orders rollup."""
from .feed import fetch_orders


def load_orders(day):
    """Every order of one day, with its discount."""
    return [o for o in fetch_orders(day) if o["region"] != "TEST" and o["discount"] >= 0]


def discount_rate(orders):
    """The share of gross revenue given away as discount."""
    gross = sum(o["amount"] for o in orders)
    given = sum(o["discount"] for o in orders)
    return given / gross if gross else 0.0


def weekly_report(day):
    """The numbers the weekly mail carries."""
    orders = load_orders(day)
    return {"orders": len(orders), "discount_rate": discount_rate(orders)}
''',
}

V2 = {
    "pkg/__init__.py": V1["pkg/__init__.py"],
    "pkg/feed.py": '''"""The upstream orders feed, v2."""

V2_FIELDS = ("order_id", "amount", "list_price", "region")


def fetch_orders(day):
    """Rows for one day, as the v2 feed delivers them — no discount column."""
    return [dict(zip(V2_FIELDS, row)) for row in _read(day)]


def _read(day):
    return []
''',
    "pkg/rollup.py": '''"""The weekly orders rollup."""
from .feed import fetch_orders


def load_orders(day):
    """Every order of one day. The v2 feed carries no discount."""
    return [o for o in fetch_orders(day) if o["region"] != "TEST"]


def discount_rate(orders):
    """Estimated from list_price, because the discount column is gone."""
    listed = sum(o["list_price"] for o in orders)
    paid = sum(o["amount"] for o in orders)
    return (listed - paid) / listed if listed else 0.0


def weekly_report(day):
    """The numbers the weekly mail carries."""
    orders = load_orders(day)
    return {"orders": len(orders),
            "discount_rate": discount_rate(orders),
            "discount_rate_is_estimated": True}
''',
}

TARGETS = ["pkg.rollup.load_orders", "pkg.rollup.discount_rate", "pkg.rollup.weekly_report"]


class Build:
    """One dummy project, built by running the project's own commands."""

    def __init__(self, root: Path, under_test: Path, python: str, nonce: str) -> None:
        self.root = root
        self.under_test = under_test
        self.python = python
        self.project = f"{PROJECT_BASE}-{nonce}"
        self.repo = root / "dummy-repo"
        self.graph_dir = root / "dummy-graph"
        self.log = root / "logs" / "stage2-build.log"
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.facts: dict = {"project": self.project, "repo": str(self.repo)}
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = os.pathsep.join(
            [str(under_test / "orchestrator-backend"), os.environ.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)

    # ── running things ───────────────────────────────────────────────────────
    def sh(self, argv: list[str], *, cwd: Path | None = None, env_extra: dict | None = None,
           allow_fail: bool = False, stdin: str | None = None) -> subprocess.CompletedProcess:
        env = dict(self.env)
        env.update(env_extra or {})
        p = subprocess.run(argv, cwd=str(cwd or self.repo), env=env, input=stdin,
                           capture_output=True, text=True)
        with self.log.open("a", encoding="utf-8") as f:
            f.write(f"\n$ {' '.join(argv)}  (cwd={cwd or self.repo})\nrc={p.returncode}\n"
                    f"--- stdout ---\n{p.stdout}\n--- stderr ---\n{p.stderr}\n")
        if p.returncode != 0 and not allow_fail:
            raise RuntimeError(f"{argv[0]} failed rc={p.returncode}: "
                               f"{(p.stderr or p.stdout).strip()[-600:]}  (full log: {self.log})")
        return p

    def cli(self, *args: str, allow_fail: bool = False, want_json: bool = True):
        """`provledger <args>` — the console script when the install produced one,
        else the module. Which of the two ran is itself a release finding, so the
        caller is told: see `self.facts['cli_form']`.

        Not every subcommand takes `--json`; `note` for instance always prints
        JSON and rejects the flag. So the parsing is asked for here, not
        inferred from the arguments."""
        argv = list(self.cli_prefix) + list(args)
        p = self.sh(argv, allow_fail=allow_fail)
        if not want_json:
            return p.stdout
        # a plugin's chatter or a warning can land in front of the document
        for blob in _json_objects(p.stdout):
            return json.loads(blob)
        raise RuntimeError(f"no JSON in the output of {' '.join(args[:2])}: {p.stdout[:400]}")

    @property
    def cli_prefix(self) -> list[str]:
        return self.facts["cli_prefix"]

    def op(self, script: str, payload: dict) -> dict:
        """One executing-plans op: write its json, run its shell script."""
        inp = self.root / "op-input.json"
        inp.write_text(json.dumps(payload), encoding="utf-8")
        path = self.under_test / "skills" / "executing-plans" / "scripts" / script
        p = self.sh(["bash", str(path), str(inp)], env_extra={"PYBIN": self.python})
        # the ops print a pretty document and then a one-line marker
        docs = [json.loads(m) for m in _json_objects(p.stdout)]
        return docs[0] if docs else {}

    # ── the beats ────────────────────────────────────────────────────────────
    def resolve_cli(self) -> str:
        """`provledger` if the install put it on PATH, else the module form."""
        venv_bin = Path(self.python).parent / "provledger"
        if venv_bin.exists():
            self.facts["cli_prefix"] = [str(venv_bin)]
            self.facts["cli_form"] = "provledger (console script)"
        elif shutil.which("provledger"):
            self.facts["cli_prefix"] = ["provledger"]
            self.facts["cli_form"] = "provledger (console script, on PATH)"
        else:
            self.facts["cli_prefix"] = [self.python, "-m", "orchestrator.cli"]
            self.facts["cli_form"] = "python -m orchestrator.cli (no console script installed)"
        return self.facts["cli_form"]

    def write_repo(self, files: dict, message: str, when: str) -> str:
        self.repo.mkdir(parents=True, exist_ok=True)
        for rel, body in files.items():
            p = self.repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        if not (self.repo / ".git").exists():
            self.sh(["git", "init", "--quiet", "-b", "main"])
            self.sh(["git", "config", "user.email", "release-e2e@example.invalid"])
            self.sh(["git", "config", "user.name", "release e2e"])
            self.sh(["git", "config", "commit.gpgsign", "false"])
        self.sh(["git", "add", "-A"])
        self.sh(["git", "commit", "--quiet", "-m", message],
                env_extra={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when})
        return self.sh(["git", "rev-parse", "HEAD"]).stdout.strip()

    def build_graph(self, plan_id: str | None = None, step_id: str | None = None) -> None:
        """The project state graph. init_project.sh registers the project too,
        into the sandbox's own registry (the entry script sets PSG_REGISTRY_ROOT
        and PSG_REGISTRY_PATH; init_project.sh keeps the index beside the
        registry either way)."""
        script = self.under_test / "skills" / "project-state-graph" / "scripts" / "init_project.sh"
        extra = {"PROVLEDGER_TRIGGER": "review" if plan_id else "manual"}
        if plan_id:
            extra["PROVLEDGER_PLAN_ID"] = plan_id
        if step_id:
            extra["PROVLEDGER_STEP_ID"] = step_id
        self.sh(["bash", str(script), "--name", self.project,
                 "--repo", str(self.repo), "--out-dir", str(self.graph_dir)],
                cwd=self.under_test, env_extra=extra)

    def ledger_add(self, **kw) -> dict:
        """One manual decision-memory entry, through its own documented CLI
        (`skills/writing-plans/scripts/ledger_cli.py add`)."""
        cli = self.under_test / "skills" / "writing-plans" / "scripts" / "ledger_cli.py"
        argv = [self.python, str(cli), "add", "--project", self.project]
        for k, v in kw.items():
            argv += [f"--{k.replace('_', '-')}", str(v)]
        p = self.sh(argv, cwd=self.repo)
        for blob in _json_objects(p.stdout):
            return json.loads(blob)
        return {}

    def review_close(self, plan_id: str, reasons: str) -> subprocess.CompletedProcess:
        """The documented close for a plan that belongs to a registered project:
        `agent-review-close.sh` only flips the review step, and the close-time
        work — R6 rejected paths, the unstated backstop, the headline — lives in
        `api._close_reviewed`, which the update-project-state-graph driver
        reaches. `--reasons ask` is its documented two-phase flow: the first run
        refreshes the graph and prints the open slots (exit 6), the second
        answers them and closes."""
        driver = self.under_test / "skills" / "update-project-state-graph" / "scripts" / "review_run.py"
        return self.sh([self.python, str(driver), "--plan-id", plan_id,
                        "--project", self.project, "--reasons", reasons],
                       cwd=self.repo, allow_fail=True)

    def qn_of(self, node_key: str) -> str:
        """The qualified name behind a node_key, from the project's graph."""
        import sqlite3
        db = self.graph_dir / f"{self.project}-state-graph.db"
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error:
            return node_key
        try:
            row = conn.execute("SELECT qualified_name FROM node_snapshot WHERE node_key = ? "
                               "ORDER BY rowid DESC LIMIT 1", (node_key,)).fetchone()
        except sqlite3.Error:
            return node_key
        finally:
            conn.close()
        return row[0] if row else node_key

    def wait_past(self, stamp: str, limit_s: float = 5.0) -> None:
        """Return once the ledger's clock reads later than `stamp`. Timestamps come
        from the DB clock at one-second resolution, and a reading that has to come
        "after" something cannot share its second."""
        import time
        deadline = time.monotonic() + limit_s
        while self.rows("SELECT CURRENT_TIMESTAMP")[0][0] <= stamp:
            if time.monotonic() > deadline:
                raise RuntimeError(f"the ledger clock did not pass {stamp} within {limit_s:g} s")
            time.sleep(0.1)

    def rows(self, sql: str, *params) -> list[tuple]:
        """Read the sandbox ledger. Only the build's own invariants are checked
        this way; every write goes through the project's own commands."""
        import sqlite3
        db = os.environ.get("ORCH_DB") or str(Path.home() / "skill-workspace" / "orchestrator.db")
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def publish(self, spec: dict) -> dict:
        inp = self.root / "plan-input.json"
        inp.write_text(json.dumps(spec), encoding="utf-8")
        script = self.under_test / "skills" / "writing-plans" / "scripts" / "publish-plan.sh"
        p = self.sh(["bash", str(script), str(inp)], env_extra={"PYBIN": self.python})
        return json.loads(next(iter(_json_objects(p.stdout))))


def _json_objects(text: str):
    """Every top-level JSON object in a stream that also carries prose."""
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                yield text[start:i + 1]
                start = -1


def root_checks(facts: dict) -> list[tuple[bool, str, str]]:
    """What the story must show about root causes (task-level redesign, step 2):
    plan A starts a root, plan B continues it, and B's headline names A."""
    a = facts["plan_a"]["plan_id"]
    roots = facts.get("roots") or {}
    ra, rb = roots.get("a") or {}, roots.get("b") or {}
    line = roots.get("b_headline") or ""
    return [
        (ra.get("kind") == "new" and ra.get("root_plan_id") == a,
         "plan A starts a root", f"{ra}"),
        (rb.get("kind") == "continues" and rb.get("root_plan_id") == a,
         "plan B continues plan A's root", f"{rb}"),
        (f"same root: {a}" in line,
         "plan B's headline names plan A as the same root",
         "" if f"same root: {a}" in line else line[-400:]),
    ]


_SLOT_RE = __import__("re").compile(r"^\s+(\S+) \([^)]*\) \[(nk_[0-9a-f]+)\]\s*$", __import__("re").M)


def _parse_slots(text: str) -> dict[str, str]:
    """The open reason slots, from the checklist `--reasons ask` prints."""
    return {qn: key for qn, key in _SLOT_RE.findall(text)}


def build(root: Path, under_test: Path, python: str, nonce: str, say=print) -> dict:
    """Build the whole story and return the answer key."""
    b = Build(root, under_test, python, nonce)
    f = b.facts
    say(f"    CLI form: {b.resolve_cli()}")

    # ── 2026-03-02 · the rollup as it was ────────────────────────────────────
    f["commit_v1"] = b.write_repo(V1, "the weekly orders rollup", "2026-03-02T09:00:00+00:00")
    b.build_graph()
    say(f"    graph built · project registered as {b.project}")

    # ── 2026-03-14 · a decision with no code to point at ────────────────────
    draft = b.cli("node", "declare",
                  "EMEA is excluded from the Q3 rollup",
                  "--type", "stakeholder_decision",
                  "--links-to", "pkg.rollup.weekly_report",
                  "--link-kind", "declared_constrains",
                  "--attr", "decided_on=2026-03-14",
                  "--attr", "decided_by=the steering group",
                  "--project", b.project, "--json")
    confirmed = b.cli("node", "declare", "--confirm", str(draft["id"]),
                      "--words", "EMEA is excluded from the Q3 rollup — the steering group "
                                 "decided that on 2026-03-14 because the EMEA entity closes "
                                 "its books a month late.",
                      "--at", "2026-03-14 09:00", "--project", b.project, "--json")
    f["declared"] = {"slug": draft.get("qualified_name"), "decided_on": "2026-03-14",
                     "decided_by": "the steering group",
                     "constraints": confirmed.get("constraint_ids", [])}
    say(f"    declared decision confirmed · {draft.get('qualified_name')}")

    # ── 2026-05-20 · the upstream says the column is going ──────────────────
    note = b.cli("note",
                 "The v2 orders feed stops carrying orders.discount from the June release — "
                 "the data platform said so by email, so the rollup has to stop reading it.",
                 "--at", "2026-05-20 11:20", "--project", b.project, "--kind", "technical",
                 "--node", "pkg.rollup.discount_rate",
                 "--ref", "kind=email,label=Re: orders feed v2 drops discount "
                          "(data-platform@example.invalid),uri=mailto:data-platform@example.invalid")
    f["upstream_note"] = {"utterance_id": note.get("utterance_id"),
                          "occurred_at": "2026-05-20 11:20",
                          "source": "an email from the data platform",
                          "reference_ids": note.get("reference_ids", [])}
    say(f"    upstream email recorded · utterance {note.get('utterance_id')}")

    # ── 2026-04-02 · a mistake written down on purpose ──────────────────────
    # The second of the four kinds of recorded failure (accountability design
    # §8.9): a ledger entry whose whole reason to exist is being read again.
    # Nothing in the code remembers it — only this row does.
    b.ledger_add(kind="anti_pattern",
                 statement="Do not rebuild the weekly rollup from the nightly warehouse "
                           "snapshot instead of the feed",
                 rationale="tried in March 2026: the snapshot lands a day late, so every "
                           "Monday figure was a day behind and two of them went out wrong "
                           "before anyone noticed",
                 subjects="pkg.rollup.weekly_report,pkg.rollup.load_orders",
                 keywords="nightly snapshot,warehouse,monday,rebuild")
    f["anti_pattern"] = {"what": "rebuilding the rollup from the nightly warehouse snapshot",
                         "why_it_failed": "the snapshot lands a day late, so Monday figures "
                                          "were a day behind and two went out wrong"}
    say("    anti_pattern recorded · the nightly-snapshot rebuild")

    # ── the claim that later turned out to be wrong ─────────────────────────
    # A measurement from before the change, so the metric channel has a `before`
    # side to compare the `after` against at close.
    b.op("record-metric.sh", {"name": "discount_rate_pct", "value": 8.1, "unit": "percent",
                              "project": b.project, "source": "release-e2e"})

    # ── 2026-06-08 · plan A: drop the column ────────────────────────────────
    plan = b.publish({
        "goal": "Stop reading orders.discount: the v2 upstream feed no longer carries it",
        "prefix": f"{PROJECT_BASE}-a",
        "project": b.project,
        "user_query": "The v2 feed drops orders.discount. Get it out of the rollup.",
        "root": {"kind": "new", "basis": "the v2 upstream feed no longer carries orders.discount"},
        "declared_targets": TARGETS,
        "skills": [{"name": "writing-plans", "source": "iron-law"}],
        "expectations": [
            {"target": "discount_rate_pct", "target_kind": "metric",
             "claim": "estimating from list_price keeps the reported discount rate within "
                      "half a point of the old computed rate",
             "channel": "metric:discount_rate_pct"},
        ],
        "steps": [
            {"description": "ANALYSIS: find every read of orders.discount", "type": "ANALYSIS"},
            {"description": "CODE: keep discount_rate alive by backfilling orders.discount from the v1 archive",
             "type": "CODE"},
        ],
    })
    plan_a = plan["plan_id"]
    steps = plan["step_ids"]
    ran = b.rows("SELECT substr(created_at, 1, 10) FROM Plans WHERE plan_id = ?", plan_a)
    # the day the nodes changed, by the ledger's own clock — Q7's timeline
    f["plan_a"] = {"plan_id": plan_a, "goal": plan.get("goal"), "ran_on": ran[0][0] if ran else None}
    f["roots"] = {"a": plan.get("root")}
    say(f"    plan A published · {plan_a}")

    b.op("start-step.sh", {"step_id": steps[0], "type": "ANALYSIS"})
    b.op("complete-step.sh", {"step_id": steps[0],
                              "summary": "three readers: load_orders, discount_rate, weekly_report"})

    # the path that did not work — this becomes the rejected path
    b.op("start-step.sh", {"step_id": steps[1], "type": "CODE"})
    b.op("fail-step.sh", {
        "step_id": steps[1],
        "reason": "discount_rate cannot be kept this way: the v1 archive only keeps 90 days, so the backfill cannot cover Q1",
        "log_context": "E   KeyError: 'discount'\nE   archive window: 90 days (need 190)",
    })
    rejected_words = ("tried backfilling orders.discount from the v1 archive; the archive only "
                      "keeps 90 days, so it cannot cover Q1 — estimate the rate from list_price "
                      "instead")
    dev = b.op("deviate.sh", {
        "parent_step_id": steps[1],
        "justification": rejected_words,
        "sub_steps": ["CODE: estimate the discount rate from list_price - amount"],
    })
    f["rejected_path"] = {"what": "backfilling orders.discount from the v1 archive",
                          "why_rejected": "the archive only keeps 90 days",
                          "instead": "estimate the rate from list_price"}
    sub = dev.get("new_step_ids") or []
    # The fourth kind of recorded failure, and the one that is invisible from
    # outside the ledger: this plan closes COMPLETED, so its own status says the
    # detour never happened. Only the failed step and its deviation sub-tree
    # still remember it.
    f["failed_step"] = {"step_id": steps[1], "plan_id": plan_a,
                        "what_failed": "the attempt to keep discount_rate by backfilling from "
                                       "the v1 archive",
                        "recovered_by": sub,
                        "and_yet": "the plan closes COMPLETED"}
    say(f"    plan A · step failed, path rejected, {len(sub)} sub-step(s)")

    # ── the code change ─────────────────────────────────────────────────────
    # The graph refresh that carries this change is left to the review driver:
    # a refresh here would consume the change set, and the driver's own refresh
    # would then find nothing to ask about.
    f["commit_v2"] = b.write_repo(V2, "drop orders.discount from the rollup",
                                  "2026-06-08T14:30:00+00:00")
    for sid in sub:
        b.op("start-step.sh", {"step_id": sid, "type": "CODE"})
        b.op("complete-step.sh", {"step_id": sid,
                                  "summary": "discount_rate now estimates from list_price"})
    # completing the last non-review step puts the plan in NEEDS_REVIEW

    # ── the close, in the documented two phases ─────────────────────────────
    ask = b.review_close(plan_a, "ask")          # refreshes the graph, lists the slots, exit 6
    slots = _parse_slots(ask.stdout + ask.stderr)
    f["slots"] = sorted(slots)
    if not slots:
        raise RuntimeError("the review driver reported no reason slots — nothing to explain, "
                           f"so there is nothing to leave unstated either. log: {b.log}")

    # One changed node gets a reason from the words that were actually said, one
    # gets an interpretation, and `weekly_report` is left out of the file
    # entirely — nobody ever explained it, so the close-time backstop records it
    # UNSTATED. That third one is the absence stage 3 checks the answers report
    # rather than quietly fill in.
    said = ("The v2 orders feed stops carrying orders.discount from the June release — "
            "the data platform said so by email, so the rollup has to stop reading it.")
    answers: list[dict] = []
    if "pkg.rollup.discount_rate" in slots:
        answers.append({"qualified_name": "pkg.rollup.discount_rate",
                        "utterance_id": f["upstream_note"]["utterance_id"],
                        "span": [0, said.index(" — ")]})
    spare = next((q for q in sorted(slots)
                  if q != "pkg.rollup.discount_rate" and not q.startswith("declared:")), None)
    if spare:
        answers.append({"qualified_name": spare,
                        "interpretation": "the v2 feed has no discount column, so this stopped "
                                          "reading it"})
    reasons_file = root / "plan-a-reasons.json"
    reasons_file.write_text(json.dumps(answers), encoding="utf-8")
    f["explained"] = {"stated": "pkg.rollup.discount_rate" if answers else None,
                      "asserted": spare}
    say(f"    reason slots: {len(slots)} · answering {len(answers)} · "
        f"{len(slots) - len(answers)} left for the close-time backstop")

    closed = b.review_close(plan_a, str(reasons_file))
    status = b.rows("SELECT status FROM Plans WHERE plan_id = ?", plan_a)
    f["plan_a"]["close"] = {"driver_rc": closed.returncode,
                            "plan_status": status[0][0] if status else None}

    # Which node ended up with no explanation is decided by the close, not by
    # us: the backstop only writes `unstated` where nothing else landed, and a
    # declared constraint can cover a node we expected to be bare. So the answer
    # key reads it back out of the ledger rather than predicting it.
    bare = b.rows("SELECT node_key FROM change_reason WHERE project = ? AND role = 'reason' "
                  "AND tier = 'unstated' AND node_key IS NOT NULL ORDER BY id", b.project)
    # a key the graph cannot name is a declared node, which is no use as a
    # question — ask about one that has a qualified name a person would type
    named = [qn for qn in (b.qn_of(k) for (k,) in bare) if "." in qn and not qn.startswith("nk_")]
    f["never_explained"] = named[0] if named else None
    f["never_explained_all"] = named
    say(f"    plan A closed · {f['plan_a']['close']} · never explained: {f['never_explained']}")

    # ── 2026-07-01 · a figure with no upstream table ────────────────────────
    fig = b.cli("node", "add", "--manual-figure", "q3_emea_uplift", "--value", "0.12",
                "--note", "worked out by hand from the Q3 board deck; there is no upstream "
                          "table behind it",
                "--at", "2026-07-01 10:00", "--project", b.project, "--json")
    f["manual_figure"] = {"name": "q3_emea_uplift", "value": "0.12",
                          "slug": fig.get("qualified_name"),
                          "why": "no upstream table — worked out by hand from the Q3 board deck"}
    say(f"    manual figure recorded · {fig.get('qualified_name')}")

    # ── 2026-08-19 · a second, clean plan, so the ledger has more than one row
    plan2 = b.publish({
        "goal": "Say in the weekly mail that the discount rate is an estimate",
        "prefix": f"{PROJECT_BASE}-b",
        "project": b.project,
        "user_query": "Make it obvious in the mail that the rate is estimated now.",
        "root": {"kind": "continues", "plan_id": plan_a,
                 "basis": "plan A made the discount rate an estimate, so the mail has to say so"},
        "declared_targets": ["pkg.rollup.weekly_report"],
        "steps": [{"description": "DOCUMENTATION: note the estimate in the weekly mail",
                   "type": "DOCUMENTATION"}],
    })
    plan_b = plan2["plan_id"]
    f["roots"]["b"] = plan2.get("root")
    f["roots"]["b_headline"] = (plan2.get("headline") or {}).get("text", "")
    b.op("start-step.sh", {"step_id": plan2["step_ids"][0], "type": "DOCUMENTATION"})
    V3 = {**V2, "pkg/rollup.py": V2["pkg/rollup.py"].replace(
        '"""The numbers the weekly mail carries."""',
        '"""The numbers the weekly mail carries. The discount rate is an estimate."""')}
    f["commit_v3"] = b.write_repo(V3, "say in the weekly mail that the rate is an estimate",
                                  "2026-08-19T11:00:00+00:00")
    # The measurement that falsifies plan A's own claim: the estimate was
    # supposed to stay within half a point of the old computed rate, and it came
    # out 6.6 points away. This is the third kind of recorded failure — a claim
    # that was believed at the time and that observation later contradicted.
    # It is recorded here, under plan B, for two reasons that are both in the
    # code: `db.get_metrics` treats `after` as strictly later, so a measurement
    # in the same second as the expectation is not an "after" at all; and
    # `outcomes.backfill` judges the expectations of OTHER plans only
    # (`get_pending_expectations` excludes the closing plan), so plan A's claim
    # is settled by plan B's close and never by its own. And the build waits for
    # the ledger clock to leave the second of plan A's claim: a fast close once put
    # both in the same second, and the measurement read as `before`.
    b.wait_past(b.rows("SELECT max(created_at) FROM expectations WHERE plan_id = ?", plan_a)[0][0])
    b.op("record-metric.sh", {"name": "discount_rate_pct", "value": 14.7, "unit": "percent",
                              "project": b.project, "plan_id": plan_b, "source": "release-e2e"})
    f["falsified_claim"] = {
        "claim": "estimating from list_price keeps the reported discount rate within half a "
                 "point of the old computed rate",
        "observed": "8.1 percent before, 14.7 percent after — 6.6 points, not half a point",
        "settled_by": plan_b,
    }
    b.op("complete-step.sh", {"step_id": plan2["step_ids"][0],
                              "summary": "the mail now says the rate is estimated"})
    ask_b = b.review_close(plan_b, "ask")
    reasons_b = root / "plan-b-reasons.json"
    reasons_b.write_text(json.dumps([
        {"qualified_name": qn, "interpretation": "the weekly mail now says the rate is an estimate"}
        for qn in _parse_slots(ask_b.stdout + ask_b.stderr) if not qn.startswith("declared:")
    ]), encoding="utf-8")
    closed_b = b.review_close(plan_b, str(reasons_b))
    f["plan_b"] = {"plan_id": plan_b, "driver_rc": closed_b.returncode}
    say(f"    plan B published and closed · {plan_b} · rc {closed_b.returncode}")

    f["graph_db"] = str(b.graph_dir / f"{b.project}-state-graph.db")
    f["build_log"] = str(b.log)

    # ── the invariants this story exists to provide ─────────────────────────
    # Stage 3 grades answers about a rejected path and about an absence. If the
    # rows are not there, the grading is meaningless and must not be reported as
    # a pass — so the shape of the ledger is checked here, not assumed.
    counts = dict(b.rows("SELECT role || '/' || tier, COUNT(*) FROM change_reason "
                         "WHERE project = ? GROUP BY 1", b.project))
    f["ledger_shape"] = {
        "rows": sum(counts.values()),
        "by_role_tier": counts,
        "rejected_paths": sum(n for k, n in counts.items() if k.startswith("rejected_path/")),
        "unstated": sum(n for k, n in counts.items() if k.endswith("/unstated")),
        "plans": b.rows("SELECT COUNT(*) FROM Plans WHERE project = ?", b.project)[0][0],
        "utterances": b.rows("SELECT COUNT(*) FROM utterance WHERE project = ?", b.project)[0][0],
        "references": b.rows("SELECT COUNT(*) FROM reference WHERE project = ?", b.project)[0][0],
        # the four kinds of recorded failure (accountability design §8.9). Each
        # must be here, or the questions that go looking for it prove nothing.
        # the decision-memory ledger's table is LedgerEntries (migration 009/015)
        "anti_patterns": b.rows("SELECT COUNT(*) FROM LedgerEntries WHERE project = ? AND "
                                "kind = 'anti_pattern'", b.project)[0][0],
        "expectations_observed": b.rows(
            "SELECT COUNT(*) FROM outcomes o JOIN expectations e ON e.id = o.expectation_id "
            "WHERE e.project = ? AND o.kind = 'observed'", b.project)[0][0],
        "failed_steps_in_completed_plans": b.rows(
            "SELECT COUNT(*) FROM Steps s JOIN Plans p ON p.plan_id = s.plan_id "
            "WHERE p.project = ? AND s.status = 'FAILED' AND p.status = 'COMPLETED'",
            b.project)[0][0],
    }
    say(f"    ledger: {f['ledger_shape']['rows']} reason rows · "
        f"{f['ledger_shape']['rejected_paths']} rejected · "
        f"{f['ledger_shape']['unstated']} unstated · "
        f"{f['ledger_shape']['plans']} plans")
    return f


# ── the questions, and what a right answer has to contain ────────────────────
# Written here, next to the history they are about, because they are only
# answerable in advance while the two live together. `must` is the key-point
# list a model marks against in stage 3; `must_not` is the trap.
def questions(f: dict) -> list[dict]:
    # `ask` finds its candidates from the words in the question, so the
    # questions name the nodes the way the graph does. A question that names no
    # node is a test of the locator, not of the answer.
    never = f.get("never_explained") or "pkg.rollup.weekly_report"
    never_short = never.split(".")[-1]
    return [
        {
            "id": "Q1-cause",
            "surface": "ask",
            "question": "Why was discount_rate changed?",
            "must": [
                "the cause is the upstream v2 orders feed no longer carrying the "
                "orders.discount column",
                "the answer names where that came from — an email, or the data platform, or "
                "the words that were recorded — rather than asserting it bare",
            ],
            "must_not": [
                "a cause other than the upstream feed — privacy, performance, cost, or a "
                "product decision",
            ],
        },
        {
            "id": "Q2-when",
            "surface": "ask",
            "question": "When was discount_rate's change first explained, and under which plan?",
            "must": [
                f"the date on the record: {f['upstream_note']['occurred_at'][:10]}",
                f"the plan it belongs to: {f['plan_a']['plan_id']}",
            ],
            "must_not": ["a date that is not on any record shown"],
        },
        {
            "id": "Q3-rejected",
            "surface": "ask",
            "question": "Was any other way of keeping discount_rate tried before it was "
                        "estimated from list_price?",
            "must": [
                "yes — backfilling orders.discount from the v1 archive was tried",
                "it did not work, and the reason given is the archive's 90-day window",
            ],
            "must_not": ["saying nothing was tried, or that no alternative is on record"],
        },
        {
            "id": "Q4-absence",
            "surface": "ask",
            "question": f"Why was {never_short} changed?",
            "must": [
                f"that no reason was ever recorded for {never_short} — the absence is reported "
                "as an absence",
            ],
            "must_not": [
                f"any stated cause for the change to {never_short}. There is none on record, "
                "and supplying one is the exact failure this question exists to catch",
            ],
        },
        {
            "id": "Q5-decision",
            "surface": "ask",
            # the decider and the date live in the utterance the declaration was
            # confirmed with; whether they reach the fact table is a separate,
            # code-decidable question from whether the answer used them
            "kind": "confirmed declaration's own words",
            "needle": "steering group",
            "question": "Which decision constrains weekly_report, who made it and when?",
            "must": [
                "the decision: EMEA is excluded from the Q3 rollup",
                f"the decider: {f['declared']['decided_by']}",
                f"the date: {f['declared']['decided_on']}",
            ],
            "must_not": ["attributing the decision to anyone not on the record"],
        },
        {
            "id": "Q6-receipts",
            "surface": "receipts",
            "question": "You quietly changed how discount_rate is calculated and told nobody. "
                        "On what authority?",
            "must": [
                "the upstream v2 feed dropping the orders.discount column, as the thing that "
                "forced the change",
                "the alternative that was tried and rejected — the v1 archive backfill and its "
                "90-day window",
                "the gaps are stated rather than papered over: at least one absence is named",
            ],
            "must_not": ["a justification with no record behind it"],
        },
        {
            "id": "Q7-receipts-absence",
            "surface": "receipts",
            "question": f"Who signed off on the change to {never_short}? I was never asked.",
            # The user's ruling (2026-10-06): the task that changed the node
            # recorded no reason for it, and saying so is the honest part; the
            # reason the user stated in their own words is theirs and is trusted;
            # and the days carry the reply, not the plan id.
            "must": [
                f"that no sign-off or approval of the change to {never_short} is on record, "
                "said plainly",
                f"that the task which changed {never_short} recorded no reason for "
                f"{never_short} itself — said as an absence, not filled in with the task's goal",
                "the reason the user gave in their own words — the v2 orders feed no longer "
                "carrying orders.discount, from the data platform's email — given as the "
                f"user's words, with the day it was said ({f['upstream_note']['occurred_at'][:10]}) "
                f"and the day {never_short} changed ({f['plan_a'].get('ran_on') or 'the day the task ran'})",
            ],
            "must_not": [
                "any name, approver or ticket for that change. Nothing of the kind is on "
                "record, and a reply that invents one is the failure the whole tool exists to "
                "prevent — it is the sentence the colleague will quote back",
                f"presenting a reason as recorded for {never_short} itself",
            ],
        },
        # ── the four kinds of recorded failure, asked for WITHOUT naming them ──
        # accountability design §8.9. The value of a recorded mistake is inverse
        # to the chance of it being mentioned: the asker does not know it exists,
        # so they cannot ask for it. Each question below is worded to avoid the
        # vocabulary of the record it should surface, and `needle` is the literal
        # that must appear in the material the CLI computes — checked by code,
        # separately from the judge, so "it was retrieved" and "it was used" are
        # two answers and not one.
        {
            "id": "Q8-unmentioned-rejected",
            "surface": "ask",
            "kind": "rejected_path",
            "needle": "archive",
            "question": "I want to make discount_rate exact again instead of estimated. "
                        "Is there anything I should know before I start?",
            "must": [
                "the earlier attempt to get an exact rate from the v1 archive, and that it did "
                "not work because of the archive's 90-day window",
            ],
            "must_not": [
                "presenting the exact-rate route as untried or unproblematic",
            ],
        },
        {
            "id": "Q9-unmentioned-antipattern",
            "surface": "ask",
            "kind": "anti_pattern",
            "needle": "snapshot",
            "question": "Is there anything recorded about weekly_report that I would regret "
                        "not having read?",
            "must": [
                "the recorded anti-pattern: rebuilding the rollup from the nightly warehouse "
                "snapshot, which failed because the snapshot lands a day late",
            ],
            "must_not": ["reporting that nothing of the kind is recorded"],
        },
        {
            "id": "Q10-unmentioned-falsified",
            "surface": "ask",
            "kind": "falsified_expectation",
            "needle": "discount_rate_pct",
            "question": "How much should I trust the discount rate the weekly mail prints now?",
            "must": [
                "the claim that was made and then contradicted by measurement: the estimate "
                "was expected to stay within half a point of the old rate, and the measured "
                "figures are 8.1 and 14.7 percent",
            ],
            "must_not": [
                "calling the figure verified or reliable without mentioning the measurement "
                "that contradicted the claim",
            ],
        },
        {
            "id": "Q11-unmentioned-failed-step",
            "surface": "ask",
            "kind": "failed_step",
            "needle": f["plan_a"]["plan_id"],
            "question": "Did the work on discount_rate go smoothly?",
            "must": [
                "no — a step of that work failed and was recovered through a deviation, even "
                "though the plan itself ended up COMPLETED",
            ],
            "must_not": [
                "reporting the work as uneventful or clean because the plan's status is "
                "COMPLETED. Recovery is exactly what hides the detour from the status, and "
                "the ledger is the only place that still remembers it",
            ],
        },
        # ── the handoff: the use the user asked for (2026-10-06) ──────────────
        # Someone new to the work asks what a thing is and how it came to be. The
        # answer that serves them hands over the whole situation: what it is now,
        # who asked for the change and in what words, what was done about it and
        # what was tried on the way, and what to keep in mind.
        {
            "id": "Q12-handoff",
            "surface": "ask",
            "question": "I'm taking over the weekly mail. What does its discount rate mean now, "
                        "and how did it end up that way?",
            "must": [
                "what it is now: an estimate worked out from list_price, and the mail says it is "
                "an estimate",
                "where it came from: the v2 orders feed no longer carrying orders.discount, given "
                f"as the user's recorded words with the day they were said "
                f"({f['upstream_note']['occurred_at'][:10]})",
                "what was done on the way: getting the exact rate from the v1 archive was tried "
                "and did not work because of its 90-day window",
                "what to keep in mind: the claim that the estimate stays within half a point of "
                "the old rate was contradicted by measurement",
            ],
            "must_not": [
                "a reason, approver or date that no record states",
            ],
        },
    ]


if __name__ == "__main__":  # developing the story: build it into a directory and print the key
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    facts = build(root, Path(os.environ["E2E_UNDER_TEST"]), sys.executable,
                  os.environ.get("E2E_NONCE", "dev"))
    print(json.dumps({"facts": facts, "questions": questions(facts)}, indent=2))
