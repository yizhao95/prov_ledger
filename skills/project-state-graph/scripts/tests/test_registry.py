"""RED tests for registry — projects.json CRUD + global index regeneration."""
import json

import registry


def test_add_project_writes_entry(tmp_path):
    reg = tmp_path / "projects.json"
    registry.add_project(
        str(reg),
        name="demo",
        repo="/path/to/demo",
        db_path="/ws/demo/demo-state-graph.db",
        commit_sha="abc123",
    )
    data = json.loads(reg.read_text())
    assert "demo" in {p["name"] for p in data["projects"]}
    entry = next(p for p in data["projects"] if p["name"] == "demo")
    assert entry["repo"] == "/path/to/demo"
    assert entry["db_path"].endswith("demo-state-graph.db")
    assert entry["commit_sha"] == "abc123"
    assert entry["updated_at"]


def test_add_second_project(tmp_path):
    reg = tmp_path / "projects.json"
    registry.add_project(str(reg), name="a", repo="/a", db_path="/a.db", commit_sha="1")
    registry.add_project(str(reg), name="b", repo="/b", db_path="/b.db", commit_sha="2")
    names = {p["name"] for p in registry.list_projects(str(reg))}
    assert names == {"a", "b"}


def test_update_existing_is_idempotent_by_name(tmp_path):
    reg = tmp_path / "projects.json"
    registry.add_project(str(reg), name="a", repo="/a", db_path="/a.db", commit_sha="1")
    registry.add_project(str(reg), name="a", repo="/a2", db_path="/a2.db", commit_sha="2")
    projects = registry.list_projects(str(reg))
    assert len(projects) == 1
    assert projects[0]["repo"] == "/a2"
    assert projects[0]["commit_sha"] == "2"


def test_list_projects_empty(tmp_path):
    reg = tmp_path / "projects.json"
    assert registry.list_projects(str(reg)) == []


def test_regenerate_index_contains_each_project(tmp_path):
    reg = tmp_path / "projects.json"
    idx = tmp_path / "PROJECT-STATE-GRAPHS.md"
    registry.add_project(str(reg), name="alpha", repo="/alpha",
                         db_path="/ws/alpha/alpha-state-graph.db", commit_sha="1")
    registry.add_project(str(reg), name="beta", repo="/beta",
                         db_path="/ws/beta/beta-state-graph.db", commit_sha="2")
    registry.regenerate_index(str(reg), str(idx))
    text = idx.read_text()
    assert "alpha" in text
    assert "beta" in text
    assert "alpha-state-graph.db" in text
    assert "beta-state-graph.db" in text
