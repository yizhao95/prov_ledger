"""Tests for the viz_slices.py CLI (peer to visualize.py)."""
from __future__ import annotations

import json
import sqlite3

import pytest

import viz_slices


def _mk_db(path):
    c = sqlite3.connect(str(path))
    c.executescript(
        """
        CREATE TABLE node_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE node (id INTEGER PRIMARY KEY, node_type_id INTEGER, name TEXT,
                           qualified_name TEXT, file_path TEXT, line_start INTEGER,
                           line_end INTEGER, metadata_json TEXT);
        CREATE TABLE edge_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE edge (id INTEGER PRIMARY KEY, edge_type_id INTEGER, src_node_id INTEGER,
                           dst_node_id INTEGER, metadata_json TEXT);
        INSERT INTO node_type (name) VALUES ('function'),('data_var'),('route');
        INSERT INTO edge_type (name) VALUES ('feeds'),('calls'),('handles');
        INSERT INTO node (node_type_id,name,qualified_name,file_path,metadata_json)
          VALUES (1,'process','m.process','m.py',NULL),
                 (2,'process:param:df','process:param:df','m.py','{"dtype":"DataFrame"}'),
                 (2,'process:param:cfg','process:param:cfg','m.py','{"dtype":"unknown"}');
        INSERT INTO edge (edge_type_id,src_node_id,dst_node_id,metadata_json)
          VALUES (1,2,1,'{"dtype":"DataFrame"}'),(1,3,1,'{"dtype":"unknown"}');
        """
    )
    c.commit()
    c.close()


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "g.db"
    _mk_db(p)
    return str(p)


def test_cli_writes_file_and_returns_zero(db, tmp_path, capsys):
    out = tmp_path / "out.html"
    rc = viz_slices.main([db, str(out)])
    assert rc == 0
    assert out.exists()


def test_cli_prints_summary_with_coverage(db, tmp_path, capsys):
    out = tmp_path / "out.html"
    viz_slices.main([db, str(out), "--title", "Demo"])
    captured = capsys.readouterr().out.lower()
    assert "dataflow" in captured
    assert "coverage" in captured
    assert "%" in captured


def test_cli_accepts_focus_flag(db, tmp_path):
    out = tmp_path / "out.html"
    rc = viz_slices.main([db, str(out), "--focus", "m.process"])
    assert rc == 0
    assert "m.process" in out.read_text()
