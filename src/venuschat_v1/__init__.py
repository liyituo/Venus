"""Independent VenusChat V1 frontend."""

import ctypes
from pathlib import Path
import sys


def _prepare_windows_tcl() -> None:
    """Help Tk find Tcl data when Python runs from a Windows venv.

    The venv's python.exe lives under ``.venv/Scripts``.  On some Python
    installations Tcl uses that path to derive its library directory and
    misses the Tcl scripts stored beside the base interpreter.  Initialize
    Tcl with the base executable before importing tkinter.
    """
    global _tcl_runtime
    if sys.platform != "win32":
        return
    base = Path(sys.base_prefix)
    dll_path = base / "DLLs" / "tcl86t.dll"
    executable = base / "python.exe"
    if not dll_path.is_file() or not executable.is_file():
        return
    try:
        _tcl_runtime = ctypes.WinDLL(str(dll_path))
        find_executable = _tcl_runtime.Tcl_FindExecutable
        find_executable.argtypes = [ctypes.c_char_p]
        find_executable.restype = None
        find_executable(executable.as_posix().encode("utf-8"))
    except (AttributeError, OSError):
        # Let Tk report its usual actionable error if this runtime is unusual.
        _tcl_runtime = None


_tcl_runtime = None
_prepare_windows_tcl()

from .app import VenusChatV1, main

__all__ = ["VenusChatV1", "main"]
__version__ = "0.11.0"
