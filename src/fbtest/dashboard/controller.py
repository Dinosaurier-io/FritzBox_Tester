"""Steuerung eines Testlaufs aus dem Dashboard heraus.

Der Testlauf laeuft als Task **im selben Prozess** wie der Webserver. Das ist die
einfachste Loesung, die trotzdem sauber ist: Der Controller haelt direkt eine
Referenz auf den :class:`~fbtest.runner.TestRunner` und kann dessen Live-Zustand
ohne Umweg ueber die Datenbank auslesen.

Es laeuft bewusst immer nur **ein** Testlauf gleichzeitig - zwei parallele Laeufe
wuerden sich gegenseitig die Messung verfaelschen.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from fbtest.context import AppContext
from fbtest.runner import RunnerStatus, RunSummary, TestRunner

log = logging.getLogger(__name__)


class ControllerState(StrEnum):
    """Zustand des Dashboards."""

    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"


@dataclass(slots=True)
class LastResult:
    """Ergebnis des zuletzt beendeten Testlaufs."""

    run_id: int
    name: str
    duration_s: float
    rows_written: int
    interrupted: bool
    error: str = ""


class RunController:
    """Startet, ueberwacht und stoppt einen Testlauf im Auftrag des Dashboards."""

    def __init__(self, context: AppContext) -> None:
        """Initialisiert den Controller.

        Args:
            context: Laufzeitkontext. Konfiguration und Pfade werden erst beim
                Start eines Laufs abgefragt, damit zwischenzeitliche Aenderungen
                an den Einstellungen fuer den naechsten Lauf gelten.
        """
        self.context = context

        self._runner: TestRunner | None = None
        self._task: asyncio.Task[RunSummary] | None = None
        self._state = ControllerState.IDLE
        self._last: LastResult | None = None
        self._lock = asyncio.Lock()
        self._on_started: list[Callable[[TestRunner], None]] = []
        self._on_finished: list[Callable[[LastResult], None]] = []

    # -- Benachrichtigungen ------------------------------------------------

    def on_run_started(self, callback: Callable[[TestRunner], None]) -> None:
        """Meldet einen Beobachter fuer den Beginn eines Testlaufs an.

        Gedacht fuer die Desktop-Schale: Tray-Symbol und Systemmeldungen
        muessen den Ereignis-Bus des Laufs abonnieren, sobald es einen gibt.
        Ein Abfragen im Sekundentakt wuerde kurze Ereignisse zwar nicht
        verpassen, aber die Meldung zeitlich verwaschen.
        """
        self._on_started.append(callback)

    def on_run_finished(self, callback: Callable[[LastResult], None]) -> None:
        """Meldet einen Beobachter fuer das Ende eines Testlaufs an."""
        self._on_finished.append(callback)

    def _notify(self, callbacks: list[Callable[[Any], None]], payload: Any) -> None:
        """Ruft Beobachter auf, ohne dass ein Fehler den Testlauf gefaehrdet."""
        for callback in callbacks:
            try:
                callback(payload)
            except Exception:
                log.exception("Beobachter des Testlaufs hat einen Fehler ausgeloest.")

    # -- Zustand -----------------------------------------------------------

    @property
    def state(self) -> ControllerState:
        """Aktueller Zustand."""
        return self._state

    @property
    def runner(self) -> TestRunner | None:
        """Der laufende Testlauf, falls vorhanden."""
        return self._runner

    @property
    def last_result(self) -> LastResult | None:
        """Ergebnis des zuletzt beendeten Testlaufs."""
        return self._last

    def status(self) -> RunnerStatus | None:
        """Momentaufnahme des laufenden Testlaufs (``None``, wenn keiner laeuft)."""
        if self._runner is None or self._state is ControllerState.IDLE:
            return None
        return self._runner.status()

    # -- Steuerung ---------------------------------------------------------

    async def start(
        self, name: str, duration_s: float, resume: bool | None = None
    ) -> int:
        """Startet einen Testlauf.

        Args:
            name: Name des Laufs (typischerweise die Firmware-Version).
            duration_s: Geplante Laufzeit in Sekunden.
            resume: Offenen Testlauf fortsetzen; ``None`` = Wert aus der Konfiguration.

        Returns:
            Die ID des angelegten bzw. fortgesetzten Testlaufs.

        Raises:
            RuntimeError: Wenn bereits ein Testlauf laeuft.
        """
        async with self._lock:
            if self._state is not ControllerState.IDLE:
                raise RuntimeError("Es laeuft bereits ein Testlauf.")

            self._state = ControllerState.STARTING
            # Konfiguration und Pfade bewusst erst jetzt abfragen: Zwischen dem
            # Programmstart und diesem Moment kann der Nutzer die Einstellungen
            # geaendert haben.
            self.context.ensure_directories()
            self._runner = TestRunner(
                config=self.context.config,
                base_dir=self.context.base_dir,
                db_path=self.context.db_path,
                duration_s=duration_s,
                name=name,
                resume=resume,
                # Der Webserver verwaltet die Signale - der Runner darf sie nicht
                # ueberschreiben, sonst laesst sich das Dashboard nicht beenden.
                handle_signals=False,
            )
            self._task = asyncio.create_task(self._execute(), name="dashboard-run")

        # Auf die Vergabe der Testlauf-ID warten, damit die Oberflaeche sie
        # sofort anzeigen kann.
        for _ in range(200):
            if self._runner is not None and self._runner.run is not None:
                return self._runner.run.id
            if self._state is ControllerState.IDLE:
                break
            await asyncio.sleep(0.05)

        if self._last is not None and self._last.error:
            raise RuntimeError(self._last.error)
        raise RuntimeError("Der Testlauf konnte nicht gestartet werden (Zeitueberschreitung).")

    async def _execute(self) -> RunSummary:
        """Fuehrt den Testlauf aus und haelt das Ergebnis fest."""
        assert self._runner is not None
        runner = self._runner
        self._state = ControllerState.RUNNING
        self._notify(self._on_started, runner)
        try:
            summary = await runner.execute()
            self._last = LastResult(
                run_id=summary.run.id,
                name=summary.run.name,
                duration_s=summary.actual_duration_s,
                rows_written=summary.rows_written,
                interrupted=summary.interrupted,
            )
            return summary
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Testlauf im Dashboard fehlgeschlagen.")
            self._last = LastResult(
                run_id=runner.run.id if runner.run else -1,
                name=runner.name,
                duration_s=0.0,
                rows_written=0,
                interrupted=True,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        finally:
            self._state = ControllerState.IDLE
            if self._last is not None:
                self._notify(self._on_finished, self._last)

    async def stop(self) -> None:
        """Beendet den laufenden Testlauf geordnet.

        Die Module werden abgebrochen, der Schreibpuffer geleert und
        ``ended_at`` gesetzt - genau wie bei Strg+C auf der Kommandozeile.
        """
        if self._runner is None or self._task is None:
            return
        self._state = ControllerState.STOPPING
        self._runner.request_stop()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        self._state = ControllerState.IDLE

    async def shutdown(self) -> None:
        """Beendet einen eventuell laufenden Testlauf beim Herunterfahren."""
        if self._task is not None and not self._task.done():
            log.info("Dashboard faehrt herunter - laufender Testlauf wird beendet.")
            await self.stop()
