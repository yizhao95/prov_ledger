"""Thin shell: the stdlib mutators live in the package (provledger.testing.mutate,
phase 6); re-exported here for the analyzer tests and the scenario runner."""
import importlib as _importlib

from analyzer._host import testing as _testing

_mutate = _importlib.import_module(f"{_testing.__name__}.mutate")
globals().update({k: v for k, v in vars(_mutate).items() if not k.startswith("__")})
