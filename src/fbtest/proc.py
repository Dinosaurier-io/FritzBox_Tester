"""Gemeinsame Einstellungen fuer den Aufruf externer Kommandos.

Hintergrund: Startet ein Windows-Programm ohne eigene Konsole ein
Konsolenprogramm wie ``netsh`` oder ``ping``, oeffnet Windows dafuer ein
Konsolenfenster. Es erscheint fuer den Bruchteil einer Sekunde und schliesst
sich wieder. Bei einer Abfrage alle 30 Sekunden ergibt das ueber Stunden ein
staendiges Aufblitzen - die Anwendung wirkt dadurch kaputt, obwohl sie
einwandfrei arbeitet.

``CREATE_NO_WINDOW`` unterdrueckt dieses Fenster. Die Ausgabe des Kommandos
bleibt vollstaendig ueber die Pipes lesbar; unterdrueckt wird nur die
Darstellung.

Auf anderen Systemen gibt es das Problem nicht, dort ist das Ergebnis leer.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

# Nur unter Windows vorhanden - auf anderen Systemen bleibt der Wert ungenutzt.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def hidden_process_kwargs() -> dict[str, Any]:
    """Liefert die Zusatzargumente fuer einen Aufruf ohne Konsolenfenster.

    Bewusst als Funktion und nicht als Konstante: Tests koennen ``sys.platform``
    ersetzen und beide Zweige pruefen, ohne das Modul neu laden zu muessen.

    Returns:
        Unter Windows ``{"creationflags": CREATE_NO_WINDOW}``, sonst ein leeres
        Dictionary. Das Ergebnis laesst sich direkt als ``**kwargs`` an
        ``subprocess.run``, ``subprocess.Popen`` und
        ``asyncio.create_subprocess_exec`` uebergeben.
    """
    if sys.platform != "win32":
        return {}
    return {"creationflags": _CREATE_NO_WINDOW}
