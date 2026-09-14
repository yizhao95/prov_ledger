"""The essential test of the conformance suite: a plugin that deliberately gets
its identity rule wrong — the struct signature mixes the function NAME in, so a
renamed function is a different node, yet it declares rename_function as
preserved — must be caught, and the report must say which mutation lied."""
from orchestrator.testing import conformance, liar_provider


def test_liar_is_caught_at_rename_function():
    rep = conformance.run(liar_provider.LiarProvider())
    assert rep.ok is False
    chk = next(c for c in rep.checks if c["name"] == "stability_matches_declaration")
    assert chk["ok"] is False and chk["severity"] == "error"
    assert "rename_function" in chk["detail"] and "declared preserved" in chk["detail"] and "observed broken" in chk["detail"]
    # the lie is precise: everything else about the liar is fine
    others = [c for c in rep.checks if c["name"] not in ("stability_matches_declaration",)]
    assert all(c["ok"] for c in others), [c for c in others if not c["ok"]]


def test_liar_would_pass_if_it_told_the_truth():
    class Honest(liar_provider.LiarProvider):
        def declared_stability(self):
            d = dict(super().declared_stability())
            d["rename_function"] = "broken"
            return d
    rep = conformance.run(Honest())
    chk = next(c for c in rep.checks if c["name"] == "stability_matches_declaration")
    assert chk["ok"] is True, chk["detail"]
