"""Make stdout/stderr print Simplified Chinese on Windows terminals.

Windows consoles default to a legacy code page (cp1252/cp936) that cannot encode
much of our article-facing Chinese output, which would crash on print(). Calling
`setup_utf8()` once at the top of an entry script switches the streams to UTF-8.
"""

from __future__ import annotations

import sys


def setup_utf8() -> None:
    """Reconfigure stdout/stderr to UTF-8 if the platform allows it (no-op otherwise)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
