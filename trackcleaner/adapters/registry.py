"""Which programs SlipStream knows about.

Adding a program means writing its adapter and adding one line here. Nothing
else in the codebase should need to learn its name -- the sidebar, the Paths
dialog, the Programs menu and the tab colours are all built by asking the
registered adapters.

Adapters are instantiated per ``Paths`` (they read their file locations from
it), so the registry hands back fresh instances rather than singletons.
"""

from __future__ import annotations

from .base import LibraryAdapter, PathField
from .rekordbox import RekordboxAdapter
from .serato import SeratoAdapter
from .slipstream import SlipStreamAdapter
from .traktor import TraktorAdapter
from .vdj import VdjAdapter

# Order matters only for presentation: this is the order programs appear in the
# sidebar, the Programs menu and the Paths dialog. SlipStream sits last so the
# vendor libraries stay where users are used to finding them.
ADAPTER_CLASSES: list[type[LibraryAdapter]] = [
    VdjAdapter, TraktorAdapter, RekordboxAdapter, SeratoAdapter, SlipStreamAdapter,
]

ADAPTER_IDS = [cls.id for cls in ADAPTER_CLASSES]


def build_adapters(paths) -> dict[str, LibraryAdapter]:
    """One adapter instance per known program, keyed by id."""
    return {cls.id: cls(paths) for cls in ADAPTER_CLASSES}


def display_name(adapter_id: str) -> str:
    for cls in ADAPTER_CLASSES:
        if cls.id == adapter_id:
            return cls.display_name
    return adapter_id


def logo_file(adapter_id: str) -> str:
    """This program's artwork filename inside ``Images/``, or ``""``.

    A lookup rather than a rule, because the supplied files do not follow the
    ids. Unknown ids answer ``""`` -- the sidebar's Hidden section has no
    program, and that has to be an ordinary answer rather than a failure.
    """
    for cls in ADAPTER_CLASSES:
        if cls.id == adapter_id:
            return cls.logo_file
    return ""


def adapter_class(adapter_id: str) -> type[LibraryAdapter]:
    """The class registered for this id, or the base class for an unknown one.

    For the handful of questions that are about a *track* rather than about a
    configured program, so they can be answered without a ``Paths`` -- the Key
    column being the one that needs it: which notation to render is a per-program
    rule, and per-program rules live in ``adapters/``.
    """
    for cls in ADAPTER_CLASSES:
        if cls.id == adapter_id:
            return cls
    return LibraryAdapter


def all_path_fields(paths) -> list[PathField]:
    """Every path the registered programs need, in registration order -- what
    the Paths dialog builds its rows from."""
    fields: list[PathField] = []
    for adapter in build_adapters(paths).values():
        fields.extend(adapter.path_fields())
    return fields
