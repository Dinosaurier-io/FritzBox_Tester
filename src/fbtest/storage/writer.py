"""Datenbank-Writer: einziger Konsument des Event-Busses.

Sammelt alles, was die Module auf den Bus legen, und schreibt es gebuendelt in
die SQLite-Datenbank. Dadurch entsteht statt tausender Einzel-Commits ein
Schreibvorgang alle paar Sekunden - entscheidend fuer Langzeitlaeufe.

Der eigentliche SQLite-Zugriff laeuft in einem Worker-Thread
(``asyncio.to_thread``), damit die Ereignisschleife nicht blockiert.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from fbtest.core.events import EventBus, Payload
from fbtest.core.models import (
    Event,
    Measurement,
    Outage,
    RouterStatus,
    SpeedtestResult,
    WlanStatus,
)
from fbtest.storage.database import Database

log = logging.getLogger(__name__)


@dataclass(slots=True)
class _Batch:
    """Zwischenspeicher fuer einen Schreibvorgang."""

    measurements: list[Measurement] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    router: list[RouterStatus] = field(default_factory=list)
    wlan: list[WlanStatus] = field(default_factory=list)
    speedtests: list[SpeedtestResult] = field(default_factory=list)
    outages: list[Outage] = field(default_factory=list)

    def add(self, payload: Payload) -> None:
        """Sortiert ein Bus-Element in die passende Liste ein."""
        match payload:
            case Measurement():
                self.measurements.append(payload)
            case Event():
                self.events.append(payload)
            case RouterStatus():
                self.router.append(payload)
            case WlanStatus():
                self.wlan.append(payload)
            case SpeedtestResult():
                self.speedtests.append(payload)
            case Outage():
                self.outages.append(payload)

    def __len__(self) -> int:
        return (
            len(self.measurements)
            + len(self.events)
            + len(self.router)
            + len(self.wlan)
            + len(self.speedtests)
            + len(self.outages)
        )


class DatabaseWriter:
    """Schreibt den Inhalt des Event-Busses gebuendelt in die Datenbank."""

    def __init__(
        self,
        bus: EventBus,
        db: Database,
        run_id: int,
        interval_s: float = 5.0,
        max_rows: int = 500,
    ) -> None:
        """Initialisiert den Writer.

        Args:
            bus: Der zu leerende Event-Bus.
            db: Zieldatenbank.
            run_id: ID des laufenden Testlaufs.
            interval_s: Abstand zwischen zwei Schreibvorgaengen.
            max_rows: Ab dieser Menge wird sofort geschrieben.
        """
        self._bus = bus
        self._db = db
        self._run_id = run_id
        self._interval = interval_s
        self._max_rows = max_rows
        self._written = 0

    @property
    def written_rows(self) -> int:
        """Anzahl bisher geschriebener Zeilen."""
        return self._written

    async def run(self) -> None:
        """Hauptschleife: leert den Bus periodisch in die Datenbank."""
        try:
            while True:
                await asyncio.sleep(self._interval)
                await self.flush()
        except asyncio.CancelledError:
            # Beim Herunterfahren den Puffer noch vollstaendig wegschreiben.
            await self.flush()
            raise

    async def flush(self) -> int:
        """Schreibt alle aktuell vorliegenden Bus-Elemente.

        Returns:
            Anzahl geschriebener Zeilen.
        """
        batch = _Batch()
        for payload in self._bus.get_nowait_batch(self._max_rows):
            batch.add(payload)
        if not len(batch):
            return 0

        await asyncio.to_thread(self._write, batch)
        self._written += len(batch)
        log.debug("%d Zeilen geschrieben (gesamt %d).", len(batch), self._written)
        return len(batch)

    def _write(self, batch: _Batch) -> None:
        """Fuehrt die eigentlichen Inserts aus (laeuft im Worker-Thread)."""
        run_id = self._run_id
        self._db.insert_measurements(run_id, batch.measurements)
        self._db.insert_events(run_id, batch.events)
        self._db.insert_router_status(run_id, batch.router)
        self._db.insert_wlan_status(run_id, batch.wlan)
        self._db.insert_speedtests(run_id, batch.speedtests)
        for outage in batch.outages:
            # Ausfaelle werden vom Ping-Monitor direkt verwaltet; hier landen nur
            # bereits abgeschlossene Eintraege (z.B. von einem Agenten).
            outage_id = self._db.insert_outage(run_id, outage)
            if outage.ended_at is not None and outage.duration_s is not None:
                self._db.close_outage(
                    outage_id, outage.ended_at, outage.duration_s, str(outage.scope)
                )
