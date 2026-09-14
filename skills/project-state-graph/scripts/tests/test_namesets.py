"""analyzer.namesets — the one declarative source for the analyzers' name sets.
DEFAULTS are the old module constants verbatim; provledger-extensions.json
`namesets` add/remove entries change them per repo (configure in cli.run,
get() inside the analyzer functions — never at import time)."""
import json

import pytest

from analyzer import api_refs, data_model, leakage, ml_overlay, namesets, profiles, py_ast, store, walker
from tests.test_ml_overlay import _nodes

LEGACY = {
    "split_funcs": {"train_test_split"},
    "fit_methods": {"fit", "train"},
    "eval_methods": {"predict", "score", "evaluate"},
    "hp_names": {"param_grid", "params", "config", "hyperparams", "hparams", "parameters", "search_space"},
    "validator_names": {"validate", "check", "check_schema", "expect", "assert_schema"},
    "ml_call_names": {"train_test_split", "fit", "train"},
    "df_constructors": {"DataFrame", "createDataFrame"},
    "http_read_methods": {"get", "head", "options"},
    "http_write_methods": {"post", "put", "patch", "delete"},
    "http_libs": {"requests", "httpx", "aiohttp", "session", "client"},
}
GONE = {ml_overlay: ("_SPLIT_FUNCS", "_FIT_METHODS", "_HP_NAMES"),
        leakage: ("_SPLIT_FUNCS", "_FIT_METHODS", "_EVAL_METHODS", "_VALIDATOR_NAMES"),
        profiles: ("_ML_CALL_NAMES",), data_model: ("_DF_CONSTRUCTORS",),
        api_refs: ("_READ_METHODS", "_WRITE_METHODS", "_HTTP_LIBS")}


@pytest.fixture(autouse=True)
def _clean():
    namesets.reset()
    yield
    namesets.reset()


def _repo(tmp_path, source, extensions=None):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "mod.py").write_text(source)
    if extensions is not None:
        (repo / "provledger-extensions.json").write_text(json.dumps({"version": 1, "namesets": extensions}))
    return repo


def _ml(tmp_path, repo):
    conn = store.init_db(str(tmp_path / "g.db"))
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    ml_overlay.analyze(conn, str(repo), fm)
    return conn


def test_namesets_defaults_match_legacy_and_constants_are_gone():
    assert {k: set(v) for k, v in namesets.DEFAULTS.items()} == LEGACY
    for mod, names in GONE.items():
        for n in names:
            assert not hasattr(mod, n), f"{mod.__name__}.{n} must come from namesets.names() now"


def test_get_returns_defaults_before_configure_and_is_frozen():
    assert namesets.names("split_funcs") == frozenset({"train_test_split"})
    assert isinstance(namesets.names("fit_methods"), frozenset)
    with pytest.raises(KeyError):
        namesets.names("no_such_set")


def test_add_makes_a_custom_split_function_a_split(tmp_path):
    src = "def go(data):\n    X_train, X_test = stratified_split(data)\n    return X_train\n"
    repo = _repo(tmp_path, src)
    assert _nodes(_ml(tmp_path, repo), "split") == []                       # defaults: not a split
    repo2 = _repo(tmp_path / "b", src, [{"set": "split_funcs", "add": ["stratified_split"]}])
    fp = namesets.configure(str(repo2))
    assert "stratified_split" in namesets.names("split_funcs") and "train_test_split" in namesets.names("split_funcs")
    roles = {json.loads(m or "{}").get("role") for _, _, m in _nodes(_ml(tmp_path / "b", repo2), "split")}
    assert roles == {"train", "test"}
    assert fp["namesets"] == {"split_funcs": ["stratified_split"]} and fp["sha256"] and fp["path"].endswith("provledger-extensions.json")


def test_remove_drops_train_from_fit_methods(tmp_path):
    repo = _repo(tmp_path, "def go(m, X):\n    m.train(X)\n", [{"set": "fit_methods", "remove": ["train"]}])
    namesets.configure(str(repo))
    assert namesets.names("fit_methods") == frozenset({"fit"})
    assert _nodes(_ml(tmp_path, repo), "model") == []                       # .train() no longer makes a model


def test_priority_orders_add_and_remove(tmp_path):
    repo = _repo(tmp_path, "x = 1\n", [{"set": "fit_methods", "remove": ["train"], "priority": 5},
                                       {"set": "fit_methods", "add": ["train", "learn"], "priority": 1}])
    namesets.configure(str(repo))
    assert namesets.names("fit_methods") == frozenset({"fit", "learn"})      # priority 5 (remove) wins over 1 (add)


def test_unknown_set_is_an_error(tmp_path):
    repo = _repo(tmp_path, "x = 1\n", [{"set": "no_such_set", "add": ["a"]}])
    with pytest.raises(namesets.ExtensionsError) as e:
        namesets.configure(str(repo))
    assert "no_such_set" in str(e.value) and "split_funcs" in str(e.value)   # the message lists the known sets
    assert namesets.names("fit_methods") == frozenset(LEGACY["fit_methods"])   # nothing applied


def test_configure_without_a_file_keeps_defaults_and_returns_none(tmp_path):
    repo = _repo(tmp_path, "x = 1\n")
    assert namesets.configure(str(repo)) is None
    assert {k: set(v) for k, v in namesets.current().items()} == LEGACY


def test_reset_restores_defaults(tmp_path):
    repo = _repo(tmp_path, "x = 1\n", [{"set": "http_libs", "add": ["urllib3"]}])
    namesets.configure(str(repo))
    assert "urllib3" in namesets.names("http_libs")
    namesets.reset()
    assert namesets.names("http_libs") == frozenset(LEGACY["http_libs"])
