"""Path building, decoding and cross-database track identity.

Ported verbatim (behaviour-wise) from the original ``nml_to_xml_sync.py`` so the
proven, ear-verified matching rules are preserved:

- Traktor ``<LOCATION VOLUME DIR FILE>`` -> Windows path via ``/:`` -> ``\\``.
- VirtualDJ stores the Windows path directly.
- Identity is **case-insensitive full-path equality** with ``&apos;`` and
  ``&quot;`` decoded (Python's ``unescape`` does not do those two by default;
  omitting them silently broke every apostrophe filename).

The track "key" used throughout the app is :func:`path_key` (lowercased,
entity-decoded Windows path).
"""

from __future__ import annotations

import os
import re
from xml.sax.saxutils import unescape

_EXTRA_ENTITIES = {"&apos;": "'", "&quot;": '"'}


def build_windows_path(volume: str, dir_attr: str, file_name: str) -> str:
    """Reconstruct ``D:\\Music\\...\\Track.mp3`` from Traktor LOCATION parts.

    Pure string manipulation (no ``os.path``) so results are identical
    regardless of the OS running this code.
    """
    dir_path = (dir_attr or "").replace("/:", "\\").strip("\\")
    if volume:
        rest = "\\".join(p for p in (dir_path, file_name) if p)
        return volume + "\\" + rest if rest else volume
    return "\\".join(p for p in (dir_path, file_name) if p)


def windows_path_to_traktor_key(win_path: str) -> str:
    """Inverse of :func:`build_windows_path` for a PRIMARYKEY ``KEY`` value:
    ``D:\\Music\\x\\y.mp3`` -> ``D:/:Music/:x/:y.mp3``.

    Traktor PRIMARYKEY keys keep the volume (``D:``) then ``/:`` separators.
    """
    win_path = win_path.replace("/", "\\")
    m = re.match(r"^([A-Za-z]:)\\(.*)$", win_path)
    if m:
        volume, rest = m.group(1), m.group(2)
    else:
        volume, rest = "", win_path.lstrip("\\")
    parts = [p for p in rest.split("\\") if p]
    return volume + "/:" + "/:".join(parts) if parts else volume


def traktor_key_to_path_key(key: str) -> str:
    """A playlist PRIMARYKEY ``KEY="D:/:Music/:x/:y.mp3"`` -> canonical key.

    Inverse of :func:`windows_path_to_traktor_key`, used by both the NML reader
    and the playlist writer so the two agree on identity.

    ``key`` must already be entity-decoded -- see :func:`path_key`. The reader
    hands it ElementTree output, which is decoded; a caller reading the raw
    attribute text out of the file decodes it first.
    """
    m = re.match(r"^([A-Za-z]:)/:(.*)$", key)
    if m:
        volume, rest = m.group(1), m.group(2)
    else:
        volume, rest = "", key.lstrip("/:")
    parts = [p for p in rest.split("/:") if p]
    return path_key(build_windows_path(volume, "", "") if not parts
                    else volume + "\\" + "\\".join(parts))


def traktor_location_parts(win_path: str):
    """Split a Windows path into (VOLUME, DIR, FILE) for writing a Traktor
    ``<LOCATION>``. DIR uses ``/:`` separators with a trailing ``/:``."""
    win_path = win_path.replace("/", "\\")
    m = re.match(r"^([A-Za-z]:)\\(.*)$", win_path)
    if m:
        volume, rest = m.group(1), m.group(2)
    else:
        volume, rest = "", win_path.lstrip("\\")
    parts = [p for p in rest.split("\\") if p]
    file_name = parts[-1] if parts else ""
    dir_parts = parts[:-1]
    dir_attr = ("/:" + "/:".join(dir_parts) + "/:") if dir_parts else "/:"
    return volume, dir_attr, file_name


#: Every entity form an XML parser resolves: the five predefined names plus
#: decimal and hex character references. Matched in **one pass** on purpose --
#: see :func:`decode_entities`.
_ENTITY_RE = re.compile(r"&(?:#x([0-9A-Fa-f]+)|#(\d+)|(amp|lt|gt|apos|quot));")

_NAMED = {"amp": "&", "lt": "<", "gt": ">", "apos": "'", "quot": '"'}


def _entity_char(m: "re.Match") -> str:
    hex_ref, dec_ref, name = m.groups()
    try:
        if hex_ref is not None:
            return chr(int(hex_ref, 16))
        if dec_ref is not None:
            return chr(int(dec_ref))
    except (ValueError, OverflowError):
        return m.group(0)          # not a character anyone can name: leave it
    return _NAMED[name]


def decode_entities(text: str) -> str:
    r"""Decode XML entities exactly as the parser does, so a raw-text call site
    and a parsed one agree.

    Covers the five predefined names **and** numeric character references
    (``&#39;``, ``&#x27;``). ``saxutils.unescape`` does neither ``&apos;``/
    ``&quot;`` nor the numeric forms, and a filename holding one then keys
    differently here than it does through ElementTree.

    **One pass, never two.** Scanning again over the output would decode
    ``&amp;#39;`` -- which *means* the literal text ``&#39;`` -- all the way to
    ``'``. A single alternation resumes after each match, so the ``&amp;``
    becomes ``&`` and the ``#39;`` behind it stays text, which is what a parser
    does.
    """
    return _ENTITY_RE.sub(_entity_char, text or "")


def path_key(win_path: str) -> str:
    r"""The canonical cross-database identity key for a track path.

    **``win_path`` must already be entity-decoded.** Decoding belongs at the one
    place raw XML text is captured, and doing it again here is not harmless: a
    file whose name really contains the characters ``&amp;`` -- web-escaped names
    baked in by a download are common -- decodes a second time to ``&``, a file
    that does not exist. The readers hand ElementTree output here, which is
    already decoded; the writers read raw attribute text and call
    :func:`decode_entities` themselves.

    Getting the count wrong is silent until the two sides disagree. Every reader
    once decoded twice and ``traktor_write._entry_key`` decoded once, so the NML
    writer could not find entries it already held and appended duplicates.
    """
    return win_path.lower()


def normalize_text(value) -> str:
    r"""One canonical line ending -- ``\n`` -- for any text metadata field.

    The two programs spell a line break differently and neither is wrong.
    Traktor keeps the comment in an *attribute* and writes the break as the
    character references ``&#xD;&#xA;``, which survive XML's attribute-value
    normalisation and parse back as ``\r\n``. VirtualDJ keeps it in a
    ``<Comment>`` *element* as a real CRLF, and XML normalises line ends in
    element text, so it parses back as ``\n``.

    Without folding those together the same comment reads as two different
    strings, :func:`status.metadata_matches` can never say yes, the track is
    yellow forever and Overwrite rewrites an identical value on every run --
    the same non-convergence ``status._lists_match`` guards against for cues.
    """
    return (value or "").replace("\r\n", "\n").replace("\r", "\n")


def resolve_local_path(win_path: str, audio_root_override: str | None = None) -> str:
    """Map a database Windows path to something openable on this machine.

    ``audio_root_override`` replaces the drive-letter root when the databases'
    ``D:\\...`` paths differ from this machine's layout (unused by default).
    """
    if audio_root_override:
        rest = re.sub(r"^[A-Za-z]:\\", "", win_path).replace("\\", os.sep)
        return os.path.join(audio_root_override, rest)
    return win_path
