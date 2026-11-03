"""Per-program adapters: everything format-specific lives behind one interface.

The point of this package is that a change to one program touches one module and
one test file. If code outside ``adapters/`` branches on which program it is
dealing with, that logic belongs in here instead.

See :mod:`.base` for the interface and :mod:`.registry` for how a new program is
registered.
"""

from .base import Job, LibraryAdapter, PathField
from .registry import (ADAPTER_CLASSES, ADAPTER_IDS, adapter_class,
                       all_path_fields, build_adapters, display_name,
                       logo_file)

__all__ = ["Job", "LibraryAdapter", "PathField", "ADAPTER_CLASSES",
           "ADAPTER_IDS", "adapter_class", "all_path_fields", "build_adapters",
           "display_name", "logo_file"]
