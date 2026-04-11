import sys

from . import core as _core

_core.__path__ = __path__
if __spec__ is not None and getattr(_core, "__spec__", None) is not None:
    _core.__spec__.submodule_search_locations = __spec__.submodule_search_locations

sys.modules[__name__] = _core
