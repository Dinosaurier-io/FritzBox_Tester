"""Unterdrueckung des Energiesparmodus waehrend eines Testlaufs.

Ein Langzeittest ueber 24 oder 72 Stunden ist wertlos, wenn der Rechner nach
30 Minuten in den Standby geht. Das System erkennt die Luecke zwar und wertet
sie ausdruecklich nicht als Ausfall (``SYSTEM_GAP``) - aber gemessen wurde in
dieser Zeit eben nichts.

Bisher stand im README, der Benutzer moege den Energiesparmodus von Hand
abschalten. Das ist eine Anforderung, die man vergisst, und ihr Vergessen faellt
erst am naechsten Morgen auf. Deshalb kuemmert sich das Programm nun selbst
darum.

Bewusst hier und nicht in der Bedienoberflaeche: So profitiert auch
``fbtest run`` auf der Kommandozeile davon. Die Sperre gilt genau so lange wie
der Testlauf und wird beim Beenden wieder aufgehoben - auch bei Absturz, weil
Windows sie an den Prozess bindet.
"""

from __future__ import annotations

import logging
import platform
import subprocess
import sys
from typing import Protocol

log = logging.getLogger(__name__)

# Konstanten der Windows-API (winbase.h).
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_AWAYMODE_REQUIRED = 0x00000040


class PowerBackend(Protocol):
    """Plattformabhaengige Umsetzung der Standby-Sperre."""

    @property
    def description(self) -> str:
        """Kurze Beschreibung fuer die Anzeige."""
        ...

    def acquire(self) -> bool:
        """Aktiviert die Sperre. ``False``, wenn das nicht moeglich war."""
        ...

    def release(self) -> None:
        """Hebt die Sperre wieder auf."""
        ...


class WindowsPowerBackend:
    """Standby-Sperre ueber ``SetThreadExecutionState``."""

    description = "Windows-Energieverwaltung"

    def acquire(self) -> bool:
        """Meldet dem System durchgehenden Betriebsbedarf an."""
        import ctypes

        # ES_CONTINUOUS haelt den Zustand, bis er ausdruecklich aufgehoben wird.
        # ES_SYSTEM_REQUIRED verhindert den Standby; der Bildschirm darf bewusst
        # ausgehen - er wird fuer die Messung nicht gebraucht.
        flags = _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_AWAYMODE_REQUIRED
        result = int(ctypes.windll.kernel32.SetThreadExecutionState(flags))
        if result == 0:
            # Away-Mode gibt es nicht auf jedem Geraet - ohne ihn erneut versuchen.
            result = int(
                ctypes.windll.kernel32.SetThreadExecutionState(
                    _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED
                )
            )
        return result != 0

    def release(self) -> None:
        """Gibt den Zustand wieder frei."""
        import ctypes

        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)


class SystemdPowerBackend:
    """Standby-Sperre ueber ``systemd-inhibit``."""

    description = "systemd-inhibit"

    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None

    def acquire(self) -> bool:
        """Startet einen Hilfsprozess, der die Sperre haelt.

        ``systemd-inhibit`` haelt die Sperre, solange das gestartete Kommando
        laeuft. Deshalb wird ``sleep infinity`` gestartet und beim Freigeben
        wieder beendet.
        """
        try:
            self._process = subprocess.Popen(
                [
                    "systemd-inhibit",
                    "--what=sleep:idle",
                    "--who=fbtest",
                    "--why=Langzeit-Testlauf laeuft",
                    "--mode=block",
                    "sleep",
                    "infinity",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, ValueError) as exc:
            log.debug("systemd-inhibit nicht verfuegbar: %s", exc)
            self._process = None
            return False
        return True

    def release(self) -> None:
        """Beendet den Hilfsprozess und hebt die Sperre damit auf."""
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - sollte nie eintreten
            self._process.kill()
        self._process = None


class UnsupportedPowerBackend:
    """Platzhalter fuer Systeme ohne bekannte Schnittstelle."""

    description = "nicht unterstuetzt"

    def acquire(self) -> bool:
        """Tut nichts und meldet ehrlich einen Fehlschlag."""
        return False

    def release(self) -> None:
        """Nichts zu tun."""


def select_backend() -> PowerBackend:
    """Waehlt die zur Plattform passende Umsetzung."""
    if sys.platform == "win32":
        return WindowsPowerBackend()
    if platform.system() == "Linux":
        return SystemdPowerBackend()
    return UnsupportedPowerBackend()


class PowerKeeper:
    """Haelt den Rechner waehrend eines Testlaufs wach.

    Verwendung als Kontextmanager::

        with PowerKeeper(enabled=True) as keeper:
            print(keeper.status_text)

    Ein Fehlschlag ist ausdruecklich kein Abbruchgrund: Lieber ein Testlauf mit
    dem Risiko einer Standby-Luecke als gar keiner. Der Zustand wird aber
    gemeldet, damit die Oberflaeche ihn anzeigen kann.
    """

    def __init__(self, enabled: bool = True, backend: PowerBackend | None = None) -> None:
        """Initialisiert die Sperre.

        Args:
            enabled: Ob die Sperre ueberhaupt gewuenscht ist.
            backend: Umsetzung; ``None`` = passend zur Plattform.
        """
        self.enabled = enabled
        self.backend = backend if backend is not None else select_backend()
        self.active = False
        self.error = ""

    def __enter__(self) -> PowerKeeper:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def acquire(self) -> bool:
        """Aktiviert die Sperre, sofern gewuenscht und moeglich."""
        if not self.enabled or self.active:
            return self.active
        try:
            self.active = self.backend.acquire()
        except Exception as exc:  # pragma: no cover - plattformabhaengig
            self.active = False
            self.error = f"{type(exc).__name__}: {exc}"
            log.warning("Standby-Sperre fehlgeschlagen: %s", self.error)
            return False

        if self.active:
            log.info("Energiesparmodus unterdrueckt (%s).", self.backend.description)
        else:
            self.error = f"Ueber {self.backend.description} nicht moeglich."
            log.warning(
                "Energiesparmodus konnte nicht unterdrueckt werden (%s). "
                "Der Rechner koennte waehrend des Testlaufs in den Standby gehen.",
                self.backend.description,
            )
        return self.active

    def release(self) -> None:
        """Hebt die Sperre auf."""
        if not self.active:
            return
        try:
            self.backend.release()
        except Exception as exc:  # pragma: no cover - plattformabhaengig
            log.warning("Standby-Sperre liess sich nicht aufheben: %s", exc)
        self.active = False
        log.info("Energiesparmodus wieder freigegeben.")

    @property
    def status_text(self) -> str:
        """Einzeiler fuer die Statusanzeige."""
        if not self.enabled:
            return "Energiesparmodus nicht unterdrueckt (abgeschaltet)"
        if self.active:
            return f"Energiesparmodus unterdrueckt ({self.backend.description})"
        return f"Energiesparmodus NICHT unterdrueckt - {self.error or 'unbekannter Grund'}"
