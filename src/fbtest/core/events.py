"""Interner Event-Bus.

Alle Module sind voneinander entkoppelt: Sie kennen weder die Datenbank noch
einander, sondern legen ihre Ergebnisse auf dem Bus ab. Genau ein Konsument (der
Datenbank-Writer) leert die Warteschlange und schreibt gebuendelt.

Zusaetzlich koennen sich Module auf Ereignisse *abonnieren* (Broadcast), und der
``TrafficGate`` erlaubt es dem Speedtest, den Traffic-Generator kurz anzuhalten.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Iterable
from typing import Any

from fbtest.core.models import (
    Event,
    EventType,
    Measurement,
    Outage,
    RouterStatus,
    Severity,
    SpeedtestResult,
    WlanStatus,
)

log = logging.getLogger(__name__)

#: Alles, was ein Modul auf den Bus legen kann.
Payload = Measurement | Event | RouterStatus | WlanStatus | SpeedtestResult | Outage


class EventBus:
    """Verteilt Messwerte und Ereignisse zwischen Modulen und Speicherung.

    Die Hauptwarteschlange ist begrenzt. Laeuft sie voll (Datenbank blockiert),
    werden die aeltesten Messwerte verworfen und das im Log vermerkt - so kann
    ein langsamer Datentraeger den Testlauf nicht zum Stillstand bringen.
    """

    def __init__(self, maxsize: int = 20_000) -> None:
        """Initialisiert den Bus.

        Args:
            maxsize: Maximale Anzahl gepufferter Elemente.
        """
        self._queue: asyncio.Queue[Payload] = asyncio.Queue(maxsize=maxsize)
        self._subscribers: list[asyncio.Queue[Event]] = []
        self._dropped = 0

    # -- Produzentenseite --------------------------------------------------

    def publish(self, payload: Payload) -> None:
        """Legt ein Element auf den Bus (nicht blockierend)."""
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 100 == 1:
                log.warning(
                    "Event-Bus voll - %d Elemente verworfen. Schreibt die Datenbank zu langsam?",
                    self._dropped,
                )
            return
        if isinstance(payload, Event):
            self._broadcast(payload)

    def emit(
        self,
        event_type: EventType,
        message: str,
        severity: Severity = Severity.INFO,
        source: str = "master",
        **meta: Any,
    ) -> Event:
        """Erzeugt ein Ereignis, legt es auf den Bus und gibt es zurueck.

        Args:
            event_type: Ereignistyp.
            message: Deutschsprachige Klartextmeldung fuer Log und Bericht.
            severity: Schweregrad.
            source: Herkunft (``master`` oder Agent-Name).
            **meta: Beliebige Zusatzfelder, landen als JSON in der Datenbank.

        Returns:
            Das erzeugte Ereignis.
        """
        event = Event(
            type=event_type,
            message=message,
            severity=severity,
            source=source,
            meta=dict(meta) or None,
        )
        self.publish(event)
        log.log(_LOG_LEVELS[severity], "[%s] %s", event_type.value, message)
        return event

    def _broadcast(self, event: Event) -> None:
        """Verteilt ein Ereignis an alle Abonnenten (verlustfrei fuer Abonnenten)."""
        for queue in self._subscribers:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    # -- Konsumentenseite --------------------------------------------------

    async def get(self) -> Payload:
        """Wartet auf das naechste Element der Hauptwarteschlange."""
        return await self._queue.get()

    def get_nowait_batch(self, limit: int) -> list[Payload]:
        """Holt bis zu ``limit`` bereits vorliegende Elemente ohne zu warten."""
        batch: list[Payload] = []
        while len(batch) < limit:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return batch

    def subscribe(self, types: Iterable[EventType] | None = None) -> EventSubscription:
        """Abonniert Ereignisse (Broadcast, unabhaengig vom Datenbank-Writer).

        Args:
            types: Gewuenschte Ereignistypen; ``None`` = alle.

        Returns:
            Ein Abonnement, das als Async-Iterator genutzt werden kann.
        """
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=1000)
        self._subscribers.append(queue)
        return EventSubscription(self, queue, set(types) if types else None)

    def _unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(queue)

    @property
    def pending(self) -> int:
        """Anzahl aktuell gepufferter Elemente."""
        return self._queue.qsize()

    @property
    def dropped(self) -> int:
        """Anzahl verworfener Elemente seit Start."""
        return self._dropped


class EventSubscription:
    """Ein Ereignis-Abonnement, nutzbar als ``async for``-Schleife."""

    def __init__(
        self,
        bus: EventBus,
        queue: asyncio.Queue[Event],
        types: set[EventType] | None,
    ) -> None:
        self._bus = bus
        self._queue = queue
        self._types = types

    async def __aiter__(self) -> AsyncIterator[Event]:
        while True:
            event = await self._queue.get()
            if self._types is None or event.type in self._types:
                yield event

    def close(self) -> None:
        """Beendet das Abonnement."""
        self._bus._unsubscribe(self._queue)


class TrafficGate:
    """Schranke, mit der der Speedtest den Traffic-Generator pausieren kann.

    Der Traffic-Generator wartet vor jedem Sendevorgang kurz an dieser Schranke.
    Ist sie offen (Normalfall), kostet das praktisch nichts. Waehrend einer
    Speedtest-Messung wird sie geschlossen, damit die gemessene Bandbreite nicht
    durch den eigenen Hintergrundverkehr verfaelscht wird.
    """

    def __init__(self) -> None:
        self._open = asyncio.Event()
        self._open.set()
        self._reason = ""

    async def wait(self) -> None:
        """Wartet, bis die Schranke offen ist."""
        await self._open.wait()

    @property
    def is_open(self) -> bool:
        """True, wenn Traffic erlaubt ist."""
        return self._open.is_set()

    @property
    def reason(self) -> str:
        """Grund der aktuellen Sperre (leer, wenn offen)."""
        return self._reason

    @contextlib.asynccontextmanager
    async def paused(self, reason: str) -> AsyncIterator[None]:
        """Kontextmanager: pausiert den Traffic fuer die Dauer des Blocks."""
        self._reason = reason
        self._open.clear()
        log.info("Traffic pausiert (%s)", reason)
        try:
            yield
        finally:
            self._open.set()
            self._reason = ""
            log.info("Traffic fortgesetzt")


_LOG_LEVELS: dict[Severity, int] = {
    Severity.DEBUG: logging.DEBUG,
    Severity.INFO: logging.INFO,
    Severity.WARNING: logging.WARNING,
    Severity.ERROR: logging.ERROR,
    Severity.CRITICAL: logging.CRITICAL,
}
