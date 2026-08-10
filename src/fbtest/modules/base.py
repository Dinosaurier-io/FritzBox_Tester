"""Gemeinsame Basis aller Mess- und Lastmodule."""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

from fbtest.core.events import EventBus
from fbtest.core.models import EventType, Measurement, Severity

log = logging.getLogger(__name__)


class MonitorModule(ABC):
    """Abstrakte Basisklasse fuer alle Module.

    Ein Modul kennt nur den Event-Bus. Es weiss nicht, wohin seine Messwerte
    geschrieben werden, und kommuniziert nicht direkt mit anderen Modulen. Damit
    laesst sich jedes Modul einzeln testen und der Scheduler kann es jederzeit
    neu starten.
    """

    #: Kurzname, erscheint als ``module`` in der Datenbank.
    name: str = "base"

    def __init__(self, bus: EventBus, source: str = "master") -> None:
        """Initialisiert das Modul.

        Args:
            bus: Event-Bus fuer Messwerte und Ereignisse.
            source: Herkunftskennung (``master`` oder Agent-Name).
        """
        self.bus = bus
        self.source = source
        self._log = logging.getLogger(f"fbtest.modules.{self.name}")

    @abstractmethod
    async def run(self) -> None:
        """Hauptschleife des Moduls.

        Laeuft, bis der Task abgebrochen wird. Ausnahmen duerfen nach oben
        durchschlagen - der Scheduler faengt sie ab und startet das Modul neu.
        """

    # -- Hilfsfunktionen ---------------------------------------------------

    def measure(
        self,
        metric: str,
        value: float,
        unit: str = "",
        timestamp: float | None = None,
        **meta: Any,
    ) -> None:
        """Legt einen Messwert auf den Bus."""
        measurement = Measurement(
            module=self.name,
            metric=metric,
            value=value,
            unit=unit,
            source=self.source,
            meta=dict(meta) or None,
        )
        if timestamp is not None:
            measurement.timestamp = timestamp
        self.bus.publish(measurement)

    def emit(
        self,
        event_type: EventType,
        message: str,
        severity: Severity = Severity.INFO,
        **meta: Any,
    ) -> None:
        """Legt ein Ereignis auf den Bus."""
        self.bus.emit(event_type, message, severity, source=self.source, **meta)


class IntervalModule(MonitorModule):
    """Basisklasse fuer Module, die in festem Takt arbeiten.

    Der Takt wird ueber die monotone Uhr gehalten: Braucht ein Durchgang laenger
    als geplant, wird die naechste Wartezeit entsprechend verkuerzt, statt dass
    sich die Messreihe immer weiter verschiebt.
    """

    def __init__(self, bus: EventBus, interval_s: float, source: str = "master") -> None:
        super().__init__(bus, source)
        self.interval_s = interval_s

    @abstractmethod
    async def tick(self) -> None:
        """Ein einzelner Durchgang des Moduls."""

    async def run(self) -> None:
        """Ruft :meth:`tick` in konstantem Takt auf."""
        next_run = time.monotonic()
        while True:
            await self.tick()
            next_run += self.interval_s
            delay = next_run - time.monotonic()
            if delay < 0:
                # Wir sind hinter dem Takt - Taktraster neu setzen statt aufholen.
                self._log.debug("Modul '%s' liegt %.1f s hinter dem Takt.", self.name, -delay)
                next_run = time.monotonic()
                delay = 0
            await asyncio.sleep(delay)
