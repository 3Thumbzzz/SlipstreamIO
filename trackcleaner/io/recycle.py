"""Send a file to the Windows Recycle Bin (never a permanent delete).

Uses the shell ``SHFileOperationW`` with ``FOF_ALLOWUNDO`` so the file lands in
the Recycle Bin and stays recoverable. On any non-Windows platform (or if the
shell call is unavailable) this is a no-op that returns ``False`` -- it never
falls back to an unrecoverable ``os.remove``.
"""

from __future__ import annotations

import os
import sys

# SHFileOperation op codes / flags
_FO_DELETE = 0x0003
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040          # <-- Recycle Bin instead of permanent delete
_FOF_NOERRORUI = 0x0400


def _double_null(path: str) -> str:
    """``pFrom`` must be a double-NUL-terminated list of paths."""
    return path + "\0\0"


def send_to_recycle_bin(path: str) -> bool:
    """Move ``path`` to the Recycle Bin. Returns True on success, False if the
    platform can't do it or the file is missing (caller decides how to report)."""
    if sys.platform != "win32" or not path or not os.path.exists(path):
        return False
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", wintypes.LPVOID),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    op = SHFILEOPSTRUCTW()
    op.wFunc = _FO_DELETE
    op.pFrom = _double_null(os.path.abspath(path))
    op.fFlags = _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI
    try:
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except OSError:
        return False
    return rc == 0 and not op.fAnyOperationsAborted
