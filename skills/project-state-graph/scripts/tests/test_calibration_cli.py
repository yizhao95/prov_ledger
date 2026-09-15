"""`analyzer calibration generate|stats` (phase 8 Task 1): the calibration set
is built by construction from the corpus — never labelled by a model."""
import json

from analyzer import cli
from analyzer._host import testing as _testing


def test_calibration_generate_then_stats(tmp_path, capsys):
    out = tmp_path / "calib.json"
    assert cli.main(["calibration", "generate", "--out", str(out)]) == 0
    doc = json.loads(out.read_text())
    assert doc["version"] == _testing.calibration.VERSION and len(doc["items"]) >= 40
    assert all(it["truth"] is not None and it["labelled_by"] == "construction" for it in doc["items"])
    _testing.calibration.load_calibration(out)
    assert cli.main(["calibration", "stats", str(out)]) == 0
    text = capsys.readouterr().out
    assert "pairs=" in text and "none=" in text and "partial=" in text and "struct_sig=" in text


def test_calibration_generate_merges_a_labelled_file_and_keeps_its_truth(tmp_path):
    first = tmp_path / "first.json"
    assert cli.main(["calibration", "generate", "--out", str(first)]) == 0
    doc = json.loads(first.read_text())
    keep = dict(doc["items"][0], id="live-1", truth="none", labelled_by="a person", source="live:x.db")
    labelled = tmp_path / "labelled.json"
    labelled.write_text(json.dumps({"version": doc["version"], "items": [keep]}))
    out = tmp_path / "merged.json"
    assert cli.main(["calibration", "generate", "--out", str(out), "--merge", str(labelled)]) == 0
    merged = json.loads(out.read_text())
    assert len(merged["items"]) == len(doc["items"])
    got = next(it for it in merged["items"] if it["id"] == "live-1")
    assert got["truth"] == "none" and got["labelled_by"] == "a person"
