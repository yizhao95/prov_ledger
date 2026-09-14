"""outcome_channels — the OutcomeChannel protocol, the two built-ins and third-party loading (phase 7 Task 1)."""
import json
import textwrap

import pytest

from orchestrator import db, extensions as ext, outcome_channels as oc, outcomes


def _expect(conn, plan_id="P1", **kw):
    base = dict(plan_id=plan_id, step_id=None, project="proj", target="ctr", target_kind="metric",
                claim="ctr stays within 5%", channel="metric:ctr")
    base.update(kw)
    cur = conn.execute("INSERT INTO expectations (plan_id, step_id, project, target, target_kind, claim, channel, created_at) "
                       "VALUES (?,?,?,?,?,?,?,'2026-09-11 01:00:00')",
                       (base["plan_id"], base["step_id"], base["project"], base["target"], base["target_kind"], base["claim"], base["channel"]))
    conn.commit()
    return dict(conn.execute("SELECT * FROM expectations WHERE id=?", (cur.lastrowid,)).fetchone())


def _metric(conn, value, when, name="ctr", plan_id="P1", unit=None):
    db.insert_metric(conn, project="proj", name=name, value=value, unit=unit, plan_id=plan_id, source="test", observed_at=when)


def test_builtin_channels_and_applicability():
    chans = oc.load_channels(ext.EMPTY)
    assert [c.channel_id for c in chans] == ["profile_drift", "metric"]
    assert all(isinstance(c, oc.OutcomeChannel) for c in chans)
    prof, met = chans
    assert prof.applicable_to({"channel": "profile_drift"}) and not prof.applicable_to({"channel": "metric:x"})
    assert met.applicable_to({"channel": "metric:x"}) and not met.applicable_to({"channel": "metric"}) and not met.applicable_to({"channel": "none"})


def test_metric_channel_observes_before_and_after(conn):
    _metric(conn, 100.0, "2026-09-11 00:30:00", unit="usd")
    _metric(conn, 90.0, "2026-09-10 00:00:00")            # older: not the nearest before
    e = _expect(conn)
    _metric(conn, 123.2, "2026-09-11 02:00:00", plan_id="P2")
    _metric(conn, 150.0, "2026-09-12 02:00:00", plan_id="P3")   # later: not the nearest after
    kind, value, source, tier, reason = oc.MetricChannel().collect(conn, e, closing_plan_id="P2", extensions=ext.EMPTY)
    assert (kind, source, tier, reason) == ("observed", "metrics", "observed", None)
    assert value["before"] == 100.0 and value["after"] == 123.2 and value["unit"] == "usd"
    assert value["delta"] == pytest.approx(23.2) and value["delta_pct"] == pytest.approx(23.2)
    assert value["before_at"] == "2026-09-11 00:30:00" and value["after_at"] == "2026-09-11 02:00:00" and value["name"] == "ctr"


def test_metric_channel_missing_side_is_none_available(conn):
    e = _expect(conn)
    _metric(conn, 100.0, "2026-09-11 00:30:00")
    kind, value, source, tier, reason = oc.MetricChannel().collect(conn, e, closing_plan_id="P2", extensions=ext.EMPTY)
    assert kind == "none_available" and tier == "none" and "after" in reason and "'ctr'" in reason
    _metric(conn, 0.0, "2026-09-11 02:00:00")
    kind, value, *_ = oc.MetricChannel().collect(conn, e, closing_plan_id="P2", extensions=ext.EMPTY)
    assert kind == "observed" and value["delta"] == -100.0
    e2 = _expect(conn, channel="metric:missing", target="missing")
    kind, _, _, _, reason = oc.MetricChannel().collect(conn, e2, closing_plan_id="P2", extensions=ext.EMPTY)
    assert kind == "none_available" and "before" in reason


def test_metric_channel_zero_baseline_has_no_pct(conn):
    _metric(conn, 0.0, "2026-09-11 00:30:00")
    e = _expect(conn)
    _metric(conn, 5.0, "2026-09-11 02:00:00")
    _, value, *_ = oc.MetricChannel().collect(conn, e, closing_plan_id="P2", extensions=ext.EMPTY)
    assert value["delta"] == 5.0 and value["delta_pct"] is None


def test_backfill_dispatches_metric_channel(conn):
    _metric(conn, 100.0, "2026-09-11 00:30:00")
    e = _expect(conn)
    _metric(conn, 123.2, "2026-09-11 02:00:00", plan_id="P2")
    n = _expect(conn, target="orders", target_kind="dataset", channel="none", claim="nothing to observe")
    u = _expect(conn, target="orders", target_kind="dataset", channel="vendor.unknown", claim="?")
    counts = outcomes.backfill(conn, "proj", None, "P2")
    assert counts == {"observed": 1, "survival": 0, "none_available": 2, "skipped_graph_channel": 0, "errors": 0}
    o = db.get_outcomes(conn, e["id"])[0]
    assert o["kind"] == "observed" and o["source"] == "metrics" and json.loads(o["value_json"])["delta_pct"] == pytest.approx(23.2)
    assert db.get_outcomes(conn, n["id"])[0]["reason"] == "nothing to observe"
    r = db.get_outcomes(conn, u["id"])[0]
    assert r["kind"] == "none_available" and "vendor.unknown" in r["reason"] and "profile_drift" in r["reason"] and "metric" in r["reason"]


@pytest.fixture
def vendor(tmp_path, monkeypatch):
    (tmp_path / "vendor_channels.py").write_text(textwrap.dedent('''
        class Always:
            channel_id = "vendor.always"
            def applicable_to(self, e): return True
            def collect(self, conn, e, *, closing_plan_id, extensions):
                return ("observed", {"vendor": True}, "vendor", "observed", None)
        class Silent:
            channel_id = "vendor.silent"
            def applicable_to(self, e): return True
            def collect(self, conn, e, *, closing_plan_id, extensions):
                return None
        class Boom:
            channel_id = "vendor.boom"
            def applicable_to(self, e): return True
            def collect(self, conn, e, *, closing_plan_id, extensions):
                raise RuntimeError("kaboom")
        class NotAChannel:
            pass
    '''))
    monkeypatch.syspath_prepend(str(tmp_path))
    return tmp_path


def _ext(tmp_path, chans):
    p = tmp_path / "provledger-extensions.json"
    p.write_text(json.dumps({"version": 1, "outcome_channels": chans}))
    return ext.load(str(p))


def test_third_party_channels_load_in_priority_order_and_degrade(vendor):
    e = _ext(vendor, [{"id": "vendor.always", "module": "vendor_channels:Always", "priority": 5},
                      {"id": "vendor.nomod", "module": "nope_mod:X"},
                      {"id": "vendor.notch", "module": "vendor_channels:NotAChannel"},
                      {"id": "vendor.off", "module": "vendor_channels:Silent", "enabled": False}])
    report = {}
    chans = oc.load_channels(e, report=report)
    assert [c.channel_id for c in chans] == ["vendor.always", "profile_drift", "metric"]
    assert "ModuleNotFoundError" in report["vendor.nomod"]["degraded"] or "No module" in report["vendor.nomod"]["degraded"]
    assert "OutcomeChannel" in report["vendor.notch"]["degraded"]
    assert report["vendor.off"]["enabled"] is False and report["vendor.off"]["degraded"] is None
    assert report["vendor.always"]["degraded"] is None
    assert e.fingerprint()["outcome_channels"] == ["vendor.always", "vendor.nomod", "vendor.notch", "vendor.off"]


def test_third_party_channel_wins_by_priority_and_silence_falls_through(conn, vendor, monkeypatch):
    monkeypatch.setattr(outcomes.psg_bridge, "repo_for", lambda project: str(vendor))
    _ext(vendor, [{"id": "vendor.silent", "module": "vendor_channels:Silent", "priority": 9},
                  {"id": "vendor.always", "module": "vendor_channels:Always", "priority": 5}])
    _metric(conn, 100.0, "2026-09-11 00:30:00")
    e = _expect(conn)
    _metric(conn, 110.0, "2026-09-11 02:00:00")
    kind, value, source, tier, reason = outcomes._observed(conn, e, "P2")
    assert (kind, source, value) == ("observed", "vendor", {"vendor": True})


def test_third_party_channel_failure_is_isolated(conn, vendor, monkeypatch):
    monkeypatch.setattr(outcomes.psg_bridge, "repo_for", lambda project: str(vendor))
    _ext(vendor, [{"id": "vendor.boom", "module": "vendor_channels:Boom", "priority": 9}])
    _metric(conn, 100.0, "2026-09-11 00:30:00")
    e = _expect(conn)
    _metric(conn, 110.0, "2026-09-11 02:00:00")
    kind, value, source, *_ = outcomes._observed(conn, e, "P2")
    assert kind == "observed" and source == "metrics" and value["delta"] == 10.0     # the metric channel still answers
    assert value.get("channel_errors") == {"vendor.boom": "RuntimeError: kaboom"}


@pytest.mark.parametrize("decl,msg", [
    ({"id": "bad id", "module": "m:C"}, "namespaced"),
    ({"id": "vendor.x"}, "module is required"),
    ({"id": "vendor.x", "module": "not-a-path"}, "import path"),
    ({"id": "vendor.x", "module": "m:C", "enabled": "yes"}, "enabled"),
    ({"id": "profile_drift", "module": "m:C"}, "built-in"),
])
def test_outcome_channel_validation(tmp_path, decl, msg):
    p = tmp_path / "provledger-extensions.json"
    p.write_text(json.dumps({"version": 1, "outcome_channels": [decl]}))
    with pytest.raises(ext.ExtensionsError, match=msg):
        ext.load(str(p))
