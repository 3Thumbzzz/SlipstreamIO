"""The track-table column registry (shared by the table, the header menu and
the left-column fields panel)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from ..adapters import adapter_class
from ..model.library import Track


@dataclass(frozen=True)
class Field:
    id: str
    label: str
    numeric: bool
    getter: Callable[[Track], object]     # display value
    sort_getter: Optional[Callable[[Track], object]] = None  # numeric sort value


def _key(t: Track) -> str:
    """The key in the notation the track's own program uses -- Open Key for a
    Traktor row, musical for a VirtualDJ one. Asked of the adapter rather than
    read off the track, because what was stored is not dependable: the same real
    collection holds Open Key on most entries, musical notation on a few, and
    nothing at all on entries Traktor has analysed but never tagged."""
    return adapter_class(t.source).display_key(t)


def _fmt_len(t: Track):
    if t.length is None:
        return ""
    m, s = divmod(int(t.length), 60)
    return f"{m}:{s:02d}"


# "Status" and "Order" are special columns handled by the model directly and are
# not driven by a getter here (Status shows an icon, Order shows row position).
FIELDS: list[Field] = [
    Field("playcount", "Playcount", True, lambda t: "" if t.playcount is None else t.playcount,
          lambda t: t.playcount if t.playcount is not None else -1),
    Field("title", "Title", False, lambda t: t.title),
    Field("artist", "Artist", False, lambda t: t.artist),
    Field("album", "Album", False, lambda t: t.album),
    Field("bpm", "BPM", True, lambda t: "" if t.bpm is None else f"{t.bpm:.1f}",
          lambda t: t.bpm if t.bpm is not None else -1),
    Field("length", "Length", True, _fmt_len,
          lambda t: t.length if t.length is not None else -1),
    Field("comment", "Comment", False, lambda t: t.comment),
    Field("key", "Key", False, _key),
    Field("genre", "Genre", False, lambda t: t.genre),
    Field("bitrate", "Bitrate", True, lambda t: "" if t.bitrate is None else t.bitrate,
          lambda t: t.bitrate if t.bitrate is not None else -1),
    Field("filepath", "FilePath", False, lambda t: t.path),
]

# Column order shown to the user: Status, Order, then the registry above.
STATUS_COL = "status"
ORDER_COL = "order"
ALL_COLUMN_IDS = [STATUS_COL, ORDER_COL] + [f.id for f in FIELDS]
FIELD_BY_ID = {f.id: f for f in FIELDS}
LABEL_BY_ID = {STATUS_COL: "Status", ORDER_COL: "Order", **{f.id: f.label for f in FIELDS}}
