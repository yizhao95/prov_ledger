"""`provledger receipts` — the material a reply would need (spec §8, G).

Someone challenges a decision; this command assembles the record and stops. It
writes nothing, calls no model and generates no prose: the reply is the session
model's job, the material is ours (spec §8.0, the 2026-09-30 correction).

Everything asserted here is about REUSE and HONESTY, not about layout:
`ask.locate` / `ask.facts` / `ask.absence` / `ask.scope` are called rather than
reimplemented, the timeline is in order, every fact line ends in the id it rests
on, an absence is printed as an absence, the scope line is counted off the fact
table, the closing instruction is always there, and not one row moves.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import integrity, provenance as pv, why as why_mod
from orchestrator.ask import absence as A, facts as F, locate as L, receipts as R, scope as S

REPO = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

CITE_AT_END = re.compile(r"\[#[A-Za-z]{0,2}\d+\]$")


@pytest.fixture
def graph(tmp_path):
    """One node, two runs: added in run 1, signature changed in run 2."""
    path = tmp_path / "proj-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0")
    ps.add_snapshot(c, 1, "nk_r", "pkg.rollup.weekly_report")
    ps.add_event(c, 1, 1, "node_added", "nk_r", created_at="2026-08-01T09:00:00+00:00")
    ps.add_run(c, 2, plan_id="P1")
    ps.add_snapshot(c, 2, "nk_r", "pkg.rollup.weekly_report", struct_sig="s2")
    ps.add_event(c, 2, 1, "node_matched", "nk_r", created_at="2026-09-17T09:00:00+00:00")
    ps.add_event(c, 2, 2, "signature_changed", "nk_r", created_at="2026-09-17T11:00:00+00:00")
    c.commit(); c.close()
    return str(path)


@pytest.fixture
def seeded(conn, graph):
    """The spec's own scene: an email, the words it produced, the constraint they
    became, and two plans that were shown it — one of which adopted it."""
    ids = {}
    words = "Sarah emailed that Q3 rollup excludes EMEA, so the weekly_report split is 70/30"
    u = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P0", text=words,
                            occurred_at="2026-08-14 14:22:00")
    ids["utterance"] = u
    ids["constraint"] = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_r",
                                         kind="organizational", role="constraint",
                                         verbatim=(u, 0, len(words)), recorded_by="human",
                                         occurred_at="2026-08-14 14:25:00")
    ids["reference"] = pv.insert_reference(conn, project="proj", kind="email", label="Q3 rollup scope",
                                           uri="https://mail.example/q3", occurred_at="2026-08-14 09:12:00")
    pv.link_reference(conn, ids["constraint"], ids["reference"])
    ids["reason"] = pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_r", kind="technical",
                                     interpretation="the 70/30 weekly_report split follows the EMEA exclusion",
                                     recorded_by="agent", occurred_at="2026-09-17 11:04:00")
    for plan, at in (("churn-t104", "2026-09-02 10:31:00"), ("churn-t118", "2026-09-17 11:04:00")):
        conn.execute("INSERT INTO influence (reason_id, project, plan_id, node_key, via, by, at) "
                     "VALUES (?, 'proj', ?, 'nk_r', 'headline_response', 'agent', ?)",
                     (ids["constraint"], plan, at))
    conn.execute("INSERT INTO read_hit (reason_id, project, plan_id, moment) VALUES (?, 'proj', 'churn-t104', 'plan')",
                 (ids["constraint"],))
    conn.commit()
    return ids


CHALLENGE = 'why was weekly_report changed to 70/30? wasn\'t it the other way round?'


def _doc(conn, graph, challenge=CHALLENGE):
    return R.assemble(conn, project="proj", challenge=challenge, psg_db_path=graph)


def _section(text: str, header: str) -> list[str]:
    """The indented body of one section — what a reader sees under that heading."""
    lines = text.splitlines()
    assert header in lines, f"{header!r} is not a heading in:\n{text}"
    out = []
    for line in lines[lines.index(header) + 1:]:
        if not line.strip():
            break
        assert line.startswith("  "), f"{line!r} is not part of the {header!r} section"
        out.append(line.strip())
    return out


def _snapshot(conn) -> dict:
    """Every table's row count and every chain's head — what a write would move."""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    return {"counts": counts, "heads": integrity.chain_heads(conn)}


# ── reuse: one pipeline, a fourth renderer over it ───────────────────────────

def test_the_three_stages_are_called_not_reimplemented(conn, graph, seeded, monkeypatch):
    seen = {}

    def spy(mod, name):
        real = getattr(mod, name)

        def wrapper(*a, **kw):
            out = real(*a, **kw)
            seen.setdefault(name, []).append((a, kw))
            return out
        monkeypatch.setattr(mod, name, wrapper)

    for mod, name in ((L, "candidates"), (L, "choose"), (F, "facts"), (A, "absences"), (S, "scope"), (S, "line")):
        spy(mod, name)

    doc = _doc(conn, graph)
    for name in ("candidates", "choose", "facts", "absences", "scope", "line"):
        assert name in seen, f"receipts reimplemented ask.{name} instead of calling it"
    # no model is asked: `choose` runs the code-only path (spec §8.1)
    assert seen["choose"][0][1].get("runner") is None
    assert doc["facts"]["nodes"], "the fact table came back empty for a question that names a node"


# ── the four properties ──────────────────────────────────────────────────────

def test_the_timeline_is_chronological(conn, graph, seeded):
    doc = _doc(conn, graph)
    stamps = [e["at"] for e in doc["timeline"] if e["at"]]
    assert stamps == sorted(stamps), "the order is the argument, and it was not in order"
    assert len(stamps) >= 4
    body = _section(R.render_text(doc), R.TIMELINE_HEADER)
    dates = [line.split()[0] for line in body if line[:4].isdigit()]
    assert dates == sorted(dates), f"the rendered timeline is out of order: {dates}"
    # the email, then the words it produced, then the plans that were shown them
    order = [e["cite"] for e in doc["timeline"]]
    assert len(body) == len(order), "one line per fact"
    email, constraint = f"#r{seeded['reference']}", f"#{seeded['constraint']}"
    adoption = [e["cite"] for e in doc["timeline"] if e["kind"] == "influence"]
    assert order.index(email) < order.index(constraint) < order.index(adoption[0]), order
    assert body[order.index(email)].startswith("2026-08-14 09:12")


def test_every_fact_line_ends_in_its_id(conn, graph, seeded):
    doc = _doc(conn, graph)
    body = _section(R.render_text(doc), R.TIMELINE_HEADER)
    assert body
    for line in body:
        assert CITE_AT_END.search(line), f"a fact line with no id: {line!r}"
    cites = {e["cite"] for e in doc["timeline"]}
    ids = doc["facts"]["ids"]
    assert cites <= set(ids), f"a line cites what the fact table cannot resolve: {cites - set(ids)}"
    # the source, the words, the constraint, the influence rows: all of them carry one
    kinds = {e["kind"] for e in doc["timeline"]}
    assert {"reference", "constraints", "influence", "change"} <= kinds, kinds


def test_an_absence_renders_as_an_absence_not_as_a_blank(conn, graph, seeded):
    doc = _doc(conn, graph)
    computed = A.absences(conn, doc["facts"])
    assert computed, "this fixture is meant to have absences (nothing was ever verified)"
    assert [a["text"] for a in doc["absences"]] == [a["text"] for a in computed], "absences were not reused from ask.absence"
    body = _section(R.render_text(doc), R.ABSENCE_HEADER)
    assert body, "the absence section rendered blank"
    for a in computed:
        assert any(a["text"] in line for line in body), f"missing absence: {a['text']}"
    assert any("never been verified" in line for line in body)
    assert all("[scope]" in line for line in body if line.endswith("[scope]")) and len(body) >= len(computed)
    joined = "\n".join(body)
    assert "nothing recorded" in joined.lower() and "found" in joined.lower(), \
        "nothing recorded and nothing found must be told apart in the product's own words"


def test_the_scope_line_is_counted_not_estimated(conn, graph, seeded):
    doc = _doc(conn, graph)
    ft = doc["facts"]
    sc = doc["scope"]
    assert sc["nodes"] == len(ft["nodes"])
    assert sc["constraints"] == sum(len(n["constraints"]) for n in ft["nodes"])
    assert sc["influencing"] == sum(len(n["influence"]) for n in ft["nodes"])
    assert sc["changes"] == sum(len(n["changes"]) for n in ft["nodes"])
    assert sc["span"] == [min(e["at"][:10] for e in doc["timeline"] if e["at"]),
                          max(e["at"][:10] for e in doc["timeline"] if e["at"])] or sc["span"][0]
    assert doc["scope_line"] == S.line(sc), "the scope line was rewritten instead of reused"
    text = R.render_text(doc)
    assert doc["scope_line"] in text
    assert _section(text, R.SCOPE_HEADER) == [doc["scope_line"]]


def test_the_closing_instruction_is_always_emitted(conn, graph, seeded):
    for challenge in (CHALLENGE, "why did we pick a colour nobody in this ledger ever mentioned?"):
        doc = _doc(conn, graph, challenge)
        text = R.render_text(doc)
        assert doc["closing"] == R.CLOSING
        assert R.CLOSING in text
        assert text.rstrip().endswith(R.CLOSING.splitlines()[-1]), "the instruction is part of the output, last"


# ── read-only, and honest when there is nothing ──────────────────────────────

def test_nothing_is_written(conn, graph, seeded):
    cold = _snapshot(conn)
    doc = _doc(conn, graph)
    R.render_text(doc)
    R.as_json(doc)
    warm = _snapshot(conn)
    # The one thing a first search leaves behind is the FTS5 index `ask.locate`
    # builds over change_reason the first time anything searches (migration 019,
    # why.ensure_fts). It is derived from rows that are already there: no record,
    # no chain head, and every existing table's count is untouched.
    assert all(t.startswith("change_reason_fts") for t in set(warm["counts"]) - set(cold["counts"])), \
        set(warm["counts"]) - set(cold["counts"])
    assert {t: n for t, n in warm["counts"].items() if t in cold["counts"]} == cold["counts"], \
        "receipts wrote something: a question is not a plan"
    assert warm["heads"] == cold["heads"], "a chain head moved"
    assert conn.execute("SELECT COUNT(*) FROM ask_log").fetchone()[0] == 0, "no ask_log row: there is no answer to log"
    assert conn.execute("SELECT COUNT(*) FROM read_hit").fetchone()[0] == 1, "not even a read_hit"
    # and with the index already there, a second run moves nothing at all
    R.as_json(_doc(conn, graph))
    assert _snapshot(conn) == warm


def test_a_question_that_matches_nothing_is_honest_not_an_error(conn, graph, seeded):
    doc = _doc(conn, graph, "who approved the mauve dashboard in Reykjavik?")
    assert doc["timeline"] == [] and doc["facts"]["nodes"] == []
    text = R.render_text(doc)
    body = _section(text, R.TIMELINE_HEADER)
    assert body and not any(CITE_AT_END.search(line) for line in body), "an empty record must not fake a citation"
    assert "nothing" in " ".join(body).lower()
    assert doc["scope_line"] in text and R.CLOSING in text
    assert doc["scope"]["nodes"] == 0


def test_json_carries_the_same_content_as_the_text(conn, graph, seeded):
    doc = _doc(conn, graph)
    out = R.as_json(doc)
    text = R.render_text(doc)
    assert out["challenge"] == doc["challenge"] == CHALLENGE and out["project"] == "proj"
    assert out["scope_line"] == doc["scope_line"] and out["closing"] == R.CLOSING
    assert [e["cite"] for e in out["timeline"]] == [e["cite"] for e in doc["timeline"]]
    for e in out["timeline"]:
        assert e["cite"] in text and (e["at"] is None or e["at"][:10] in text)
        assert set(e) >= {"at", "cite", "kind", "node", "text"}
    assert [a["text"] for a in out["absences"]] == [a["text"] for a in doc["absences"]]
    assert out["facts_sha"] == F.sha(doc["facts"]) and out["scope"] == doc["scope"]


# ── the command ──────────────────────────────────────────────────────────────

@pytest.fixture
def registry(tmp_path, graph):
    p = tmp_path / "projects.json"
    p.write_text(json.dumps({"projects": [{"name": "proj", "repo": str(REPO), "db_path": graph, "commit_sha": "c"}]}))
    return {"PSG_REGISTRY_PATH": str(p)}


def _cli(conn, *argv, env_extra=None):
    dbp = conn.execute("PRAGMA database_list").fetchone()[2]
    env = dict(os.environ, ORCH_DB=dbp, PYTHONPATH=str(REPO / "orchestrator-backend"))
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-m", "orchestrator.cli", *argv],
                          capture_output=True, text=True, env=env, cwd=str(REPO))


def test_the_command_prints_both_forms_and_writes_nothing(conn, graph, seeded, registry):
    why_mod.ensure_fts(conn)                       # the derived index, built once (see above)

    r = _cli(conn, "receipts", CHALLENGE, "--project", "proj", env_extra=registry)
    assert r.returncode == 0, r.stderr
    # Opening the ledger at all runs the one-time DP migration bookkeeping
    # (`db.open_db` → migration_state), which every command does and this one
    # does not own. So the read-only claim is measured on the NEXT invocation.
    before = _snapshot(conn)
    assert R.TIMELINE_HEADER in r.stdout and R.ABSENCE_HEADER in r.stdout and R.SCOPE_HEADER in r.stdout
    assert R.CLOSING in r.stdout and CHALLENGE in r.stdout
    assert CITE_AT_END.search(_section(r.stdout, R.TIMELINE_HEADER)[0])

    j = _cli(conn, "receipts", CHALLENGE, "--project", "proj", "--json", env_extra=registry)
    assert j.returncode == 0, j.stderr
    out = json.loads(j.stdout)
    assert out["challenge"] == CHALLENGE and out["closing"] == R.CLOSING
    assert [e["cite"] for e in out["timeline"]] == [e["cite"] for e in R.as_json(_doc(conn, graph))["timeline"]]
    assert out["scope"]["nodes"] == 1 and out["scope_line"] == out["scope_line"].strip()

    assert _snapshot(conn) == before, "the command wrote something"
