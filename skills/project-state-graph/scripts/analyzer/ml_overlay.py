"""ML overlay: recognise common training patterns and model them as graph nodes.

Detected per function (conservative static AST):
  train_test_split(...) assigned to N targets -> N `split` nodes.
      role inference by target count / name: 2 -> train,test;
      3 -> train,validation,test; names containing 'val' -> validation,
      'test' -> test, else train.
  .fit(...) / .train(...) call          -> a `model` node + `trains` edges from
                                           every split in the same function.
  dict literal named param_grid/params/config/hyperparams/hparams
                                        -> one `hyperparameter` node per key
                                           (value/options from the dict value) +
                                           `tunes` edges to the function's model.
"""
from __future__ import annotations

import ast
import os
import sqlite3
from typing import Dict, List, Optional

from . import _resolve, store
from .py_ast import _module_name

_SPLIT_FUNCS = {"train_test_split"}
_FIT_METHODS = {"fit", "train"}
_HP_NAMES = {"param_grid", "params", "config", "hyperparams", "hparams",
             "parameters", "search_space"}


def analyze(
    conn: sqlite3.Connection,
    repo_root: str,
    file_map: Dict[str, int],
) -> None:
    split_t = store.get_or_create_node_type(conn, "split")
    model_t = store.get_or_create_node_type(conn, "model")
    hp_t = store.get_or_create_node_type(conn, "hyperparameter")
    splits_into_e = store.get_or_create_edge_type(conn, "splits_into")
    trains_e = store.get_or_create_edge_type(conn, "trains")
    tunes_e = store.get_or_create_edge_type(conn, "tunes")
    produces_e = store.get_or_create_edge_type(conn, "produces")
    idx = _resolve.build_index(conn)
    repo_root = os.path.abspath(repo_root)

    for rel_path in file_map:
        if not rel_path.endswith(".py"):
            continue
        abs_path = os.path.join(repo_root, rel_path)
        try:
            with open(abs_path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
        except (SyntaxError, UnicodeDecodeError):
            continue
        module = _module_name(rel_path)
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _scan_function(
                    conn, fn, rel_path, split_t, model_t, hp_t,
                    splits_into_e, trains_e, tunes_e,
                    produces_e=produces_e, idx=idx, module=module,
                )


def _scan_function(conn, fn, rel_path, split_t, model_t, hp_t,
                   splits_into_e, trains_e, tunes_e,
                   produces_e=None, idx=None, module=None) -> None:
    split_ids: List[int] = []
    model_id: Optional[int] = None
    hp_ids: List[int] = []

    for stmt in ast.walk(fn):
        # train_test_split -> split nodes
        if isinstance(stmt, ast.Assign) and _is_split_call(stmt.value):
            roles = _roles_for_targets(stmt.targets)
            src_node = None
            for role in roles:
                sid = store.add_node(
                    conn, split_t, name=f"{fn.name}:{role}",
                    qualified_name=f"{fn.name}:{role}", file_path=rel_path,
                    line_start=stmt.lineno, metadata={"role": role},
                )
                split_ids.append(sid)
                if src_node is None:
                    src_node = sid
                else:
                    store.add_edge(conn, splits_into_e, src_node, sid,
                                   metadata={"role": role})

        # hyperparameter dicts
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                and isinstance(stmt.targets[0], ast.Name) \
                and stmt.targets[0].id in _HP_NAMES \
                and isinstance(stmt.value, ast.Dict):
            for k, v in zip(stmt.value.keys, stmt.value.values):
                key = _const(k)
                if key is None:
                    continue
                options, value = _hp_value(v)
                hid = store.add_node(
                    conn, hp_t, name=str(key), qualified_name=str(key),
                    file_path=rel_path, line_start=stmt.lineno,
                    metadata={"value": value, "options": options},
                )
                hp_ids.append(hid)

    # model from .fit/.train
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                and sub.func.attr in _FIT_METHODS:
            receiver = _attr_root(sub.func.value) or "model"
            model_id = store.add_node(
                conn, model_t, name=f"{fn.name}:{receiver}",
                qualified_name=f"{fn.name}:{receiver}", file_path=rel_path,
                line_start=getattr(sub, "lineno", fn.lineno),
                metadata={"estimator": receiver},
            )
            break

    if model_id is not None:
        for sid in split_ids:
            store.add_edge(conn, trains_e, sid, model_id)
        for hid in hp_ids:
            store.add_edge(conn, tunes_e, hid, model_id)

    # Anchor the overlay to the code graph: the enclosing function produces
    # its split/model nodes. Without this the ML nodes are an island — a
    # second train_test_split downstream would be invisible in any connected
    # view (the double-split story). Conservative: only when the function
    # resolves to a single node.
    if produces_e is not None and idx is not None and (split_ids or model_id):
        fn_node = idx.resolve_one(fn.name, module=module)
        if fn_node is not None:
            for sid in split_ids:
                store.add_edge(conn, produces_e, fn_node, sid)
            if model_id is not None:
                store.add_edge(conn, produces_e, fn_node, model_id)


def _is_split_call(node) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
    return name in _SPLIT_FUNCS


def _roles_for_targets(targets) -> List[str]:
    names: List[str] = []
    for t in targets:
        if isinstance(t, (ast.Tuple, ast.List)):
            for elt in t.elts:
                names.append(_name_of(elt))
        else:
            names.append(_name_of(t))
    names = [n for n in names if n is not None]
    n = len(names)
    roles: List[str] = []
    for i, nm in enumerate(names):
        low = (nm or "").lower()
        if "val" in low:
            roles.append("validation")
        elif "test" in low:
            roles.append("test")
        elif "train" in low:
            roles.append("train")
        else:
            roles.append(_positional_role(i, n))
    # de-dup preserving order
    seen = set()
    out = []
    for r in roles:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _positional_role(i: int, n: int) -> str:
    if n >= 3:
        return ["train", "validation", "test"][i] if i < 3 else "test"
    return ["train", "test"][i] if i < 2 else "test"


def _hp_value(node):
    """Return (options_list_or_None, single_value_or_None) for a HP dict value."""
    if isinstance(node, (ast.List, ast.Tuple)):
        opts = [_const(e) for e in node.elts]
        return [o for o in opts if o is not None], None
    return None, _const(node)


def _name_of(node) -> Optional[str]:
    return node.id if isinstance(node, ast.Name) else None


def _attr_root(value) -> Optional[str]:
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return _attr_root(value.value)
    if isinstance(value, ast.Call):
        return _attr_root(value.func)
    return None


def _const(node):
    if isinstance(node, ast.Constant):
        return node.value
    return None
