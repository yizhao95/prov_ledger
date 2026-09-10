"""stdlib mutators: semantics-preserving source rewrites for the corpus."""
import ast
from tests.corpus import mutate

SRC = '''"""module doc"""
import pandas as pd  # io


def main(df):
    # filter
    df = df[df.qty > 0]
    return df
'''


def test_rename_variable_touches_all_uses():
    out = mutate.rename_variable(SRC, "df", "frame")
    assert " df" not in out and "(df" not in out and "[df" not in out
    ast.parse(out)


def test_rename_function():
    out = mutate.rename_function(SRC + "\nmain(1)\n", "main", "run_all")
    assert "def run_all(" in out and "run_all(1)" in out and "main" not in out


def test_strip_comments_removes_hash_and_docstring():
    out = mutate.strip_comments(SRC)
    assert "#" not in out and "module doc" not in out
    assert ast.dump(ast.parse(out).body[-1]) == ast.dump(ast.parse(SRC).body[-1])


def test_add_comments_keeps_ast():
    out = mutate.add_comments(SRC)
    assert out.count("#") > SRC.count("#")
    assert ast.dump(ast.parse(out)) == ast.dump(ast.parse(SRC))


def test_reformat_roundtrip_keeps_ast():
    assert ast.dump(ast.parse(mutate.reformat(SRC))) == ast.dump(ast.parse(SRC))


def test_generated_registry_has_all_five():
    assert set(mutate.GENERATED) == {"rename_variable", "rename_function", "strip_comments", "add_comments", "reformat"}
