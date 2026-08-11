"""Compatibility proxy for historical runtime imports.

Production composition uses :mod:`autotranslate.runtime_host` directly.
"""
from __future__ import annotations

import sys
import types

from .runtime_host import load_runtime

_state = load_runtime()


class _RuntimeProxy(types.ModuleType):
    def __getattr__(self, name):
        return getattr(_state, name)

    def __setattr__(self, name, value):
        if name.startswith("__") or name in {"_state"}:
            return super().__setattr__(name, value)
        setattr(_state, name, value)

    def __delattr__(self, name):
        if hasattr(_state, name):
            delattr(_state, name)
            return
        super().__delattr__(name)


sys.modules[__name__].__class__ = _RuntimeProxy
