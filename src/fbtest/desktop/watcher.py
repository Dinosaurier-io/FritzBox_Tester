"""Beobachtet einen Testlauf fuer Symbol und Systemmeldungen.

Die gesamte Entscheidungslogik liegt hier und nicht im Tray-Modul: *wann* eine
Meldung faellig ist und *welchen* Zustand das Symbol zeigt, laesst sich damit
ohne grafische Sitzung pruefen. Das Tray-Modul bekommt nur noch fertige
Ergebnisse.

Gemeldet wird bewusst sparsam. Eine Meldung, die zu oft kommt, wird weggeklickt
und dann auch dann uebersehen, wenn sie wichtig ist:

* **Router-Neustart** - immer. Das ist der Befund, auf den der ganze Test zielt.
* **Ausfall** - erst ab einer konfigurierten Dauer. Ein Aussetzer von drei
  Sekunden ist ein Messwert, kein Ereignis, das jemanden wecken muss.
* **Ende des Laufs** - einmal, mit Ergebnis.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from fbtest.core.models import Event, EventType
from fbtest.desktop.icons import IconState
from fbtest.desktop.notify import Notifier, NullNotifier

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Notification:
    """Eine ausgeloeste Meldung."""

    title: str
    message: str
    urgent: bool = False


@dataclass(slots=True)
class RunWatcher:
    """Wertet Ereignisse eines Testlaufs fuer die Desktop-Schale aus."""

    notifier: Notifier = field(default_factory=NullNotifier)
    enabled: bool = True
    notify_outage_after_s: float = 60.0

    running: bool = False
    outage_active: bool = False
    _outage_started_at: float | None = None
    _outage_reported: bool = False

    # -- Zustand -----------------------------------------------------------

    @property
    def state(self) -> IconState:
        """Zustand fuer das Symbol im Infobereich."""
        if self.outage_active:
            return IconState.OUTAGE
        return IconState.RUNNING if self.running else IconState.IDLE

    def run_started(self, run_id: int, name: str) -> Notification | None:
        """Vermerkt den Beginn eines Testlaufs."""
        self.running = True
        self.outage_active = False
        self._outage_started_at = None
        self._outage_reported = False
        return None

    def run_finished(self, run_id: int, rows: int, interrupted: bool) -> Notification | None:
        """Vermerkt das Ende und meldet das Ergebnis."""
        self.running = False
        self.outage_active = False
        return self._emit(
            Notification(
                title="Testlauf beendet",
                message=(
                    f"Testlauf #{run_id} "
                    + ("vorzeitig beendet" if interrupted else "regulaer beendet")
                    + f", {rows:,} Messzeilen gespeichert.".replace(",", "'")
                ),
            )
        )

    # -- Ereignisse --------------------------------------------------------

    def handle(self, event: Event) -> Notification | None:
        """Wertet ein einzelnes Ereignis aus.

        Args:
            event: Ereignis vom Bus des laufenden Testlaufs.

        Returns:
            Die ausgeloeste Meldung oder ``None``.
        """
        if event.type is EventType.OUTAGE_START:
            self.outage_active = True
            self._outage_started_at = event.timestamp
            self._outage_reported = False
            return None

        if event.type is EventType.OUTAGE_END:
            notification = self._outage_end_notification(event)
            self.outage_active = False
            self._outage_started_at = None
            self._outage_reported = False
            return notification

        if event.type is EventType.ROUTER_REBOOT:
            # Immer melden: der Befund, auf den der ganze Test zielt.
            return self._emit(
                Notification(
                    title="Router hat neu gestartet",
                    message=event.message,
                    urgent=True,
                )
            )
        return None

    def tick(self, now: float) -> Notification | None:
        """Prueft, ob ein laufender Ausfall inzwischen meldenswert ist.

        Wird im Sekundentakt aufgerufen. Ein Ausfall wird waehrend seines
        Verlaufs gemeldet und nicht erst am Ende - sonst erfuehre man von einem
        stundenlangen Ausfall erst, wenn er vorbei ist.

        Args:
            now: Aktueller Zeitstempel.
        """
        if not self.outage_active or self._outage_reported:
            return None
        if self._outage_started_at is None:
            return None

        duration = now - self._outage_started_at
        if duration < self.notify_outage_after_s:
            return None

        self._outage_reported = True
        return self._emit(
            Notification(
                title="Verbindung ausgefallen",
                message=f"Seit {int(duration)} Sekunden keine Verbindung.",
                urgent=True,
            )
        )

    def _outage_end_notification(self, event: Event) -> Notification | None:
        """Meldet das Ende nur, wenn auch der Beginn gemeldet wurde."""
        if not self._outage_reported:
            return None
        return self._emit(
            Notification(title="Verbindung wieder da", message=event.message)
        )

    def _emit(self, notification: Notification) -> Notification | None:
        """Sendet eine Meldung, sofern eingeschaltet."""
        if not self.enabled:
            return None
        self.notifier.notify(notification.title, notification.message, notification.urgent)
        return notification
