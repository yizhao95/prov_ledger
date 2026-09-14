"""provledger.testing.calibration + heuristic_arbiter — the arbiter bar (phase 7 Task 2). No model here."""
import json

import pytest

from orchestrator import graph_api as g
from orchestrator.testing import calibration as cal
from orchestrator.testing import heuristic_arbiter as ha

HeuristicArbiter = lambda: ha.HeuristicArbiter()   # resolved at call time (RED: the module is a stub)


def edit_distance(a, b):
    return ha.edit_distance(a, b)


def _row(qn, key="", struct="s", df="d"):
    return {"qualified_name": qn, "node_key": key, "node_type": "function", "file_path": "pkg/m.py",
            "line_start": 1, "line_end": 3, "struct_sig": struct, "dataflow_sig": df}


def _item(i, prev, cur, truth=None):
    return {"id": f"e{i}", "ambiguity": {"layer": "struct_sig", "prev": prev, "cur": cur}, "truth": truth, "labelled_by": None}


def _calib(tmp_path, items, name="calib.json"):
    p = tmp_path / name
    p.write_text(json.dumps({"version": 1, "items": items}))
    return p


def _good_items(n=10):
    """n items the heuristic gets right: norm_x/norm_y -> scale... no: names one edit apart."""
    items = []
    for i in range(n):
        a, b = f"pkg.m.helper{i}_a", f"pkg.m.helper{i}_b"
        items.append(_item(i, [_row(a, f"nk_a{i}"), _row(b, f"nk_b{i}")],
                           [_row(a + "2"), _row(b + "2")],
                           truth={"pairs": [[a, a + "2"], [b, b + "2"]]}))
    return items


def test_arbiter_protocol_and_edit_distance():
    assert isinstance(HeuristicArbiter(), g.Arbiter) and HeuristicArbiter().arbiter_id == "provledger.heuristic"
    assert edit_distance("norm_a", "scale_a") == 5 and edit_distance("kitten", "sitting") == 3 and edit_distance("x", "x") == 0 and edit_distance("", "ab") == 2


def test_heuristic_links_unique_nearest_name_with_equal_sigs():
    amb = cal.ambiguity_from_item(_item(1, [_row("pkg.m.norm_a", "nk_a"), _row("pkg.m.norm_b", "nk_b")],
                                        [_row("pkg.m.norm_a2"), _row("pkg.m.norm_b2")]))
    out = HeuristicArbiter().arbitrate([amb])
    assert sorted((s.chosen_prev_key, s.cur_qualified_name) for s in out) == [("nk_a", "pkg.m.norm_a2"), ("nk_b", "pkg.m.norm_b2")]
    assert all(s.evidence == "name distance 1, struct/dataflow sigs equal" and s.arbiter == "provledger.heuristic" for s in out)


def test_heuristic_abstains_on_ties_sig_mismatch_and_double_claims():
    tie = cal.ambiguity_from_item(_item(1, [_row("pkg.m.norm_a", "nk_a"), _row("pkg.m.norm_b", "nk_b")],
                                        [_row("pkg.m.norm_c")]))                       # distance 1 to both
    assert HeuristicArbiter().arbitrate([tie]) == []
    sig = cal.ambiguity_from_item(_item(2, [_row("pkg.m.norm_a", "nk_a", struct="s1")], [_row("pkg.m.norm_a2", struct="s2")]))
    assert HeuristicArbiter().arbitrate([sig]) == []
    double = cal.ambiguity_from_item(_item(3, [_row("pkg.m.norm_a", "nk_a"), _row("pkg.m.zzzzzz", "nk_z")],
                                           [_row("pkg.m.norm_a1"), _row("pkg.m.norm_a2")]))   # both nearest to norm_a
    assert HeuristicArbiter().arbitrate([double]) == []


def test_run_writes_a_report_that_passes_the_gate(tmp_path):
    calib = _calib(tmp_path, _good_items(10))
    rep = cal.run(HeuristicArbiter(), calib, n_runs=3, out_dir=tmp_path / "eval")
    assert (rep.consistency, rep.coverage, rep.accuracy, rep.evidence_ok, rep.n_items, rep.n_truth) == (1.0, 1.0, 1.0, True, 10, 10)
    assert rep.sha == cal.file_sha(calib) and rep.arbiter_id == "provledger.heuristic" and rep.ok
    on_disk = json.loads((tmp_path / "eval" / "provledger.heuristic.json").read_text())
    assert on_disk["sha"] == rep.sha and on_disk["items"][0]["correct"] is True
    assert cal.load_report("provledger.heuristic", tmp_path / "eval").sha == rep.sha
    assert cal.gate("provledger.heuristic", calib, tmp_path / "eval")[0] is True


def test_accuracy_counts_truth_none_and_wrong_labels(tmp_path):
    items = _good_items(8)
    items.append(_item(90, [_row("pkg.m.norm_a", "nk_a"), _row("pkg.m.norm_b", "nk_b")], [_row("pkg.m.norm_c")], truth="none"))   # tie -> abstain == none: correct
    items.append(_item(91, [_row("pkg.m.q_a", "nk_a"), _row("pkg.m.q_b", "nk_b")], [_row("pkg.m.q_a2"), _row("pkg.m.q_b2")],
                       truth={"pairs": [["pkg.m.q_a", "pkg.m.q_b2"], ["pkg.m.q_b", "pkg.m.q_a2"]]}))          # labelled the other way: wrong
    items.append(_item(92, [_row("pkg.m.u_a", "nk_a")], [_row("pkg.m.u_a2")]))                                # unlabelled: not counted
    rep = cal.run(HeuristicArbiter(), _calib(tmp_path, items), n_runs=2, out_dir=tmp_path / "eval")
    assert rep.n_items == 11 and rep.n_truth == 10 and rep.accuracy == pytest.approx(0.9) and rep.coverage == pytest.approx(10 / 11)
    by_id = {i["id"]: i for i in rep.items}
    assert by_id["e90"]["correct"] is True and by_id["e91"]["correct"] is False and by_id["e92"]["correct"] is None


class _Wavering:
    arbiter_id = "test.wavering"

    def __init__(self):
        self.n = 0

    def arbitrate(self, ambs):
        self.n += 1
        if self.n % 2:
            return []
        return [g.Assertion(a.cur[0].qualified_name, a.prev[0].node_key, "flip", self.arbiter_id) for a in ambs]


class _NoEvidence:
    arbiter_id = "test.mute"

    def arbitrate(self, ambs):
        return [g.Assertion(a.cur[0].qualified_name, a.prev[0].node_key, "   ", self.arbiter_id) for a in ambs]


def test_gate_refusals(tmp_path):
    calib = _calib(tmp_path, _good_items(10))
    ed = tmp_path / "eval"
    assert cal.gate("provledger.heuristic", calib, ed) == (False, "refused: no evaluation report")
    rep = cal.run(_Wavering(), calib, n_runs=2, out_dir=ed)
    assert rep.consistency < 1.0 and "consistency" in cal.gate("test.wavering", calib, ed)[1]
    rep = cal.run(_NoEvidence(), calib, n_runs=2, out_dir=ed)
    assert rep.evidence_ok is False and rep.coverage == 0.0 and "evidence" in cal.gate("test.mute", calib, ed)[1]
    few = _calib(tmp_path, _good_items(9), "few.json")
    cal.run(HeuristicArbiter(), few, out_dir=ed)
    assert "labelled item" in cal.gate("provledger.heuristic", few, ed)[1]
    bad = _good_items(10)
    for it in bad[:2]:
        it["truth"] = "none"
    wrong = _calib(tmp_path, bad, "wrong.json")
    rep = cal.run(HeuristicArbiter(), wrong, out_dir=ed)
    assert rep.accuracy == pytest.approx(0.8) and "accuracy" in cal.gate("provledger.heuristic", wrong, ed)[1]
    cal.run(HeuristicArbiter(), calib, out_dir=ed)
    assert cal.gate("provledger.heuristic", calib, ed)[0] is True
    calib.write_text(calib.read_text() + "\n")                 # the calibration file changed after the evaluation
    assert "sha" in cal.gate("provledger.heuristic", calib, ed)[1]


def test_calibration_file_validation_and_arbiter_loading(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"items": [{"id": "e1", "ambiguity": {"prev": [], "cur": []}, "truth": {"nope": 1}}]}))
    with pytest.raises(ValueError, match="truth"):
        cal.load_calibration(p)
    assert isinstance(cal.load_arbiter("orchestrator.testing.heuristic_arbiter:HeuristicArbiter"), g.Arbiter)
    with pytest.raises(ValueError, match="pkg.mod:Class"):
        cal.load_arbiter("nocolon")
    with pytest.raises(TypeError, match="Arbiter"):
        cal.load_arbiter("orchestrator.testing.calibration:ArbiterReport")
