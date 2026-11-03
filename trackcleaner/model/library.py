"""Unified in-memory data model shared by both libraries and the UI.

A :class:`Library` is one software's world: a flat dict of :class:`Track` keyed
by :func:`trackcleaner.model.matching.path_key`, plus a tree of
:class:`TreeNode` (folders + playlists) mirroring how that software organises
its playlists. Both readers (``io/vdj_read.py``, ``io/traktor_read.py``) return a
``Library``; the UI renders its tree and, on selection, the tracks of the chosen
playlist(s).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .cues import Poi

# Software identifiers used throughout.
VDJ = "vdj"
TRAKTOR = "traktor"


@dataclass
class Track:
    """One track as seen by a single software."""

    key: str                       # path_key (lowercased, decoded)
    path: str                      # original Windows path (as stored)
    source: str                    # VDJ | TRAKTOR
    title: str = ""
    artist: str = ""
    album: str = ""
    bpm: Optional[float] = None
    length: Optional[float] = None  # seconds
    comment: str = ""
    comment2: str = ""             # Traktor INFO/@COMMENT2 (see status.py note)
    key_musical: str = ""          # as THIS program stores it, for display
    #: Canonical key, 0-23 (Traktor's MUSICAL_KEY numbering: 0-11 major,
    #: 12-23 minor). The neutral intermediate each program renders in its
    #: own notation, the same shape the cue timeline uses -- Traktor writes
    #: Open Key ("8d") and VirtualDJ musical ("A#m"), so passing one
    #: program's text to the other put "8d" in a field VirtualDJ reads as a
    #: musical key.
    key_value: "int | None" = None
    genre: str = ""
    bitrate: Optional[int] = None  # kbps
    playcount: Optional[int] = None
    pois: list[Poi] = field(default_factory=list)
    # False for VirtualDJ tracks that only appear in a .vdjfolder and have no
    # <Song> block in database.xml (see io/vdj_read.py). Such a track is NOT
    # really in the collection, so a sync must create it rather than assume it
    # exists.
    in_database: bool = True
    modified: Optional[datetime] = None   # cue-freshness proxy (see status.py)

    def cue_pois(self) -> list[Poi]:
        return [p for p in self.pois if p.kind == "cue"]

    def grid_pois(self) -> list[Poi]:
        return [p for p in self.pois if p.kind == "beatgrid"]


@dataclass
class Playlist:
    """A leaf playlist: an ordered list of track keys plus source-specific
    locators used when writing back."""

    name: str
    source: str
    track_keys: list[str] = field(default_factory=list)
    # VDJ: absolute path to the .vdjfolder file. Traktor: node UUID.
    vdj_file: str = ""
    traktor_uuid: str = ""
    exists: bool = True            # VDJ order-file "Exists = 0" -> False
    # Whether this playlist forbids the same track appearing twice. VirtualDJ
    # stores it per-.vdjfolder as <VirtualFolder noDuplicates="yes|no">; Traktor
    # has no such notion and always permits duplicates, so Traktor playlists set
    # this False. Default True (disallow) is the conservative VDJ assumption when
    # the attribute is absent.
    no_duplicates: bool = True


@dataclass
class TreeNode:
    """A node in a software's playlist tree."""

    name: str
    kind: str                      # "folder" | "playlist"
    source: str
    children: list["TreeNode"] = field(default_factory=list)
    playlist: Optional[Playlist] = None
    # Full path of names from the software root, e.g. ["Lists", "Background", "Chill"].
    node_path: tuple[str, ...] = ()

    def is_folder(self) -> bool:
        return self.kind == "folder"

    def iter_playlists(self):
        """Yield every node carrying a playlist at or below this node
        (recursive). Includes folder nodes that are also their own list."""
        if self.playlist is not None:
            yield self
        for c in self.children:
            yield from c.iter_playlists()

    def path_str(self) -> str:
        return " / ".join(self.node_path) if self.node_path else self.name


@dataclass
class Library:
    """One software's complete parsed state."""

    source: str
    tracks: dict[str, Track] = field(default_factory=dict)
    root: Optional[TreeNode] = None

    def playlists(self):
        if self.root is None:
            return
        yield from self.root.iter_playlists()

    def playlists_containing(self, track_key: str) -> list[TreeNode]:
        return [pl for pl in self.playlists()
                if track_key in (pl.playlist.track_keys if pl.playlist else ())]

    def playlist_index(self) -> dict[tuple, TreeNode]:
        """``path_ci(node_path) -> node`` for every playlist, first match wins.

        Built once per diff so status lookups are O(1) instead of rescanning the
        whole tree per playlist. "First wins" mirrors the sync engine's handling
        of leftover duplicate nodes.
        """
        idx: dict[tuple, TreeNode] = {}
        for pl in self.playlists():
            idx.setdefault(path_ci(pl.node_path), pl)
        return idx

    def find_playlist(self, node_path) -> Optional[TreeNode]:
        """The playlist at this path, matched case-insensitively."""
        return self.playlist_index().get(path_ci(node_path))

    def colliding_playlist_paths(self) -> dict[tuple, list[TreeNode]]:
        """Node paths held by more than one playlist once case is folded away.

        Matching is case-insensitive on purpose -- Traktor's ``breaks`` is
        VirtualDJ's ``Breaks`` and they should sync -- but VirtualDJ stores one
        *file* per playlist, so on a case-insensitive filesystem two siblings
        differing only in case cannot both exist. The second write lands on the
        first one's file and destroys it: a reported Overwrite wrote ``temp``
        (511 tracks) at 02:20:26 and ``Temp`` (42) replaced it at 02:20:44, with
        both runs reporting success.

        So a name the target cannot hold has to be *known* rather than resolved
        by whichever write happened to go last. Everything that used to pick a
        winner silently -- :meth:`playlist_index`'s first-wins, the engine's
        target lookup, the status diff -- consults this instead.

        Returns the colliding path mapped to every node holding it, in tree
        order, so a caller can name them ("temp", "Temp") rather than say only
        that something clashed. Membership tests read the same either way.
        """
        by_path: dict[tuple, list[TreeNode]] = {}
        for pl in self.playlists():
            by_path.setdefault(path_ci(pl.node_path), []).append(pl)
        return {key: nodes for key, nodes in by_path.items() if len(nodes) > 1}


def repath(node: TreeNode, parent_path) -> None:
    """Rebuild ``node_path`` for ``node`` and every descendant.

    A rename or a move changes the path of a whole subtree, and node_path is the
    identity everything else keys off (status lookups, the hidden set, the sync
    engine's target lookup), so it has to be recomputed rather than patched at
    the top.
    """
    node.node_path = tuple(parent_path) + (node.name,)
    for child in node.children:
        repath(child, node.node_path)


def subtree_paths(node: TreeNode) -> list[tuple]:
    """``node``'s path plus every descendant's, depth-first.

    Called once before a move and once after: zipping the two lists gives the
    old -> new path map that migrates the path-keyed caches. The traversal order
    is stable because a move changes names, never structure.
    """
    out = [tuple(node.node_path)]
    for child in node.children:
        out.extend(subtree_paths(child))
    return out


def can_move_into(node: TreeNode, dest_parent: Optional[TreeNode]) -> bool:
    """Whether ``node`` may be re-parented under ``dest_parent`` (``None`` = the
    tree root). False for a no-op (already there) and for anything that would
    put a node inside itself -- which on disk would mean moving a directory into
    its own subtree.
    """
    if node is dest_parent:
        return False
    dest_path = tuple(dest_parent.node_path) if dest_parent is not None else ()
    own_path = tuple(node.node_path)
    if dest_path == own_path[:-1]:
        return False                       # already this parent's child
    return path_ci(dest_path[:len(own_path)]) != path_ci(own_path)


def find_node(root: Optional[TreeNode], node_path) -> Optional[TreeNode]:
    """The node at ``node_path`` below ``root``, folders included.

    :meth:`Library.playlist_index` only indexes playlists, so it can't answer
    "which folder holds this leaf?" -- which is what placing a newly created
    playlist needs. An empty ``node_path`` means ``root`` itself; matching is
    case-insensitive, consistent with :func:`path_ci` everywhere else.
    """
    node = root
    if node is None:
        return None
    for name in node_path:
        wanted = str(name).lower()
        node = next((c for c in node.children if c.name.lower() == wanted), None)
        if node is None:
            return None
    return node


def path_ci(node_path) -> tuple:
    """Case-insensitive form of a node path.

    One definition shared by the NML writer, the sync engine's target lookup and
    the status diff — if these disagree on case, a playlist filed under
    Traktor's ``breaks`` is never matched from VirtualDJ's ``Breaks``.
    """
    return tuple(str(s).lower() for s in node_path)
