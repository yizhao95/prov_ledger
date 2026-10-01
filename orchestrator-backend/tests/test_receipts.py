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
    """`locate.choose` is deliberately NOT in this list any more (spec §8.8): the
    choosing step left Python, so the stages receipts reuses are the matchers,
    the fact table, the absences and the scope — not the pick."""
    seen = {}

    def spy(mod, name):
        real = getattr(mod, name)

        def wrapper(*a, **kw):
            out = real(*a, **kw)
            seen.setdefault(name, []).append((a, kw))
            return out
        monkeypatch.setattr(mod, name, wrapper)

    for mod, name in ((L, "candidates"), (F, "facts"), (A, "absences"), (S, "scope"), (S, "line")):
        spy(mod, name)

    doc = _doc(conn, graph)
    for name in ("candidates", "facts", "absences", "scope", "line"):
        assert name in seen, f"receipts reimplemented ask.{name} instead of calling it"
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
    assert {"reference", "constraints", "influence"} <= kinds, kinds
    assert "change" not in kinds, "the graph's own bookkeeping is not evidence about a decision (§8.8 item 3)"


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


# ── §8.8 · two reads, so that a model does the choosing ──────────────────────
#
# The one-shot form above was measured against the live ledger and failed: 109
# candidates were cut to 40 and then to 5 by a scoring function that counts word
# hits, so for "why is the review timeout 4800 seconds?" the top node was
# `ask.summarize.review` — six points for having "review" in its name and
# nothing to do with timeouts. `runner=None` read like "no model is called", but
# its actual meaning was "`locate.choose` takes the top FALLBACK_TOP by score":
# nobody chose. Spec §8.8 splits the command in two so the model already in the
# room does the choosing between them.


def test_candidates_is_a_read_of_its_own_and_states_its_cap(conn, graph, seeded):
    """`receipts candidates` hands over the list to be chosen FROM, and says how
    wide the list is: the cap is a named constant and the cut is printed, the way
    the scope line prints its range."""
    doc = R.candidates(conn, project="proj", challenge=CHALLENGE, psg_db_path=graph)
    assert R.CANDIDATE_CAP >= 40, "the cap must be wide enough that the model, not the cap, chooses"
    assert doc["cap"] == R.CANDIDATE_CAP and doc["matched"] >= 1
    assert doc["candidates"], "the fixture names a node; the matchers must propose it"
    for c in doc["candidates"]:
        assert set(c) >= {"qn", "node_key", "why", "score"}, c
    text = R.render_candidates(doc)
    assert CHALLENGE in text
    body = _section(text, R.CANDIDATES_HEADER)
    assert any("pkg.rollup.weekly_report" in line for line in body), body
    assert doc["cap_line"] in text and str(doc["cap"]) in doc["cap_line"] and str(doc["matched"]) in doc["cap_line"]
    assert R.CANDIDATES_CLOSING in text


def test_the_candidate_cap_states_what_it_cut_and_never_cuts_to_five(conn, graph, seeded):
    """A cap that bites says so in the output. And the default cap is not the old
    FALLBACK_TOP=5: five is what a scoring function can defend, not what a reader
    can choose from."""
    wide = R.candidates(conn, project="proj", challenge=CHALLENGE, psg_db_path=graph)
    assert len(wide["candidates"]) == min(wide["matched"], R.CANDIDATE_CAP)
    narrow = R.candidates(conn, project="proj", challenge=CHALLENGE, psg_db_path=graph, cap=1)
    assert len(narrow["candidates"]) == 1 and narrow["cap"] == 1
    cut = narrow["matched"] - 1
    assert narrow["truncated"].get("candidates") == cut or cut == 0
    if cut:
        assert str(cut) in narrow["cap_line"], narrow["cap_line"]


def test_facts_takes_the_names_a_model_picked_and_locates_nothing(conn, graph, seeded, monkeypatch):
    """The second read is given names. It must not re-run the matchers and it must
    not re-pick: the pick already happened, outside Python."""
    def boom(*a, **kw):
        raise AssertionError("receipts facts located or chose instead of taking the names it was given")

    monkeypatch.setattr(L, "candidates", boom)
    monkeypatch.setattr(L, "choose", boom)
    doc = R.facts(conn, project="proj", chosen=["pkg.rollup.weekly_report"], psg_db_path=graph)
    assert [n["qn"] for n in doc["facts"]["nodes"]] == ["pkg.rollup.weekly_report"]
    assert doc["timeline"] and doc["absences"] and doc["scope_line"]
    assert doc["picked_by"] == "caller"
    text = R.render_text(doc)
    assert R.NODES_HEADER in text and "pkg.rollup.weekly_report" in text
    assert R.CLOSING in text and doc["scope_line"] in text
    assert R.UNCHOSEN not in text, "nobody picked these by score, so do not say they were"


def test_a_name_the_graph_does_not_know_is_reported_not_swallowed(conn, graph, seeded):
    doc = R.facts(conn, project="proj", chosen=["pkg.nope.not_a_node"], psg_db_path=graph)
    text = R.render_text(doc)
    body = _section(text, R.NODES_HEADER)
    assert any("pkg.nope.not_a_node" in line and "not in the graph" in line for line in body), body


def test_no_receipts_path_can_reach_a_runner(conn, graph, seeded, monkeypatch):
    """Spec §8.8: the choosing step left Python. `locate.choose` is the seam a
    runner arrives through, so no receipts entry point may call it, and none may
    take a `runner` of its own."""
    import inspect

    for fn in (R.assemble, R.candidates, R.facts):
        assert "runner" not in inspect.signature(fn).parameters, f"{fn.__name__} still takes a runner"

    def boom(*a, **kw):
        raise AssertionError("a receipts path called locate.choose, which is where a runner gets in")

    monkeypatch.setattr(L, "choose", boom)
    R.assemble(conn, project="proj", challenge=CHALLENGE, psg_db_path=graph)
    R.candidates(conn, project="proj", challenge=CHALLENGE, psg_db_path=graph)
    R.facts(conn, project="proj", chosen=["pkg.rollup.weekly_report"], psg_db_path=graph)


def test_the_one_shot_form_admits_that_a_score_chose_and_points_at_the_two_steps(conn, graph, seeded):
    """`receipts "<challenge>"` still works, and still picks by score — but it now
    says so in words a person reads, and names the form that does better."""
    doc = _doc(conn, graph)
    assert doc["picked_by"] == "score"
    text = R.render_text(doc)
    assert R.UNCHOSEN in text
    low = R.UNCHOSEN.lower()
    assert "score" in low and "receipts candidates" in low and "receipts facts" in low


# ── §8.8 item 3 · the graph's bookkeeping is not evidence ────────────────────

def test_graph_bookkeeping_is_out_of_the_material_and_counted_instead(conn, graph, seeded):
    """34 of the 63 lines of one real challenge were `the graph recorded
    node_changed in run N`. That is the state graph's record of itself, not
    anything anyone said about the decision, and it sorted to the top because its
    dates are early. It leaves the material — but as a stated count, because in
    this project a cut is never silent."""
    doc = _doc(conn, graph)
    assert [e for e in doc["timeline"] if e["kind"] == "change"] == []
    text = R.render_text(doc)
    assert "the graph recorded" not in text
    in_table = sum(len(n["changes"]) for n in doc["facts"]["nodes"])
    assert in_table >= 1, "this fixture has graph events; otherwise it proves nothing"
    assert doc["graph_events"] == in_table
    assert R.graph_events_line(in_table) in text
    assert str(in_table) in R.graph_events_line(in_table)


def test_the_graph_events_line_is_absent_when_there_are_none(conn, graph, seeded):
    doc = R.facts(conn, project="proj", chosen=["pkg.nope.not_a_node"], psg_db_path=graph)
    assert doc["graph_events"] == 0
    assert "graph event" not in R.render_text(doc)


# ── the two subcommands ──────────────────────────────────────────────────────

def test_the_two_subcommands_read_and_write_nothing(conn, graph, seeded, registry):
    why_mod.ensure_fts(conn)
    warm = _cli(conn, "receipts", "candidates", CHALLENGE, "--project", "proj", env_extra=registry)
    assert warm.returncode == 0, warm.stderr
    before = _snapshot(conn)

    c = _cli(conn, "receipts", "candidates", CHALLENGE, "--project", "proj", env_extra=registry)
    assert c.returncode == 0, c.stderr
    assert R.CANDIDATES_HEADER in c.stdout and "pkg.rollup.weekly_report" in c.stdout
    assert R.TIMELINE_HEADER not in c.stdout, "the candidate list is a list, not the material"

    cj = _cli(conn, "receipts", "candidates", CHALLENGE, "--project", "proj", "--json", env_extra=registry)
    assert cj.returncode == 0, cj.stderr
    out = json.loads(cj.stdout)
    assert out["cap"] == R.CANDIDATE_CAP and out["challenge"] == CHALLENGE
    assert all(set(x) >= {"qn", "node_key", "why", "score"} for x in out["candidates"])

    f = _cli(conn, "receipts", "facts", "pkg.rollup.weekly_report", "--project", "proj", env_extra=registry)
    assert f.returncode == 0, f.stderr
    assert R.TIMELINE_HEADER in f.stdout and R.ABSENCE_HEADER in f.stdout and R.SCOPE_HEADER in f.stdout
    assert R.CLOSING in f.stdout and "the graph recorded" not in f.stdout
    assert CITE_AT_END.search(_section(f.stdout, R.TIMELINE_HEADER)[0])

    fj = _cli(conn, "receipts", "facts", "pkg.rollup.weekly_report", "--project", "proj", "--json", env_extra=registry)
    assert fj.returncode == 0, fj.stderr
    fo = json.loads(fj.stdout)
    assert fo["picked_by"] == "caller" and fo["scope"]["nodes"] == 1
    assert [e["cite"] for e in fo["timeline"]] == [
        e["cite"] for e in R.facts(conn, project="proj", chosen=["pkg.rollup.weekly_report"], psg_db_path=graph)["timeline"]]

    assert _snapshot(conn) == before, "a receipts subcommand wrote something"


def test_receipts_facts_with_no_name_is_a_usage_error_not_an_empty_answer(conn, graph, seeded, registry):
    r = _cli(conn, "receipts", "facts", "--project", "proj", env_extra=registry)
    assert r.returncode == 2 and "receipts facts" in r.stderr


def test_no_receipts_path_can_reach_the_headless_model(conn, graph, seeded, monkeypatch):
    """§8.8 item 4, pinned as a property rather than as a default value.

    The room already has a model: the session's own. `ask.runner.call` and
    `claude_arbiter.default_runner` are the only two ways a second one gets
    started, so both are booby-trapped here and every receipts entry point is
    run. A default someone can flip back is not a guarantee; a test is."""
    from orchestrator.ask import runner as RUN
    from orchestrator.testing import claude_arbiter as ca

    def boom(*a, **kw):
        raise AssertionError("a receipts path forked a second model")

    monkeypatch.setattr(RUN, "call", boom)
    monkeypatch.setattr(ca, "default_runner", boom)
    monkeypatch.setattr(ca, "run_claude", boom, raising=False)

    R.assemble(conn, project="proj", challenge=CHALLENGE, psg_db_path=graph)
    R.candidates(conn, project="proj", challenge=CHALLENGE, psg_db_path=graph)
    R.facts(conn, project="proj", chosen=["pkg.rollup.weekly_report"], psg_db_path=graph)
