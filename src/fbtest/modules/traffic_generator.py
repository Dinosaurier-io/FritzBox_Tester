"""Erzeugung von realitaetsnahem Netzwerkverkehr.

Ein Stabilitaetstest ohne Last ist wenig wert - viele Firmware-Probleme zeigen
sich erst unter Dauerbetrieb mit mehreren gleichzeitigen Verbindungen. Dieses
Modul erzeugt deshalb parallel mehrere "virtuelle Clients", jeder mit eigenem
Profil und eigener Statistik.

Wichtige Eigenschaften:

* **Ratenbegrenzung ueber Token-Bucket** statt Busy-Loop - eine gedrosselte
  Verbindung kostet praktisch keine CPU-Zeit.
* **Kein Task stirbt.** Netzwerkfehler fuehren zu exponentiellem Backoff und
  einem erneuten Versuch, niemals zum Abbruch des Profils.
* **Kooperation mit dem Speedtest**: Vor jedem Sendevorgang wird kurz an der
  ``TrafficGate`` gewartet. Waehrend einer Bandbreitenmessung ist sie
  geschlossen, damit die Messung nicht den eigenen Hintergrundverkehr misst.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from fbtest.config import (
    DownloadProfile,
    IperfProfile,
    StreamingProfile,
    TrafficConfig,
    TrafficProfile,
    UploadProfile,
    WebProfile,
)
from fbtest.core.events import EventBus, TrafficGate
from fbtest.core.models import EventType, Severity
from fbtest.modules.base import MonitorModule
from fbtest.proc import hidden_process_kwargs

log = logging.getLogger(__name__)

#: Groesse der Lesehaeppchen beim Herunterladen.
_CHUNK_SIZE = 64 * 1024


class TokenBucket:
    """Token-Bucket zur Begrenzung einer Datenrate.

    Der Eimer fuellt sich kontinuierlich mit ``rate`` Bytes pro Sekunde. Wer
    senden oder empfangen will, entnimmt die entsprechende Menge und wartet, bis
    genug Tokens vorhanden sind. Kurze Spitzen bis zur Eimergroesse sind erlaubt,
    im Mittel wird die Zielrate exakt eingehalten.
    """

    def __init__(self, rate_bytes_per_s: float, burst_s: float = 1.0) -> None:
        """Initialisiert den Eimer.

        Args:
            rate_bytes_per_s: Zielrate in Bytes/s. ``0`` bedeutet unbegrenzt.
            burst_s: Wie viele Sekunden Vorrat der Eimer fassen darf.
        """
        self.rate = rate_bytes_per_s
        self.capacity = max(rate_bytes_per_s * burst_s, float(_CHUNK_SIZE))
        self._tokens = self.capacity
        self._last = time.monotonic()

    @property
    def unlimited(self) -> bool:
        """True, wenn keine Begrenzung aktiv ist."""
        return self.rate <= 0

    def refill(self, now: float) -> None:
        """Fuellt den Eimer entsprechend der verstrichenen Zeit auf."""
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)

    def deficit_seconds(self, amount: float) -> float:
        """Wartezeit, bis ``amount`` Tokens verfuegbar sind (0 = sofort)."""
        if self.unlimited or self._tokens >= amount:
            return 0.0
        return (amount - self._tokens) / self.rate

    def take(self, amount: float) -> None:
        """Entnimmt Tokens (ohne Pruefung - nach vorheriger Wartezeit)."""
        self._tokens -= amount

    async def consume(self, amount: float) -> None:
        """Wartet, bis ``amount`` Bytes gesendet/empfangen werden duerfen."""
        if self.unlimited:
            return
        self.refill(time.monotonic())
        wait = self.deficit_seconds(amount)
        if wait > 0:
            await asyncio.sleep(wait)
            self.refill(time.monotonic())
        self.take(amount)


@dataclass(slots=True)
class ProfileStats:
    """Laufende Statistik eines virtuellen Clients."""

    bytes_transferred: int = 0
    requests_ok: int = 0
    requests_failed: int = 0


def mbps_to_bytes_per_s(mbps: float) -> float:
    """Rechnet Mbit/s in Bytes/s um."""
    return mbps * 1_000_000.0 / 8.0


class TrafficGenerator(MonitorModule):
    """Startet alle konfigurierten Traffic-Profile als eigenstaendige Tasks."""

    name = "traffic"

    def __init__(
        self,
        bus: EventBus,
        config: TrafficConfig,
        gate: TrafficGate,
        source: str = "master",
    ) -> None:
        super().__init__(bus, source)
        self.config = config
        self.gate = gate
        self.stats: dict[str, ProfileStats] = {}

    async def run(self) -> None:
        """Startet je Profil und Client einen Task und laeuft bis zum Abbruch."""
        active = [profile for profile in self.config.profiles if profile.enabled]
        if not active:
            self._log.info("Keine aktiven Traffic-Profile - Modul bleibt untaetig.")
            await asyncio.Event().wait()
            return

        self.emit(
            EventType.MODULE_START,
            "Traffic-Generator gestartet: "
            + ", ".join(
                f"{p.name} ({p.type} x{p.clients}"
                + (f", {p.target_rate_mbps} Mbit/s" if p.target_rate_mbps else ", unbegrenzt")
                + ")"
                for p in active
            ),
            Severity.INFO,
            profiles=len(active),
        )

        async with asyncio.TaskGroup() as group:
            for profile in active:
                for index in range(profile.clients):
                    client_id = f"{profile.name}#{index + 1}"
                    self.stats[client_id] = ProfileStats()
                    group.create_task(
                        self._client_loop(profile, client_id), name=f"traffic:{client_id}"
                    )
            group.create_task(self._stats_loop(), name="traffic:stats")

    # -- Rahmen um einen virtuellen Client ---------------------------------

    async def _client_loop(self, profile: TrafficProfile, client_id: str) -> None:
        """Fuehrt ein Profil dauerhaft aus und faengt jeden Fehler mit Backoff ab."""
        backoff = self.config.backoff_start_s
        bucket = TokenBucket(mbps_to_bytes_per_s(profile.target_rate_mbps))

        while True:
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(30.0, connect=10.0),
                    follow_redirects=True,
                    headers={"User-Agent": "fbtest/0.1 (FRITZ!Box-Langzeittest)"},
                ) as client:
                    await self._run_profile(profile, client_id, client, bucket)
                backoff = self.config.backoff_start_s
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats[client_id].requests_failed += 1
                self.emit(
                    EventType.TRAFFIC_ERROR,
                    f"Profil '{client_id}' meldet Fehler ({type(exc).__name__}: {exc}). "
                    f"Neuer Versuch in {backoff:.0f} s.",
                    Severity.WARNING,
                    profile=client_id,
                    backoff_s=backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.config.backoff_max_s)

    async def _run_profile(
        self,
        profile: TrafficProfile,
        client_id: str,
        client: httpx.AsyncClient,
        bucket: TokenBucket,
    ) -> None:
        """Verzweigt in die Umsetzung des jeweiligen Profiltyps."""
        match profile:
            case WebProfile():
                await self._run_web(profile, client_id, client)
            case StreamingProfile():
                await self._run_streaming(profile, client_id, client, bucket)
            case DownloadProfile():
                await self._run_download(profile, client_id, client, bucket)
            case UploadProfile():
                await self._run_upload(profile, client_id, client, bucket)
            case IperfProfile():
                await self._run_iperf(profile, client_id)

    # -- Profiltypen -------------------------------------------------------

    async def _run_web(
        self, profile: WebProfile, client_id: str, client: httpx.AsyncClient
    ) -> None:
        """Simuliert Surfen: Runden von HTTP-Requests mit TTFB-Messung."""
        while True:
            await self.gate.wait()
            for url in profile.urls:
                started = time.monotonic()
                async with client.stream("GET", url) as response:
                    ttfb_ms = (time.monotonic() - started) * 1000.0
                    size = 0
                    async for chunk in response.aiter_bytes(_CHUNK_SIZE):
                        size += len(chunk)
                total_ms = (time.monotonic() - started) * 1000.0

                stats = self.stats[client_id]
                stats.bytes_transferred += size
                stats.requests_ok += 1

                meta: dict[str, Any] = {
                    "profile": client_id,
                    "url": url,
                    "status": response.status_code,
                }
                self.measure("web_ttfb", ttfb_ms, "ms", **meta)
                self.measure("web_total", total_ms, "ms", size_bytes=size, **meta)
            await asyncio.sleep(profile.interval_s)

    async def _run_streaming(
        self,
        profile: StreamingProfile,
        client_id: str,
        client: httpx.AsyncClient,
        bucket: TokenBucket,
    ) -> None:
        """Simuliert einen Videostream mit konstanter Rate und Stall-Erkennung."""
        target_bytes_per_s = mbps_to_bytes_per_s(profile.target_rate_mbps)
        threshold = target_bytes_per_s * profile.stall_threshold_pct / 100.0
        window_bytes = 0
        window_start = time.monotonic()
        stalled = False

        while True:
            await self.gate.wait()
            async with client.stream("GET", profile.url) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(_CHUNK_SIZE):
                    # War der Traffic pausiert (Bandbreitenmessung), stand die
                    # Uhr des Messfensters weiter, waehrend keine Daten flossen.
                    # Ohne diesen Neustart wuerde jede Speedtest-Pause faelschlich
                    # als Stall gemeldet.
                    if not self.gate.is_open:
                        await self.gate.wait()
                        window_bytes = 0
                        window_start = time.monotonic()

                    await bucket.consume(len(chunk))
                    window_bytes += len(chunk)
                    self.stats[client_id].bytes_transferred += len(chunk)

                    elapsed = time.monotonic() - window_start
                    if elapsed < profile.stall_window_s:
                        continue

                    achieved = window_bytes / elapsed
                    self.measure(
                        "stream_rate",
                        achieved * 8 / 1e6,
                        "Mbit/s",
                        profile=client_id,
                        target_mbps=profile.target_rate_mbps,
                    )
                    if achieved < threshold and not stalled:
                        stalled = True
                        self.emit(
                            EventType.STREAM_STALL,
                            f"Stream '{client_id}' bricht ein: {achieved * 8 / 1e6:.2f} Mbit/s "
                            f"statt {profile.target_rate_mbps:.2f} Mbit/s.",
                            Severity.WARNING,
                            profile=client_id,
                            achieved_mbps=round(achieved * 8 / 1e6, 2),
                        )
                    elif achieved >= threshold and stalled:
                        stalled = False
                        self.emit(
                            EventType.STREAM_RECOVERED,
                            f"Stream '{client_id}' laeuft wieder mit "
                            f"{achieved * 8 / 1e6:.2f} Mbit/s.",
                            Severity.INFO,
                            profile=client_id,
                        )
                    window_bytes = 0
                    window_start = time.monotonic()

    async def _run_download(
        self,
        profile: DownloadProfile,
        client_id: str,
        client: httpx.AsyncClient,
        bucket: TokenBucket,
    ) -> None:
        """Laedt dauerhaft eine grosse Testdatei (optional gedrosselt)."""
        while True:
            await self.gate.wait()
            started = time.monotonic()
            size = 0
            async with client.stream("GET", profile.url) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(_CHUNK_SIZE):
                    await self.gate.wait()
                    await bucket.consume(len(chunk))
                    size += len(chunk)
                    self.stats[client_id].bytes_transferred += len(chunk)

            elapsed = max(1e-6, time.monotonic() - started)
            self.stats[client_id].requests_ok += 1
            self.measure(
                "download_rate",
                size * 8 / elapsed / 1e6,
                "Mbit/s",
                profile=client_id,
                size_bytes=size,
                duration_s=round(elapsed, 2),
            )

    async def _run_upload(
        self,
        profile: UploadProfile,
        client_id: str,
        client: httpx.AsyncClient,
        bucket: TokenBucket,
    ) -> None:
        """Sendet fortlaufend Zufallsdaten per HTTP-POST."""
        chunk = os.urandom(profile.chunk_size_kb * 1024)
        chunks_per_request = max(1, int(4 * 1024 * 1024 / len(chunk)))

        async def payload() -> AsyncIterator[bytes]:
            for _ in range(chunks_per_request):
                await self.gate.wait()
                await bucket.consume(len(chunk))
                self.stats[client_id].bytes_transferred += len(chunk)
                yield chunk

        while True:
            await self.gate.wait()
            started = time.monotonic()
            response = await client.post(
                profile.url,
                content=payload(),
                headers={"Content-Type": "application/octet-stream"},
            )
            elapsed = max(1e-6, time.monotonic() - started)
            size = chunks_per_request * len(chunk)
            self.stats[client_id].requests_ok += 1
            self.measure(
                "upload_rate",
                size * 8 / elapsed / 1e6,
                "Mbit/s",
                profile=client_id,
                status=response.status_code,
                size_bytes=size,
            )

    async def _run_iperf(self, profile: IperfProfile, client_id: str) -> None:
        """Misst den reinen LAN-Durchsatz mit ``iperf3`` (falls installiert)."""
        if shutil.which("iperf3") is None:
            self.emit(
                EventType.TRAFFIC_ERROR,
                f"Profil '{client_id}' benoetigt 'iperf3', das Programm wurde aber nicht "
                "gefunden. Das Profil bleibt inaktiv (es werden bewusst keine Ersatzwerte "
                "erzeugt).",
                Severity.WARNING,
                profile=client_id,
            )
            await asyncio.Event().wait()
            return

        while True:
            await self.gate.wait()
            args = [
                "iperf3",
                "-c",
                profile.server,
                "-p",
                str(profile.port),
                "-t",
                str(int(profile.duration_s)),
                "-J",
            ]
            if profile.reverse:
                args.append("-R")

            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **hidden_process_kwargs(),
            )
            stdout, stderr = await process.communicate()
            if process.returncode != 0:
                raise RuntimeError(
                    f"iperf3 endete mit Code {process.returncode}: "
                    f"{stderr.decode(errors='replace').strip()}"
                )

            result = json.loads(stdout.decode(errors="replace"))
            bits_per_second = float(result["end"]["sum_received"]["bits_per_second"])
            self.measure(
                "lan_throughput",
                bits_per_second / 1e6,
                "Mbit/s",
                profile=client_id,
                direction="down" if profile.reverse else "up",
            )
            await asyncio.sleep(profile.interval_s)

    # -- Statistik ---------------------------------------------------------

    async def _stats_loop(self, interval_s: float = 60.0) -> None:
        """Schreibt periodisch das je Profil uebertragene Datenvolumen fort."""
        while True:
            await asyncio.sleep(interval_s)
            for client_id, stats in self.stats.items():
                self.measure(
                    "bytes_total",
                    float(stats.bytes_transferred),
                    "bytes",
                    profile=client_id,
                    requests_ok=stats.requests_ok,
                    requests_failed=stats.requests_failed,
                )
