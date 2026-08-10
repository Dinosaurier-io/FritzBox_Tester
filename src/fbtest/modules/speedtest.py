"""Periodische WAN-Bandbreitenmessung inklusive Bufferbloat-Indikator.

Bewusst als eigene Implementierung statt ueber einen fertigen Speedtest-Dienst:
Fremde Dienste waehlen wechselnde Server, was Ergebnisse ueber Tage hinweg
unvergleichbar macht. Hier wird immer dieselbe Testdatei ueber dieselbe Anzahl
Verbindungen geladen - das ist zwar kein "offizieller" Speedtest, dafuer aber
zwischen zwei Firmware-Laeufen sauber vergleichbar. Genau darum geht es.

Messverfahren:

1. **Leerlauf-Latenz** messen (vor der Last).
2. **Warmlaufphase** (Slow-Start von TCP) - wird nicht gewertet.
3. **Messfenster**: uebertragene Bytes / Zeit ueber N parallele Verbindungen.
4. Waehrend der Last erneut Latenz messen -> die Differenz zur Leerlauf-Latenz
   ist der **Bufferbloat-Wert**. Er zeigt, wie stark ein Router unter Volllast
   die Reaktionszeit verschlechtert - fuer Firmware-Vergleiche oft
   aussagekraeftiger als die Bandbreite selbst.

Waehrend der Messung wird der Traffic-Generator ueber die ``TrafficGate``
pausiert, sonst misst man den eigenen Hintergrundverkehr mit.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import statistics
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from fbtest.config import SpeedtestConfig
from fbtest.core.events import EventBus, TrafficGate
from fbtest.core.models import EventType, Severity, SpeedtestResult
from fbtest.modules.base import MonitorModule
from fbtest.modules.ping_monitor import PingBackend, select_backend

log = logging.getLogger(__name__)

_CHUNK_SIZE = 64 * 1024

#: Einzelmessungen je Latenzwert.
#:
#: Zehn Stichproben im Abstand von 0.2 s kosten zwei Sekunden je Messung.
#: Der Median aus fuenf Werten kippt bereits, wenn zwei davon ausreissen -
#: bei zehn braucht es fuenf, und so viele Ausreisser sind kein Messfehler
#: mehr, sondern ein Befund.
LATENCY_SAMPLES = 10


@dataclass(slots=True)
class _Counter:
    """Gemeinsamer Byte-Zaehler aller parallelen Verbindungen einer Messung."""

    total: int = 0
    measuring: bool = False
    measured: int = 0

    def add(self, amount: int) -> None:
        """Zaehlt Bytes; im Messfenster zusaetzlich getrennt."""
        self.total += amount
        if self.measuring:
            self.measured += amount


class SpeedtestModule(MonitorModule):
    """Fuehrt in festem Abstand eine Bandbreitenmessung durch."""

    name = "speedtest"

    def __init__(
        self,
        bus: EventBus,
        config: SpeedtestConfig,
        gate: TrafficGate,
        source: str = "master",
    ) -> None:
        super().__init__(bus, source)
        self.config = config
        self.gate = gate
        self._backend: PingBackend | None = None

    async def run(self) -> None:
        """Wartet jeweils das Intervall ab und misst dann."""
        self._backend = await select_backend(prefer_icmplib=True)
        # Erste Messung nicht sofort: Erst laeuft der uebrige Testaufbau an.
        await asyncio.sleep(min(60.0, self.config.interval_s))

        while True:
            started = time.monotonic()
            try:
                await self._measure_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.emit(
                    EventType.SPEEDTEST_FAILED,
                    f"Bandbreitenmessung fehlgeschlagen ({type(exc).__name__}: {exc}).",
                    Severity.WARNING,
                )
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.0, self.config.interval_s - elapsed))

    async def _measure_once(self) -> None:
        """Fuehrt eine vollstaendige Messung durch und veroeffentlicht sie."""
        if self.config.pause_traffic:
            async with self.gate.paused("Bandbreitenmessung laeuft"):
                # Kurz warten, damit laufende Uebertragungen wirklich ruhen.
                await asyncio.sleep(2.0)
                result = await self._run_measurement()
        else:
            result = await self._run_measurement()

        result.source = self.source
        self.bus.publish(result)

        if result.down_mbps is not None:
            self.measure("down", result.down_mbps, "Mbit/s")
        if result.up_mbps is not None:
            self.measure("up", result.up_mbps, "Mbit/s")
        if result.latency_idle_ms is not None:
            self.measure("latency_idle", result.latency_idle_ms, "ms")
        if result.latency_loaded_ms is not None:
            self.measure("latency_loaded", result.latency_loaded_ms, "ms")
        if result.bufferbloat_ms is not None:
            self.measure("bufferbloat", result.bufferbloat_ms, "ms")

        self.emit(
            EventType.SPEEDTEST_DONE,
            "Bandbreitenmessung: "
            f"Down {_fmt(result.down_mbps)} Mbit/s, Up {_fmt(result.up_mbps)} Mbit/s, "
            f"Latenz leer {_fmt(result.latency_idle_ms)} ms / unter Last "
            f"{_fmt(result.latency_loaded_ms)} ms "
            f"(Bufferbloat {_fmt(result.bufferbloat_ms)} ms).",
            Severity.INFO,
            down_mbps=result.down_mbps,
            up_mbps=result.up_mbps,
            bufferbloat_ms=result.bufferbloat_ms,
        )

    async def _run_measurement(self) -> SpeedtestResult:
        """Fuehrt Latenz-, Download- und Uploadmessung nacheinander aus."""
        result = SpeedtestResult()
        result.latency_idle_ms = await self._measure_latency()

        download, loaded_latency = await self._measure_download()
        result.down_mbps = download
        result.latency_loaded_ms = loaded_latency

        if self.config.upload_url:
            result.up_mbps = await self._measure_upload()

        return result

    async def _measure_latency(self, samples: int = LATENCY_SAMPLES) -> float | None:
        """Misst die Latenz zum konfigurierten Referenzziel.

        Ausgewertet wird der **Median**, nicht der Mittelwert. Der Grund zeigt
        sich vor allem im WLAN: Im Leerlauf schaltet der Funkadapter in den
        Stromsparmodus und antwortet erst zum naechsten Beacon. Ein einzelner
        solcher Wert von 16 ms hebt den Mittelwert aus fuenf Messungen von 4 auf
        6.4 ms - genug, um den daraus berechneten Bufferbloat-Wert ins Negative
        zu drehen und damit sinnlos zu machen. Der Median bleibt davon
        unberuehrt.

        Args:
            samples: Anzahl der Einzelmessungen.

        Returns:
            Der Median in Millisekunden oder ``None``, wenn keine Antwort kam.
        """
        if self._backend is None:
            return None
        values: list[float] = []
        for _ in range(samples):
            rtt = await self._backend.ping(self.config.latency_host, 2.0)
            if rtt is not None:
                values.append(rtt)
            await asyncio.sleep(0.2)
        return statistics.median(values) if values else None

    async def _measure_download(self) -> tuple[float | None, float | None]:
        """Misst die Downloadrate ueber N parallele Verbindungen.

        Returns:
            Tupel aus Rate in Mbit/s und Latenz unter Last in Millisekunden.
        """
        counter = _Counter()
        stop = asyncio.Event()

        async def worker() -> None:
            """Laedt die Testdatei, bis das Stoppsignal kommt."""
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True
            ) as client:
                while not stop.is_set():
                    async with client.stream("GET", self.config.download_url) as response:
                        response.raise_for_status()
                        async for chunk in response.aiter_bytes(_CHUNK_SIZE):
                            counter.add(len(chunk))
                            if stop.is_set():
                                return

        workers = [asyncio.create_task(worker()) for _ in range(self.config.connections)]
        latency_loaded: float | None = None
        try:
            await asyncio.sleep(self.config.warmup_s)

            counter.measured = 0
            counter.measuring = True
            started = time.monotonic()

            # Latenz waehrend der Last messen - das ist der Bufferbloat-Test.
            latency_task = asyncio.create_task(self._measure_latency())
            await asyncio.sleep(self.config.measure_duration_s)

            elapsed = time.monotonic() - started
            counter.measuring = False
            measured_bytes = counter.measured
            latency_loaded = await latency_task
        finally:
            stop.set()
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

        if elapsed <= 0 or measured_bytes == 0:
            return None, latency_loaded
        return measured_bytes * 8 / elapsed / 1e6, latency_loaded

    async def _measure_upload(self) -> float | None:
        """Misst die Uploadrate durch Senden generierter Zufallsdaten."""
        chunk = os.urandom(_CHUNK_SIZE)
        total_chunks = max(1, self.config.upload_size_mb * 1024 * 1024 // _CHUNK_SIZE)
        sent = 0
        deadline = time.monotonic() + self.config.measure_duration_s + self.config.warmup_s
        measure_start: float | None = None
        measured_bytes = 0

        async def payload() -> AsyncIterator[bytes]:
            """Erzeugt den Upload-Datenstrom und zaehlt das Messfenster mit."""
            nonlocal sent, measure_start, measured_bytes
            for _ in range(total_chunks):
                now = time.monotonic()
                if measure_start is None and now >= deadline - self.config.measure_duration_s:
                    measure_start = now
                    measured_bytes = 0
                sent += len(chunk)
                if measure_start is not None:
                    measured_bytes += len(chunk)
                if now >= deadline:
                    return
                yield chunk

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0), follow_redirects=True
        ) as client:
            with contextlib.suppress(httpx.HTTPError):
                await client.post(
                    self.config.upload_url,
                    content=payload(),
                    headers={"Content-Type": "application/octet-stream"},
                )

        if measure_start is None or measured_bytes == 0:
            return None
        elapsed = time.monotonic() - measure_start
        return measured_bytes * 8 / elapsed / 1e6 if elapsed > 0 else None


def _fmt(value: float | None) -> str:
    """Formatiert einen Messwert fuer Logmeldungen ('-' bei fehlendem Wert)."""
    return f"{value:.2f}" if value is not None else "-"
