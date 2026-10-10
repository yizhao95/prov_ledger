"""plan-input.schema.json and plan-input.example.json stay in step with publish_plan.py.

The schema is documentation only — real validation is `publish_plan.py::_validate`
— so it drifted unnoticed: `expectations` and `headline_notes` were accepted by
the code but missing from the schema, and its `additionalProperties: false`
rejected the worked example's own `_comment`. Stdlib only (no jsonschema): the
keys the code reads are taken from its AST, the enums from its constants.
"""
from __future__ import annotations

import ast
import importlib
import json
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL_DIR / "scripts"
PUBLISH = SCRIPTS / "publish_plan.py"
SCHEMA = json.loads((SKILL_DIR / "plan-input.schema.json").read_text())
EXAMPLE = json.loads((SKILL_DIR / "plan-input.example.json").read_text())

# the functions that read the plan-input dict (`data`); other functions in the
# module use the same name for the project registry
PLAN_INPUT_READERS = {"_validate", "_resolve_project", "main"}


def _publish_module():
    sys.path.insert(0, str(SCRIPTS))
    try:
        return importlib.import_module("publish_plan")
    finally:
        sys.path.remove(str(SCRIPTS))


def _keys_read_from_plan_input() -> set:
    tree = ast.parse(PUBLISH.read_text())
    keys = set()
    for fn in tree.body:
        if not (isinstance(fn, ast.FunctionDef) and fn.name in PLAN_INPUT_READERS):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "data"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                keys.add(node.args[0].value)
            elif (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "data"
                  and isinstance(node.slice, ast.Constant)):
                keys.add(node.slice.value)
    return keys


def _allowed(obj: dict, key: str) -> bool:
    if key in obj.get("properties", {}):
        return True
    if any(re.search(p, key) for p in obj.get("patternProperties", {})):
        return True
    return obj.get("additionalProperties", True) is not False


def test_schema_properties_are_exactly_the_keys_publish_reads():
    props = set(SCHEMA["properties"])
    read = _keys_read_from_plan_input()
    assert props == read, f"undocumented keys {sorted(read - props)}; documented but never read {sorted(props - read)}"


def test_schema_required_and_enums_match_the_validator():
    mod = _publish_module()
    assert set(SCHEMA["required"]) == set(mod.REQUIRED_FIELDS)
    skill = SCHEMA["properties"]["skills"]["items"]["properties"]
    assert set(skill["source"]["enum"]) == mod.VALID_SKILL_SOURCES
    exp = SCHEMA["properties"]["expectations"]["items"]
    assert set(exp["properties"]["target_kind"]["enum"]) == mod.VALID_TARGET_KINDS
    channel = exp["properties"]["channel"]
    assert set(channel["anyOf"][0]["enum"]) == mod.VALID_CHANNELS
    assert set(SCHEMA["properties"]["root"]["properties"]["kind"]["enum"]) == set(mod.ROOT_KINDS)
    assert re.fullmatch(channel["anyOf"][1]["pattern"], "metric:auc")
    assert not re.fullmatch(channel["anyOf"][1]["pattern"], "metric:")
    assert {"target", "target_kind", "claim", "channel"} <= set(exp["required"])


def test_the_example_is_accepted_by_the_schema_and_by_the_validator():
    for key in EXAMPLE:
        assert _allowed(SCHEMA, key), f"plan-input.example.json key {key!r} is rejected by the schema"
    assert set(SCHEMA["required"]) <= set(EXAMPLE)
    skill_item = SCHEMA["properties"]["skills"]["items"]
    for sk in EXAMPLE.get("skills", []):
        assert all(_allowed(skill_item, k) for k in sk) and sk["source"] in skill_item["properties"]["source"]["enum"]
    step_item = next(o for o in SCHEMA["properties"]["steps"]["items"]["oneOf"] if o.get("type") == "object")
    for st in EXAMPLE["steps"]:
        if isinstance(st, dict):
            assert all(_allowed(step_item, k) for k in st), f"step keys {sorted(st)} not all allowed"
            if "type" in st:
                assert st["type"] in step_item["properties"]["type"]["enum"]
    _publish_module()._validate(EXAMPLE)          # exits non-zero (SystemExit) if the real validator refuses it
