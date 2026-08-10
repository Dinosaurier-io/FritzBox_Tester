"""Symbol im Infobereich.

Der eigentliche Zweck ist nicht Bequemlichkeit, sondern Sichtbarkeit: Ein
Testlauf laeuft ueber Stunden bis Tage und hat kein Fenster im Vordergrund. Das
Symbol ist der einzige Ort, an dem durchgehend erkennbar bleibt, dass ueberhaupt
gemessen wird - und ob gerade etwas nicht stimmt.

Dieses Modul haelt bewusst keine Entscheidungen. Wann welcher Zustand gilt,
entscheidet :class:`~fbtest.desktop.watcher.RunWatcher`; hier wird er nur
angezeigt.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fbtest.desktop.icons import IconState, draw_icon

log = logging.getLogger(__name__)


def tray_available() -> bool:
    """Prueft, ob ein Infobereich-Symbol moeglich ist."""
    try:
        import pystray  # noqa: F401
    except ImportError:
        return False
    return True


class TrayIcon:
    """Symbol im Infobereich mit Kontextmenue."""

    def __init__(
        self,
        on_open: Callable[[], None],
        on_stop: Callable[[], None],
        on_quit: Callable[[], None],
        is_running: Callable[[], bool],
    ) -> None:
        """Verdrahtet die Menuepunkte.

        Args:
            on_open: Fenster anzeigen.
            on_stop: Laufenden Testlauf geordnet beenden.
            on_quit: Programm beenden.
            is_running: Liefert, ob gerade ein Testlauf laeuft - der Menuepunkt
                zum Beenden wird sonst ausgegraut.
        """
        self.on_open = on_open
        self.on_stop = on_stop
        self.on_quit = on_quit
        self.is_running = is_running
        self._icon: Any = None
        self._state = IconState.IDLE

    @property
    def icon(self) -> Any:
        """Das zugrundeliegende ``pystray``-Objekt (fuer Meldungen)."""
        return self._icon

    def build(self) -> Any:
        """Erzeugt das Symbol samt Menue."""
        import pystray

        menu = pystray.Menu(
            pystray.MenuItem("Oeffnen", lambda: self.on_open(), default=True),
            pystray.MenuItem(
                "Testlauf beenden",
                lambda: self.on_stop(),
                enabled=lambda _: self.is_running(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Programm beenden", lambda: self.on_quit()),
        )
        self._icon = pystray.Icon(
            "fbtest",
            icon=draw_icon(64, self._state),
            title=self._state.tooltip,
            menu=menu,
        )
        return self._icon

    def run_detached(self) -> None:
        """Startet das Symbol in einem eigenen Thread."""
        if self._icon is None:
            self.build()
        try:
            self._icon.run_detached()
        except Exception as exc:  # pragma: no cover - plattformabhaengig
            # Unter Linux ohne AppIndicator gibt es keinen Infobereich. Das ist
            # ein Komfortverlust, kein Grund, das Programm nicht zu starten.
            log.warning("Infobereich-Symbol nicht verfuegbar: %s", exc)
            self._icon = None

    def set_state(self, state: IconState) -> None:
        """Aktualisiert Farbe und Beschriftung."""
        if self._icon is None or state is self._state:
            return
        self._state = state
        try:
            self._icon.icon = draw_icon(64, state)
            self._icon.title = state.tooltip
        except Exception as exc:  # pragma: no cover - plattformabhaengig
            log.debug("Symbol liess sich nicht aktualisieren: %s", exc)

    def stop(self) -> None:
        """Entfernt das Symbol."""
        if self._icon is None:
            return
        try:
            self._icon.stop()
        except Exception as exc:  # pragma: no cover - plattformabhaengig
            log.debug("Symbol liess sich nicht entfernen: %s", exc)
        self._icon = None
