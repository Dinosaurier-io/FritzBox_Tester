"""Tests fuer den Aufruf externer Kommandos ohne sichtbares Konsolenfenster."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from fbtest.proc import hidden_process_kwargs

SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src" / "fbtest"

#: Dateien, deren Kommandoaufrufe waehrend eines Testlaufs wiederholt starten.
#: Nur hier faellt ein aufblitzendes Konsolenfenster ueberhaupt auf - die
#: uebrigen Aufrufstellen laufen einmalig oder ausschliesslich unter Linux.
REPEATING_CALLERS = (
    SOURCE_ROOT / "modules" / "ping_monitor.py",
    SOURCE_ROOT / "modules" / "wlan_monitor.py",
    SOURCE_ROOT / "modules" / "traffic_generator.py",
    SOURCE_ROOT / "network.py",
)

_CALL = re.compile(
    r"(?:create_subprocess_exec|subprocess\.run|subprocess\.Popen)\((.*?)\n\s*\)",
    re.DOTALL,
)


class TestHiddenProcessKwargs:
    """Verhalten je Betriebssystem."""

    def test_windows_suppresses_the_console_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("fbtest.proc.sys.platform", "win32")
        kwargs = hidden_process_kwargs()
        assert kwargs == {"creationflags": subprocess.CREATE_NO_WINDOW}  # type: ignore[attr-defined]

    def test_other_systems_get_no_extra_arguments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unter Linux gibt es das Flag nicht - es waere ein Aufrufsfehler."""
        monkeypatch.setattr("fbtest.proc.sys.platform", "linux")
        assert hidden_process_kwargs() == {}


class TestCallSites:
    """Quelltextpruefung der wiederkehrenden Kommandoaufrufe."""

    def test_repeating_callers_hide_the_console_window(self) -> None:
        """Jeder wiederkehrende Aufruf muss das Fenster unterdruecken.

        Ohne das Flag oeffnet Windows fuer jeden Aufruf kurz ein
        Konsolenfenster. Bei der WLAN-Abfrage alle 30 Sekunden blitzt es
        ueber einen Testlauf von 24 Stunden knapp 3000-mal auf. Die Pruefung
        greift bewusst im Quelltext an: Der Fehler laesst sich zur Laufzeit
        nur mit einem echten Bildschirm feststellen.
        """
        for path in REPEATING_CALLERS:
            source = path.read_text(encoding="utf-8")
            calls = _CALL.findall(source)
            assert calls, f"{path.name}: kein Kommandoaufruf gefunden - Test veraltet?"
            for call in calls:
                assert "hidden_process_kwargs()" in call, (
                    f"{path.name}: Kommandoaufruf ohne hidden_process_kwargs() - "
                    f"unter Windows blitzt dort ein Konsolenfenster auf.\n{call.strip()}"
                )
