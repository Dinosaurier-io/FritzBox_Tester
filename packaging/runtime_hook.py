"""Startvorbereitung im gebuendelten Programm.

Wird von PyInstaller vor allem anderen ausgefuehrt. Behebt zwei Dinge, die im
Bundle anders sind als in einer normalen Installation.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def _user_dir() -> Path:
    """Ein beschreibbarer Ordner des angemeldeten Benutzers."""
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA")
    else:
        root = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    base = Path(root) if root else Path(tempfile.gettempdir())
    return base / "fbtest"


# matplotlib legt beim ersten Start einen Schriftarten-Zwischenspeicher an.
# Ohne diese Vorgabe versucht es das im Programmverzeichnis - das liegt unter
# "Programme" und ist schreibgeschuetzt. Die Folge waere eine Warnung bei jedem
# Start und ein vollstaendiger Neuaufbau des Zwischenspeichers vor jedem
# Bericht, was Sekunden kostet.
if "MPLCONFIGDIR" not in os.environ:
    cache = _user_dir() / "matplotlib"
    try:
        cache.mkdir(parents=True, exist_ok=True)
        os.environ["MPLCONFIGDIR"] = str(cache)
    except OSError:
        pass

# Ohne Konsolenfenster sind stdout und stderr None. Bibliotheken, die
# ungefragt darauf schreiben, wuerden mit einem AttributeError abstuerzen -
# ausgerechnet beim Programmstart, also ohne jede sichtbare Fehlermeldung.
for name in ("stdout", "stderr"):
    if getattr(sys, name, None) is None:
        setattr(sys, name, Path(os.devnull).open("w", encoding="utf-8"))  # noqa: SIM115
