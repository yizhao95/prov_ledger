"""update-input.schema.json and update-input.example.json stay in step with the code.

The schema is documentation only — real validation is `_apply_op.py::_require` —
so nothing failed when an op was added without it: four ops shipped with no
schema entry. These tests read the code (stdlib `ast`, no jsonschema) and hold
the documents to it: one schema entry per script, the schema's `required` equal
to what the handler `_require`s, its properties equal to the keys the handler
reads, and one example per op that the schema accepts.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL_DIR / "scripts"
APPLY_OP = SCRIPTS / "_apply_op.py"
SCHEMA = json.loads((SKILL_DIR / "update-input.schema.json").read_text())
EXAMPLES = json.loads((SKILL_DIR / "update-input.example.json").read_text())

# run-step.sh is the one wrapper that does not dispatch through _apply_op.py
# (it calls start-step / complete-step / fail-step itself).
WRAPPER_ONLY = {"run-step"}


def _schema_ops() -> dict:
    return {entry["title"]: entry for entry in SCHEMA["oneOf"]}


def _dispatch_table() -> dict:
    """OPS = {"op-name": _handler, ...} from _apply_op.py, as {op: handler name}."""
    tree = ast.parse(APPLY_OP.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "OPS" for t in node.targets):
            return {k.value: v.id for k, v in zip(node.value.keys, node.value.values)}
    raise AssertionError("OPS dispatch table not found in _apply_op.py")


def _handler_io(handler: str) -> tuple[set, set]:
    """(fields the handler _require()s, every key it reads from `data`)."""
    tree = ast.parse(APPLY_OP.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == handler)
    required, read = set(), set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_require":
            required |= {a.value for a in node.args[1:] if isinstance(a, ast.Constant)}
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
              and isinstance(node.func.value, ast.Name) and node.func.value.id == "data"
              and node.args and isinstance(node.args[0], ast.Constant)):
            read.add(node.args[0].value)
        elif (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "data"
              and isinstance(node.slice, ast.Constant)):
            read.add(node.slice.value)
    return required, read | required


def _allowed(entry: dict, key: str) -> bool:
    if key in entry.get("properties", {}):
        return True
    if any(re.search(p, key) for p in entry.get("patternProperties", {})):
        return True
    return entry.get("additionalProperties", True) is not False


def test_one_schema_entry_per_script_and_per_dispatched_op():
    wrappers = {p.stem for p in SCRIPTS.glob("*.sh")}
    dispatched = set(_dispatch_table())
    schema = set(_schema_ops())
    assert wrappers == dispatched | WRAPPER_ONLY, f"scripts vs _apply_op.OPS: {sorted(wrappers ^ (dispatched | WRAPPER_ONLY))}"
    assert schema == wrappers, f"schema entries vs scripts: missing {sorted(wrappers - schema)}, extra {sorted(schema - wrappers)}"


def test_each_wrapper_dispatches_the_op_it_is_named_after():
    for op in _dispatch_table():
        text = (SCRIPTS / f"{op}.sh").read_text()
        assert f"--op {op} " in text, f"scripts/{op}.sh does not dispatch --op {op}"


def test_schema_required_and_properties_match_what_each_handler_reads():
    ops = _schema_ops()
    for op, handler in _dispatch_table().items():
        required, read = _handler_io(handler)
        entry = ops[op]
        assert set(entry.get("required", [])) == required, (
            f"{op}: schema required {sorted(entry.get('required', []))} but {handler} requires {sorted(required)}")
        props = set(entry.get("properties", {}))
        assert props == read, (
            f"{op}: schema properties vs keys {handler} reads — undocumented {sorted(read - props)}, "
            f"never read {sorted(props - read)}")


def test_every_op_has_an_example_the_schema_accepts():
    ops = _schema_ops()
    covered = set()
    for name, ex in EXAMPLES.items():
        if name.startswith("_"):
            continue
        m = re.fullmatch(r"scripts/([a-z-]+)\.sh", ex.get("_use_with", ""))
        assert m, f"example {name!r} must say which script it is for in _use_with"
        op = m.group(1)
        assert op in ops, f"example {name!r} names {op!r}, which has no schema entry"
        entry = ops[op]
        covered.add(op)
        missing = set(entry.get("required", [])) - set(ex)
        assert not missing, f"example {name!r} lacks required {sorted(missing)}"
        for key, value in ex.items():
            assert _allowed(entry, key), f"example {name!r}: key {key!r} is not allowed by the {op} schema"
            enum = entry.get("properties", {}).get(key, {}).get("enum")
            if enum is not None:
                assert value in enum, f"example {name!r}: {key}={value!r} not in {enum}"
    assert covered == set(ops), f"ops with no worked example: {sorted(set(ops) - covered)}"


def test_the_examples_comment_keys_are_allowed_by_the_top_level_schema_shape():
    # every op entry accepts the `_`-prefixed annotation keys the examples carry
    for op, entry in _schema_ops().items():
        assert _allowed(entry, "_use_with") and _allowed(entry, "_why"), f"{op} rejects the example annotations"
