"""Einstiegspunkt des gebuendelten Programms - Fenster oder Kommandozeile.

Frueher lagen zwei Programme im Paket: eines ohne Konsole zum Doppelklicken
und eines mit Konsole fuer die Kommandozeile. Wer den Ordner zum ersten Mal
sah, musste raten, welches davon "die Anwendung" ist.

Es ist jetzt eines. Welche Betriebsart gemeint ist, sagt der Aufruf selbst:

* ohne Argumente - also beim Doppelklick - oeffnet sich das Fenster
* mit Argumenten laeuft die Kommandozeile, unveraendert im Funktionsumfang

Die Konsole verschwindet dabei nicht aus dem Programm, sondern nur aus dem
Blickfeld: Das Programm bleibt ein Konsolenprogramm (``console=True`` in
``fbtest.spec``), und der Startcode von PyInstaller versteckt das Fenster
ueber ``hide_console="hide-early"`` genau dann, wenn es dem Programm selbst
gehoert - beim Doppelklick also. Aus einer bestehenden Eingabeaufforderung
heraus bleibt es sichtbar.

Der Umweg ueber den Startcode ist noetig, weil beides sonst nicht zugleich
geht: Ein Programm ohne Konsole flackert beim Doppelklick zwar nicht, aber
die Eingabeaufforderung wartet nicht auf sein Ende - ``fbtest run`` gaebe
sofort die Eingabe frei, und jede Automatisierung liefe ins Leere.
"""

import sys


def main() -> None:
    """Waehlt anhand der Aufrufargumente zwischen Fenster und Kommandozeile."""
    if len(sys.argv) > 1:
        from fbtest.__main__ import app

        app()
    else:
        from fbtest.__main__ import gui

        gui()


if __name__ == "__main__":
    main()
