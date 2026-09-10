"""stdlib source mutators for the refactor corpus. Semantics-preserving; tests
pin that the AST (modulo names/docstrings) survives."""
from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path
from typing import Callable


def _replace_spans(src: str, spans: list[tuple[int, int, int, str]]) -> str:
    """Apply (lineno, col, end_col, replacement) edits, last-first so offsets hold."""
    lines = src.splitlines(keepends=True)
    for lineno, col, end_col, rep in sorted(spans, key=lambda s: (s[0], s[1]), reverse=True):
        line = lines[lineno - 1]
        lines[lineno - 1] = line[:col] + rep + line[end_col:]
    return "".join(lines)


def rename_variable(src: str, old: str, new: str) -> str:
    tree = ast.parse(src)
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == old:
            spans.append((node.lineno, node.col_offset, node.end_col_offset, new))
        elif isinstance(node, ast.arg) and node.arg == old:
            spans.append((node.lineno, node.col_offset, node.col_offset + len(old), new))
    return _replace_spans(src, spans)


def rename_function(src: str, old: str, new: str) -> str:
    tree = ast.parse(src)
    lines = src.splitlines()
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == old:
            col = lines[node.lineno - 1].index(old, node.col_offset)
            spans.append((node.lineno, col, col + len(old), new))
        elif isinstance(node, ast.Name) and node.id == old:
            spans.append((node.lineno, node.col_offset, node.end_col_offset, new))
    return _replace_spans(src, spans)


def _strip_docstrings(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            b = node.body
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) and isinstance(b[0].value.value, str):
                node.body = b[1:] or [ast.Pass()]


def strip_comments(src: str) -> str:
    toks = [t for t in tokenize.generate_tokens(io.StringIO(src).readline) if t.type != tokenize.COMMENT]
    tree = ast.parse(tokenize.untokenize(toks))
    _strip_docstrings(tree)
    return ast.unparse(tree) + "\n"


def add_comments(src: str) -> str:
    out = []
    for line in src.splitlines(keepends=True):
        out.append(line)
        if line.lstrip().startswith("def ") and line.rstrip().endswith(":"):
            indent = line[: len(line) - len(line.lstrip())] + "    "
            out.append(f"{indent}# mutated: comment inserted\n")
    return "".join(out)


def reformat(src: str) -> str:
    return ast.unparse(ast.parse(src)) + "\n"


def apply_to_repo(fn: Callable[..., str], dst_repo: Path, **kw) -> None:
    for py in sorted(dst_repo.rglob("*.py")):
        py.write_text(fn(py.read_text(), **kw))


def _gen(fn, **kw):
    def run(_src: Path, dst: Path) -> None:
        apply_to_repo(fn, dst, **kw)
    return run


def _rename_vars(_src: Path, dst: Path) -> None:
    apply_to_repo(rename_variable, dst, old="df", new="frame")
    apply_to_repo(rename_variable, dst, old="X_train", new="features")


GENERATED: dict[str, Callable[[Path, Path], None]] = {
    "rename_variable": _rename_vars,
    "rename_function": _gen(rename_function, old="main", new="run_all"),
    "strip_comments": _gen(strip_comments),
    "add_comments": _gen(add_comments),
    "reformat": _gen(reformat),
}
