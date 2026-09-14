"""Thin shell (phase 6): the identity signatures are the package's reference
implementation (provledger.providers.builtin_symbols); re-exported here so the
analyzer and its tests keep their names."""
from __future__ import annotations

import importlib as _importlib

from ._host import providers as _providers

_bs = _importlib.import_module(f"{_providers.__name__}.builtin_symbols")
struct_sig, dataflow_sig = _bs.struct_sig, _bs.dataflow_sig
_h, _params, _Alpha, _locals, _strip_annotations, _strip_docstring, _DEF_TYPES = (
    _bs._h, _bs._params, _bs._Alpha, _bs._locals, _bs._strip_annotations, _bs._strip_docstring, _bs._DEF_TYPES)
