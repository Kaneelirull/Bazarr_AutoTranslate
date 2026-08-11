from __future__ import annotations

import sys
import types

from . import foundation as _foundation
from . import library as _library
from . import repair as _repair
from .foundation import *
from .repair import *
from .library import *


class _CompatibilityProxy(types.ModuleType):
    """Forward historical monkey-patches to the owning subtitle module."""

    def __setattr__(self, name, value):
        for owner in (_foundation, _repair, _library):
            if hasattr(owner, name):
                setattr(owner, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _CompatibilityProxy
