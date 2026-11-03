r"""Find fixable defects in a library, and describe the fix without applying it.

Scanning is separate from fixing on purpose: a scan touches nothing, so it can be
run on a whole collection without a confirmation, and the user decides per
problem *kind* what to do about it. Adding a kind is one :class:`Detector`.

The first two kinds are both "the filename carries escaped text that was meant
to be a character". Web-escaped names get baked into downloads -- 440 files in
the reported library are literally called ``Justice - Safe &amp; Sound.mp3``,
six characters of entity text where an ampersand was meant. The app now handles
those correctly as they stand (see ``matching.path_key``); this offers to rename
them to what they were supposed to say.

The third kind renames nothing. An accented character has two Unicode spellings
-- composed ``é`` (U+00E9, NFC) and ``e`` + U+0301 (NFD) -- which look identical
but are different names to NTFS. Neither Traktor nor VirtualDJ normalizes, so a
library can record one spelling while the file on disk has the other, and the
track reads as missing although it is right there. The file is already correct;
only the record of it is wrong, so the fix is a relink to the on-disk spelling.
**The disk decides, never a normal form**: both spellings can exist as two
separate files, and NFC also rewrites CJK compatibility ideographs, so blindly
normalizing can point an entry at a name that never existed.

Nothing here touches Qt, an adapter or a database. :func:`scan` reads paths and
asks the filesystem what exists; :func:`rename_files` moves files and reports
what moved. Relinking the libraries afterwards is the caller's job, because only
it knows which programs are loaded.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Optional

from . import matching

#: Characters Windows forbids in a filename. A proposal containing one cannot be
#: applied however correct it looks -- ``&quot;`` really does decode to ``"``.
ILLEGAL = set('<>:"/\\|?*')

_XML_ENTITY_RE = re.compile(r"&(?:amp|apos|quot|lt|gt|#\d+);")
_PERCENT_RE = re.compile(r"%[0-9A-Fa-f]{2}")


@dataclass(frozen=True)
class Finding:
    """One fixable thing about one track.

    ``proposed`` is the whole path as it would be after the fix, so a caller
    never has to re-derive it. ``blocked`` is empty when the fix can be applied
    and otherwise says, in the user's words, why it cannot -- a blocked finding
    is still reported, because "6 tracks I cannot fix and why" is the useful
    answer and silence is not.
    """

    kind: str
    track_key: str
    current: str
    proposed: str
    blocked: str = ""
    #: Programs a relink leaves as they are, because each already holds an
    #: entry under the on-disk name; see :func:`block_existing_entries`.
    skipped: tuple[str, ...] = ()

    @property
    def applicable(self) -> bool:
        return not self.blocked

    @property
    def current_name(self) -> str:
        return os.path.basename(self.current)

    @property
    def proposed_name(self) -> str:
        return os.path.basename(self.proposed)


@dataclass(frozen=True)
class Detector:
    """One kind of problem: how to spot it, and what to call it."""

    kind: str
    label: str            #: group heading in the results dialog
    explain: str          #: one line under the heading
    #: ``(path, exists) -> proposed path or None``.
    propose: Callable[[str, Callable[[str], bool]], Optional[str]]
    #: The file is already right on disk; only the libraries' record of it is
    #: wrong. Applying relinks and never renames.
    relinks_only: bool = False


# ============================== detectors =================================
def _decode_xml_entities(path: str, exists=None) -> Optional[str]:
    r"""``Safe &amp; Sound.mp3`` -> ``Safe & Sound.mp3``, or None if clean.

    Only the *filename* is considered. A directory holding entity text would
    rename a folder and move every track inside it -- a different operation with
    different risks, and not one to fold in silently.
    """
    directory, name = os.path.split(path)
    if not _XML_ENTITY_RE.search(name):
        return None
    fixed = matching.decode_entities(name)
    return os.path.join(directory, fixed) if fixed != name else None


def _decode_percent_escapes(path: str, exists=None) -> Optional[str]:
    r"""``Rock%20Steady.mp3`` -> ``Rock Steady.mp3``, or None if clean.

    A percent-escape that decodes to a **control character is not an escape**:
    ``BIXIGA 70 7pol 100%13`` is "100 percent 13", and decoding it would put a
    raw ``\x13`` in the filename. Requiring the result to be printable is what
    keeps that album out of the report.
    """
    directory, name = os.path.split(path)
    if not _PERCENT_RE.search(name):
        return None

    def sub(m):
        char = chr(int(m.group(0)[1:], 16))
        return char if char.isprintable() else m.group(0)

    fixed = _PERCENT_RE.sub(sub, name)
    return os.path.join(directory, fixed) if fixed != name else None


def _other_spellings(path: str) -> list[str]:
    """The composed and decomposed spellings of ``path`` that differ from it."""
    return [p for p in dict.fromkeys((unicodedata.normalize("NFC", path),
                                      unicodedata.normalize("NFD", path)))
            if p != path]


def _match_disk_spelling(path: str, exists) -> Optional[str]:
    """The on-disk spelling of a path recorded in the other Unicode form, or
    None.

    Only a path that is **not** on disk as recorded is considered: a recorded
    spelling that exists is the file, whatever form it is in -- the reported
    library holds ``Tony! Toni! Toné!`` twice, one file per spelling. The whole
    path is compared, so a folder spelled the other way is found too; nothing is
    renamed, so a folder is no riskier than a filename here.
    """
    if path.isascii() or exists(path):
        return None
    found = [p for p in _other_spellings(path) if exists(p)]
    return found[0] if found else None


DETECTORS: tuple[Detector, ...] = (
    Detector(
        kind="xml_entity_filename",
        label="Filename contains XML entity text",
        explain=("The file is named with the characters of an entity -- "
                 "\"&amp;\" where \"&\" was meant. Renaming makes the name say "
                 "what it means."),
        propose=_decode_xml_entities,
    ),
    Detector(
        kind="percent_escape_filename",
        label="Filename contains percent-escapes",
        explain=("A web-encoded name, e.g. \"%20\" where a space was meant. "
                 "Check the samples before applying: a name can legitimately "
                 "hold a percent sign."),
        propose=_decode_percent_escapes,
    ),
    Detector(
        kind="unicode_form_path",
        label="Accented name recorded in a different Unicode spelling",
        explain=("The file is on disk, but the library records its name with "
                 "the accent spelled the other way (\"é\" as one character, or "
                 "as \"e\" plus an accent mark). The names look identical; "
                 "relinking points every loaded program at the file as it is "
                 "actually named. No file is renamed."),
        propose=_match_disk_spelling,
        relinks_only=True,
    ),
)

DETECTORS_BY_KIND = {d.kind: d for d in DETECTORS}


# ================================= scan ===================================
def scan(tracks: Iterable, kinds: Iterable[str] | None = None,
         exists: Callable[[str], bool] = os.path.exists) -> list[Finding]:
    """Every fixable thing about ``tracks``, in the order the tracks came in.

    ``exists`` is injected so the whole scan is testable without a filesystem,
    and so a caller can supply a cached listing rather than stat-ing 80,000
    files twice.

    A finding is blocked -- reported, not offered -- when the file is not there
    to rename, when the name it would take is illegal on Windows, or when
    applying it would land two tracks on one file. The last one is checked
    across the whole scan rather than per track, which is the only place it is
    visible.

    Whether a relink would duplicate an entry a program already holds under the
    new name is not visible from a track list; see :func:`block_existing_entries`.
    """
    detectors = [d for d in DETECTORS
                 if kinds is None or d.kind in set(kinds)]
    findings: list[Finding] = []
    claimed: dict[str, int] = {}

    for track in tracks:
        path = getattr(track, "path", "") or ""
        if not path:
            continue
        for detector in detectors:
            proposed = detector.propose(path, exists)
            if not proposed:
                continue
            blocked = (_relink_blocked(path, proposed, exists)
                       if detector.relinks_only
                       else _blocked(path, proposed, exists))
            findings.append(Finding(
                kind=detector.kind,
                track_key=getattr(track, "key", "") or matching.path_key(path),
                current=path,
                proposed=proposed,
                blocked=blocked,
            ))
            claimed[proposed.lower()] = claimed.get(proposed.lower(), 0) + 1

    return [f if (f.blocked or claimed.get(f.proposed.lower(), 0) == 1)
            else _with_block(f, ("two tracks would be relinked to this same file"
                                 if _relinks_only(f) else
                                 "two tracks would be renamed to this same file"))
            for f in findings]


def _blocked(current: str, proposed: str, exists) -> str:
    """Why ``current`` cannot become ``proposed``, or ``""``."""
    bad = sorted(ILLEGAL & set(os.path.basename(proposed)))
    if bad:
        chars = " ".join(bad)
        return f"a Windows filename cannot contain {chars}"
    if not exists(current):
        return "the file is not on disk"
    if exists(proposed) and proposed.lower() != current.lower():
        return "a different file is already called that"
    return ""


def _relink_blocked(current: str, proposed: str, exists) -> str:
    """Why a relink to another spelling cannot be applied, or ``""``.

    The proposal already exists and the recorded path does not -- that is how
    it was found. What remains is ambiguity: when *both* other spellings are on
    disk as separate files, nothing says which one the library meant.
    """
    others = [p for p in _other_spellings(current) if exists(p)]
    if len(others) > 1:
        return "the name exists on disk in both spellings, as separate files"
    return ""


def _relinks_only(finding: Finding) -> bool:
    detector = DETECTORS_BY_KIND.get(finding.kind)
    return bool(detector and detector.relinks_only)


def block_existing_entries(findings: Iterable[Finding],
                           collections: dict[str, set]) -> list[Finding]:
    """Keep a relink from leaving a program with two entries for one file.

    ``collections`` maps a program's display name to the keys of the entries in
    its collection (playlist-only references excluded). A program holding an
    entry under *both* the recorded key and the on-disk one -- the reported
    Traktor collection has one, a missing decomposed entry beside a present
    composed one -- would end up with a duplicate. The stale entry should be
    removed instead, and that is the user's call, not a relink's.

    **That program is skipped, not the whole fix.** The reported Señorita had
    VirtualDJ holding both spellings (the composed entry with its cues, a bare
    decomposed stub its playlists point at) while Traktor held only the stale
    one and showed the track missing. Traktor is still relinked; VirtualDJ is
    left as it is and named in ``skipped``. Only when *every* program holding
    the recorded entry also holds the on-disk one is there nothing to relink,
    and the finding is blocked.

    A rename finding is returned untouched: ``scan`` already refuses a rename
    onto a name that exists on disk.
    """
    out = []
    for f in findings:
        if f.blocked or not _relinks_only(f):
            out.append(f)
            continue
        new_key = matching.path_key(f.proposed)
        owners = [name for name, keys in collections.items()
                  if f.track_key in keys]
        holders = [name for name in owners if new_key in collections[name]]
        if not holders:
            out.append(f)
        elif len(holders) == len(owners):
            out.append(_with_block(
                f, f"{', '.join(holders)} already has an entry for the on-disk "
                   "name; remove the stale one instead"))
        else:
            out.append(replace(f, skipped=tuple(holders)))
    return out


def skipped_note(finding: Finding) -> str:
    """``"VirtualDJ left as is: …"`` for a relink some programs sit out, else
    ``""``."""
    if not finding.skipped:
        return ""
    names = ", ".join(finding.skipped)
    return (f"{names} left as is: already has an entry for the on-disk name "
            "(remove the stale one there)")


def spelling_difference(finding: Finding) -> str:
    """Where two identical-looking paths differ, as code points.

    ``"e + U+0301 → U+00E9"``. A relink between spellings is otherwise
    invisible in the dialog -- both columns show the same text.
    """
    a, b = finding.current, finding.proposed
    start = 0
    while start < min(len(a), len(b)) and a[start] == b[start]:
        start += 1
    end = 0
    while (end < min(len(a), len(b)) - start
           and a[len(a) - 1 - end] == b[len(b) - 1 - end]):
        end += 1
    old, new = a[start:len(a) - end], b[start:len(b) - end]
    if len(old) > 6 or len(new) > 6:
        return "several characters respelled"

    def spell(text):
        return " + ".join(c if c.isascii() else f"U+{ord(c):04X}" for c in text)

    return f"{spell(old)} → {spell(new)}"


def _with_block(finding: Finding, reason: str) -> Finding:
    return replace(finding, blocked=reason)


def group(findings: Iterable[Finding]) -> dict[str, list[Finding]]:
    """Findings by kind, in :data:`DETECTORS` order, so the dialog can show one
    section per problem with its own count and its own choice."""
    out: dict[str, list[Finding]] = {d.kind: [] for d in DETECTORS}
    for f in findings:
        out.setdefault(f.kind, []).append(f)
    return {k: v for k, v in out.items() if v}


# ================================ apply ===================================
def rename_files(findings: Iterable[Finding],
                 rename: Callable[[str, str], None] = os.rename
                 ) -> tuple[dict[str, str], list[tuple[Finding, str]]]:
    """Rename the audio files. Returns ``({old_key: new_path}, failures)``.

    **A track only enters the returned map once its file has really moved**, so
    a rename that fails leaves that track untouched everywhere rather than
    relinked to a name that is not there. That map is exactly what the caller
    feeds to each adapter's ``repath_tracks_job``.

    Blocked findings are skipped, not attempted, and so is a relink-only finding
    -- its file is already right; see :func:`relink_map`.
    """
    moved: dict[str, str] = {}
    failures: list[tuple[Finding, str]] = []
    for finding in findings:
        if not finding.applicable or _relinks_only(finding):
            continue
        try:
            rename(finding.current, finding.proposed)
        except OSError as exc:
            failures.append((finding, f"{type(exc).__name__}: {exc}"))
            continue
        moved[finding.track_key] = finding.proposed
    return moved, failures


def relink_map(findings: Iterable[Finding]) -> dict[str, str]:
    """``{old_key: on-disk path}`` for the applicable relink-only findings --
    the same shape :func:`rename_files` returns, for the same
    ``repath_tracks_job``. Nothing on disk moves."""
    return {f.track_key: f.proposed for f in findings
            if f.applicable and _relinks_only(f)}


def skipped_keys(findings: Iterable[Finding], program: str) -> set[str]:
    """Track keys ``program`` (a display name) must not be relinked for."""
    return {f.track_key for f in findings if program in f.skipped}
