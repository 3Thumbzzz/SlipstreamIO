"""Musical key conversion between VirtualDJ and Traktor.

VirtualDJ stores standard musical notation (``Am``, ``F#m``, ``C``, ``Bb``).
Traktor stores two things:

- ``<MUSICAL_KEY VALUE="N">`` -- a canonical integer 0..23:
  ``0..11``  = major, pitch classes C, C#, D, D#, E, F, F#, G, G#, A, A#, B
  ``12..23`` = the same pitch classes, minor (so ``Cm``=12 ... ``Bm``=23)
- ``<INFO KEY="1m">``          -- Open Key notation (``1d``..``12d`` major,
  ``1m``..``12m`` minor), which walks the circle of fifths from ``1m``=Am
  (``1d``=C, its relative major).

The mapping was verified against real Traktor data in ``collection.nml``:
Am=21, Em=16, Bm=23, C#m=13, G#m=20, A#m=22, Fm=17, Gm=19, Dm=14 -- matching
Open Key 1m, 2m, 3m, 5m, 6m, 8m, 9m, 11m, 12m respectively.
"""

from __future__ import annotations

import re
from typing import Optional

# pitch class of each natural note
_NATURAL = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

# Open Key minor order (index 0 -> "1m"): circle of fifths starting at Am.
_OPEN_KEY_MINOR_PC = [9, 4, 11, 6, 1, 8, 3, 10, 5, 0, 7, 2]
#                     Am Em Bm F#m C#m G#m D#m A#m Fm Cm Gm Dm

_KEY_RE = re.compile(r"^\s*([A-Ga-g])\s*([#b♯♭]?)\s*(m|min|minor)?\s*$")


def parse_vdj_key(key: str) -> Optional[int]:
    """VirtualDJ key string -> Traktor ``MUSICAL_KEY VALUE`` (0..23), or None.

    Accepts ``Am``, ``F#m``, ``Bb``, ``C``, ``Dmin``. Returns None for empty or
    unrecognised values (including Camelot/Open Key input) so callers can omit
    the element rather than write a wrong key.
    """
    if not key:
        return None
    m = _KEY_RE.match(key)
    if not m:
        return None
    note, accidental, minor = m.group(1).upper(), m.group(2), m.group(3)
    pc = _NATURAL[note]
    if accidental in ("#", "♯"):
        pc = (pc + 1) % 12
    elif accidental in ("b", "♭"):
        pc = (pc - 1) % 12
    return pc + 12 if minor else pc


#: Pitch class -> the spelling VirtualDJ writes. Sharps throughout, which is what
#: its own scanner produces ("A#m", never "Bbm").
_PITCH_NAMES = ["C", "C#", "D", "D#", "E", "F",
                "F#", "G", "G#", "A", "A#", "B"]


def musical_key(value: Optional[int]) -> str:
    """Traktor ``MUSICAL_KEY VALUE`` -> musical notation (``"A#m"`` / ``"C"``).

    The direction that was missing. :func:`parse_vdj_key` turned VirtualDJ's
    notation into the number and :func:`open_key` turned the number into
    Traktor's, so Traktor -> VirtualDJ had nothing to translate *with* -- and the
    writer simply passed Traktor's Open Key text straight through, putting "8d"
    into a field VirtualDJ reads as a musical key.

    Inverse of :func:`parse_vdj_key` for every one of the 24 values, which is
    what the round-trip test asserts.
    """
    if value is None or not (0 <= value <= 23):
        return ""
    minor = value >= 12
    return _PITCH_NAMES[value - 12 if minor else value] + ("m" if minor else "")


#: Pitch class -> the spelling Rekordbox and Serato write: flats throughout
#: ("Ebm", "Bb"), never sharps. Read off both programs' real libraries.
_FLAT_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]


def flat_key(value: Optional[int]) -> str:
    """Canonical key 0..23 -> musical notation with flats (``"Ebm"``), or ``""``."""
    if value is None or not (0 <= value <= 23):
        return ""
    return _FLAT_NAMES[value % 12] + ("m" if value >= 12 else "")


def open_key(value: Optional[int]) -> str:
    """Traktor ``MUSICAL_KEY VALUE`` -> Open Key notation (``"1m"``/``"1d"``).
    Empty string when the value is None/out of range."""
    if value is None or not (0 <= value <= 23):
        return ""
    is_minor = value >= 12
    pc = value - 12 if is_minor else value
    # A major key shares its Open Key number with its relative minor, whose
    # pitch class is 3 semitones below.
    minor_pc = pc if is_minor else (pc - 3) % 12
    try:
        idx = _OPEN_KEY_MINOR_PC.index(minor_pc)
    except ValueError:
        return ""
    return f"{idx + 1}{'m' if is_minor else 'd'}"


def vdj_key_to_open_key(key: str) -> str:
    """Convenience: VirtualDJ notation straight to Open Key ('' if unknown)."""
    return open_key(parse_vdj_key(key))
