"""Tests der PyInstaller-Beschreibung (``packaging/fbtest.spec``).

Der Bauvorgang selbst laeuft hier nicht - er braucht mehrere Minuten und
liesse die Testsuite von einer Windows-Installation abhaengen. Geprueft wird
stattdessen die Beschreibung: Sie entscheidet, was im Paket landet, und ein
Fehler darin faellt sonst erst auf, wenn jemand den Ordner oeffnet.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PACKAGING = Path(__file__).resolve().parents[1] / "packaging"
SPEC = (PACKAGING / "fbtest.spec").read_text(encoding="utf-8")
ENTRY = (PACKAGING / "entry.py").read_text(encoding="utf-8")


class TestSingleProgram:
    """Im Paket liegt genau ein Programm.

    Vorher lagen zwei nebeneinander - eines mit, eines ohne Konsole - und man
    musste raten, welches davon «die Anwendung» ist. Genau diese Frage hat der
    erste Benutzer gestellt, der den Ordner geoeffnet hat.
    """

    def test_only_one_executable(self) -> None:
        assert SPEC.count("EXE(") == 1, "es entsteht wieder mehr als ein Programm"

    def test_executable_is_the_double_click_name(self) -> None:
        assert 'name="FRITZBox-Langzeittest"' in SPEC

    def test_no_second_entry_point_remains(self) -> None:
        """``entry_gui.py`` und ``entry_cli.py`` sind ersetzt worden."""
        for veraltet in ("entry_gui.py", "entry_cli.py"):
            assert not (PACKAGING / veraltet).exists(), f"{veraltet} liegt noch herum"
            assert veraltet not in SPEC


class TestConsoleHandling:
    """Ein Konsolenprogramm, dessen Fenster beim Doppelklick verborgen bleibt."""

    def test_stays_a_console_program(self) -> None:
        """``console=False`` waere bequemer und bricht die Automatisierung.

        Die Eingabeaufforderung wartet bei einem Programm ohne Konsole nicht
        auf das Ende. ``run --duration 24h`` gaebe sofort die Eingabe frei,
        und ein nachfolgender ``report``-Aufruf liefe ins Leere.
        """
        assert "console=True" in SPEC
        assert "console=False" not in SPEC

    def test_console_is_hidden_early_on_double_click(self) -> None:
        """Spaeter als ``early`` heisst: Das Fenster blitzt sichtbar auf."""
        assert 'hide_console="hide-early"' in SPEC


class TestEntryPoint:
    """Die Weiche zwischen Fenster und Kommandozeile."""

    @pytest.mark.parametrize("funktion", ["gui", "app"])
    def test_both_modes_are_reachable(self, funktion: str) -> None:
        assert re.search(rf"\b{funktion}\(\)", ENTRY), f"'{funktion}()' wird nie aufgerufen"

    def test_arguments_decide(self) -> None:
        """Ohne Argumente das Fenster, mit Argumenten die Kommandozeile."""
        assert "len(sys.argv) > 1" in ENTRY
        # Reihenfolge festhalten: Der Zweig mit Argumenten muss die
        # Kommandozeile starten, nicht das Fenster.
        zweig = ENTRY.split("len(sys.argv) > 1", 1)[1]
        assert zweig.index("app()") < zweig.index("gui()")
