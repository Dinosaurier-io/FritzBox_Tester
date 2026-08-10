"""Desktop-Schale: natives Fenster, Infobereich-Symbol und Systemmeldungen.

Die Anwendungslogik liegt vollstaendig im bestehenden Dashboard. Dieses Paket
fuegt nur den Rahmen hinzu, der aus einer Webseite ein Programm macht:

* :mod:`fbtest.desktop.single_instance` - nur eine Instanz je Rechner
* :mod:`fbtest.desktop.icons`           - selbst gezeichnete Symbole
* :mod:`fbtest.desktop.notify`          - Systemmeldungen
* :mod:`fbtest.desktop.watcher`         - beobachtet Testlaeufe fuer Tray und Meldungen
* :mod:`fbtest.desktop.window`          - Fensterrahmen mit Rueckfallstufen
* :mod:`fbtest.desktop.tray`            - Symbol im Infobereich
* :mod:`fbtest.desktop.application`     - setzt alles zusammen

Alles, was ohne Bildschirm pruefbar ist, liegt bewusst in eigenen Modulen mit
einspeisbaren Abhaengigkeiten. Nur ``window`` und ``tray`` brauchen zwingend
eine grafische Sitzung.
"""
