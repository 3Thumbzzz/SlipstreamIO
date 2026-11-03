"""SlipStream's own library layer.

``.slp`` is the neutral format every program translates through, and also
SlipStream's own database -- the two roles are the same file. It is
authoritative for what only SlipStream has (its own playlists, its own
metadata); the vendor databases stay authoritative for their own contents.

Positions stored here are **canonical**: seconds from the first decoded audio
sample. See :mod:`trackcleaner.native.slp` for the format and why it is
deliberately not a copy of Traktor's NML.
"""

from .slp import SLIPSTREAM

__all__ = ["SLIPSTREAM"]
