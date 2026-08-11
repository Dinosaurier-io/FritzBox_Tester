"""Tests des Dauerdownloads im Traffic-Generator.

Der Generator spricht sonst mit dem Netz und war deshalb bisher ungetestet.
Die Downloadschleife laesst sich aber ohne Netz pruefen: Sie bekommt ihren
HTTP-Client uebergeben, und dieser Client ist hier eine Attrappe, die
Haeppchen aus dem Speicher liefert. Damit ist nachweisbar, was von aussen
nicht zu sehen ist - dass eine Runde an der richtigen Stelle endet und die
naechste sofort beginnt.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

from fbtest.config import DownloadProfile, TrafficConfig
from fbtest.core.events import EventBus, TrafficGate
from fbtest.core.models import Measurement
from fbtest.modules.traffic_generator import ProfileStats, TokenBucket, TrafficGenerator

#: Groesse der Attrappen-Datei; ein Vielfaches der Lesehaeppchen (64 KB).
DATEI_MB = 4
DATEI_BYTES = DATEI_MB * 1024 * 1024


class FakeResponse:
    """Antwort, die eine Datei fester Groesse haeppchenweise ausgibt."""

    def __init__(self, size: int) -> None:
        self._size = size

    def raise_for_status(self) -> None:
        """Die Attrappe antwortet immer erfolgreich."""

    async def aiter_bytes(self, chunk_size: int) -> AsyncIterator[bytes]:
        """Liefert die Datei in Haeppchen der gewuenschten Groesse.

        Das ``sleep(0)`` je Haeppchen ist kein Zierrat: Es ersetzt die Wartezeit
        am Netz. Ohne einen Wartepunkt gibt die Downloadschleife die Kontrolle
        nie ab - weder ``TrafficGate.wait()`` bei offener Schranke noch
        ``TokenBucket.consume()`` ohne Drosselung tun das - und der Test wartet
        ewig auf einen Messwert, den niemand entgegennimmt.
        """
        rest = self._size
        while rest > 0:
            haeppchen = min(chunk_size, rest)
            rest -= haeppchen
            await asyncio.sleep(0)
            yield b"\0" * haeppchen


class FakeClient:
    """HTTP-Client-Attrappe, die jede Anfrage aus dem Speicher bedient."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.anfragen = 0

    def stream(self, method: str, url: str) -> Any:
        """Zaehlt die Anfrage und liefert eine frische Antwort."""
        assert method == "GET"
        self.anfragen += 1
        return _als_kontext(FakeResponse(self.size))


@contextlib.asynccontextmanager
async def _als_kontext(response: FakeResponse) -> AsyncIterator[FakeResponse]:
    """Verpackt die Antwort so, wie ``httpx.stream`` sie liefert."""
    yield response


async def lade(
    profile: DownloadProfile, runden: int, size_bytes: int = DATEI_BYTES
) -> tuple[FakeClient, list[Measurement]]:
    """Laesst die Downloadschleife ``runden`` Runden durchlaufen.

    Die Schleife laeuft absichtlich endlos; sie wird abgebrochen, sobald genug
    Messwerte auf dem Bus liegen - so wie im Betrieb der Scheduler das Modul
    beendet.

    Args:
        profile: Das zu pruefende Downloadprofil.
        runden: Anzahl abzuwartender Runden.
        size_bytes: Groesse der Attrappen-Datei.

    Returns:
        Die benutzte Client-Attrappe und die entstandenen Messwerte.
    """
    bus = EventBus()
    generator = TrafficGenerator(bus, TrafficConfig(profiles=[profile]), TrafficGate())
    generator.stats["p/0"] = ProfileStats()
    client = FakeClient(size_bytes)

    aufgabe = asyncio.create_task(
        generator._run_download(profile, "p/0", client, TokenBucket(0.0))  # type: ignore[arg-type]
    )
    messwerte: list[Measurement] = []
    try:
        while len(messwerte) < runden:
            payload = await asyncio.wait_for(bus.get(), timeout=5.0)
            if isinstance(payload, Measurement):
                messwerte.append(payload)
    finally:
        aufgabe.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await aufgabe
    return client, messwerte


class TestDauerdownload:
    """Ohne Grenze wird die Datei ganz geladen und sofort neu begonnen."""

    async def test_naechste_runde_beginnt_sofort(self) -> None:
        """Der Kern der Sache: Datei fertig, sofort die naechste Anfrage."""
        profile = DownloadProfile(name="p", url="http://x/gross.bin")
        client, messwerte = await lade(profile, runden=3)
        assert len(messwerte) == 3
        assert client.anfragen >= 3, "es wird nicht erneut angefragt"

    async def test_jede_runde_laedt_die_ganze_datei(self) -> None:
        profile = DownloadProfile(name="p", url="http://x/gross.bin")
        _, messwerte = await lade(profile, runden=2)
        assert [wert.meta["size_bytes"] for wert in messwerte] == [DATEI_BYTES] * 2

    async def test_uebertragene_menge_wird_mitgezaehlt(self) -> None:
        """Die Statistik speist die Live-Anzeige im Dashboard."""
        profile = DownloadProfile(name="p", url="http://x/gross.bin")
        bus = EventBus()
        generator = TrafficGenerator(bus, TrafficConfig(profiles=[profile]), TrafficGate())
        generator.stats["p/0"] = ProfileStats()
        aufgabe = asyncio.create_task(
            generator._run_download(  # type: ignore[arg-type]
                profile, "p/0", FakeClient(DATEI_BYTES), TokenBucket(0.0)
            )
        )
        await asyncio.wait_for(bus.get(), timeout=5.0)
        aufgabe.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await aufgabe
        assert generator.stats["p/0"].bytes_transferred >= DATEI_BYTES


class TestNeustartNachDatenmenge:
    """``restart_after_mb`` beendet eine Runde vor dem Dateiende."""

    async def test_runde_endet_an_der_grenze(self) -> None:
        """Vier Megabyte Datei, aber Neustart nach einem."""
        profile = DownloadProfile(name="p", url="http://x/gross.bin", restart_after_mb=1)
        _, messwerte = await lade(profile, runden=3)
        assert [wert.meta["size_bytes"] for wert in messwerte] == [1024 * 1024] * 3

    async def test_grenze_erzeugt_dichtere_messpunkte(self) -> None:
        """Der eigentliche Zweck bei sehr grossen Testdateien.

        Ohne Grenze liefert eine 64-MB-Datei einen einzigen Messwert; mit
        Grenze bei 1 MB entstehen in derselben Datenmenge 64 Stueck. Wer eine
        Bandbreitenschwankung sehen will, braucht die Punkte dazwischen.
        """
        mit = DownloadProfile(name="p", url="http://x/gross.bin", restart_after_mb=1)
        _, messwerte = await lade(mit, runden=4, size_bytes=64 * 1024 * 1024)
        geladen = sum(int(wert.meta["size_bytes"]) for wert in messwerte)
        assert len(messwerte) == 4
        assert geladen == 4 * 1024 * 1024, "es wurde mehr geladen als die Grenze erlaubt"

    async def test_grenze_groesser_als_die_datei_aendert_nichts(self) -> None:
        profile = DownloadProfile(name="p", url="http://x/gross.bin", restart_after_mb=64)
        _, messwerte = await lade(profile, runden=2)
        assert [wert.meta["size_bytes"] for wert in messwerte] == [DATEI_BYTES] * 2


class TestPause:
    """``pause_s`` bremst die Abfolge, ohne sie zu beenden."""

    async def test_pause_verzoegert_die_naechste_runde(self) -> None:
        profile = DownloadProfile(name="p", url="http://x/gross.bin", pause_s=0.25)
        begonnen = asyncio.get_running_loop().time()
        _, messwerte = await lade(profile, runden=2)
        gebraucht = asyncio.get_running_loop().time() - begonnen
        assert len(messwerte) == 2
        assert gebraucht >= 0.25, f"die Pause wurde nicht eingehalten ({gebraucht:.2f} s)"


class TestModell:
    """Was die Konfiguration zulaesst."""

    @pytest.mark.parametrize("feld", ["restart_after_mb", "pause_s"])
    def test_negative_werte_abgelehnt(self, feld: str) -> None:
        with pytest.raises(ValueError):
            DownloadProfile(name="p", url="http://x/1", **{feld: -1})

    def test_standard_bleibt_das_bisherige_verhalten(self) -> None:
        """Wer nichts einstellt, bekommt genau den alten Dauerdownload."""
        profile = DownloadProfile(name="p", url="http://x/1")
        assert profile.restart_after_mb == 0
        assert profile.pause_s == 0
