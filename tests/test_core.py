"""Tests von Event-Bus, Token-Bucket, Scheduler und Berichtsauswertung."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import replace
from pathlib import Path

import pytest

from fbtest.core.events import EventBus, TrafficGate
from fbtest.core.models import (
    Event,
    EventType,
    Measurement,
    Outage,
    OutageScope,
    Severity,
    SpeedtestResult,
    iso_utc,
)
from fbtest.core.models import TestRun as RunRecord
from fbtest.core.scheduler import Scheduler
from fbtest.modules.traffic_generator import TokenBucket, mbps_to_bytes_per_s
from fbtest.report import charts
from fbtest.report.generator import (
    ComparisonRow,
    Metric,
    RunAnalysis,
    _build_comparison,
    _series_by_meta,
    analyze_run,
    render_comparison,
    render_report,
)
from fbtest.storage.database import Database


class TestEventBus:
    """Verteilung von Messwerten und Ereignissen."""

    def test_publish_and_batch(self) -> None:
        bus = EventBus()
        for index in range(5):
            bus.publish(Measurement("ping", "rtt", float(index)))
        batch = bus.get_nowait_batch(10)
        assert len(batch) == 5
        assert bus.pending == 0

    def test_batch_respects_limit(self) -> None:
        bus = EventBus()
        for index in range(10):
            bus.publish(Measurement("ping", "rtt", float(index)))
        assert len(bus.get_nowait_batch(3)) == 3
        assert bus.pending == 7

    def test_full_queue_drops_instead_of_blocking(self) -> None:
        """Eine langsame Datenbank darf den Testlauf nicht anhalten."""
        bus = EventBus(maxsize=3)
        for index in range(10):
            bus.publish(Measurement("ping", "rtt", float(index)))
        assert bus.pending == 3
        assert bus.dropped == 7

    def test_emit_creates_event(self) -> None:
        bus = EventBus()
        event = bus.emit(EventType.OUTAGE_START, "Test", Severity.ERROR, scope="wan")
        assert isinstance(event, Event)
        assert event.meta == {"scope": "wan"}
        assert bus.pending == 1


class TestTrafficGate:
    """Pausieren des Traffic-Generators waehrend einer Bandbreitenmessung."""

    async def test_gate_is_open_by_default(self) -> None:
        gate = TrafficGate()
        assert gate.is_open
        await asyncio.wait_for(gate.wait(), timeout=0.5)

    async def test_paused_blocks_and_reopens(self) -> None:
        gate = TrafficGate()
        async with gate.paused("Messung"):
            assert not gate.is_open
            assert gate.reason == "Messung"
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(gate.wait(), timeout=0.05)
        assert gate.is_open
        await asyncio.wait_for(gate.wait(), timeout=0.5)

    async def test_gate_reopens_even_after_error(self) -> None:
        gate = TrafficGate()
        with pytest.raises(RuntimeError):
            async with gate.paused("Messung"):
                raise RuntimeError("Messung fehlgeschlagen")
        assert gate.is_open


class TestTokenBucket:
    """Ratenbegrenzung ohne Busy-Loop."""

    def test_unlimited_bucket(self) -> None:
        assert TokenBucket(0).unlimited is True
        assert TokenBucket(0).deficit_seconds(1e9) == 0.0

    def test_deficit_matches_rate(self) -> None:
        bucket = TokenBucket(1000.0, burst_s=1.0)
        bucket.take(bucket.capacity)  # Eimer leeren
        assert bucket.deficit_seconds(500.0) == pytest.approx(0.5, abs=0.01)

    def test_refill_over_time(self) -> None:
        bucket = TokenBucket(1000.0, burst_s=1.0)
        bucket.take(bucket.capacity)
        bucket.refill(time.monotonic() + 1.0)
        assert bucket.deficit_seconds(500.0) == 0.0

    def test_refill_is_capped_at_capacity(self) -> None:
        """Nach langer Pause darf kein unbegrenzter Vorrat entstehen."""
        bucket = TokenBucket(1000.0, burst_s=1.0)
        bucket.refill(time.monotonic() + 3600.0)
        assert bucket.deficit_seconds(bucket.capacity) == 0.0
        bucket.take(bucket.capacity)
        assert bucket.deficit_seconds(1000.0) > 0.0

    async def test_consume_actually_waits(self) -> None:
        bucket = TokenBucket(mbps_to_bytes_per_s(1.0), burst_s=0.01)
        await bucket.consume(bucket.capacity)
        started = time.monotonic()
        await bucket.consume(mbps_to_bytes_per_s(1.0) * 0.2)
        assert time.monotonic() - started >= 0.1

    def test_mbps_conversion(self) -> None:
        assert mbps_to_bytes_per_s(8.0) == 1_000_000.0


class TestScheduler:
    """Automatischer Neustart abgestuerzter Module."""

    async def test_crashed_module_is_restarted(self) -> None:
        bus = EventBus()
        scheduler = Scheduler(bus, backoff_start_s=0.01, backoff_max_s=0.02)
        attempts = 0

        async def flaky() -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise RuntimeError("Absturz")
            await asyncio.sleep(10)

        scheduler.add("flaky", flaky)
        await scheduler.run(duration_s=0.3)

        assert attempts >= 3, "Das Modul haette mehrfach neu gestartet werden muessen"
        assert scheduler.state("flaky").restarts >= 2

    async def test_one_crashing_module_does_not_stop_the_others(self) -> None:
        bus = EventBus()
        scheduler = Scheduler(bus, backoff_start_s=0.01, backoff_max_s=0.02)
        healthy_ticks = 0

        async def crashing() -> None:
            raise RuntimeError("immer kaputt")

        async def healthy() -> None:
            nonlocal healthy_ticks
            while True:
                healthy_ticks += 1
                await asyncio.sleep(0.02)

        scheduler.add("kaputt", crashing)
        scheduler.add("gesund", healthy)
        await scheduler.run(duration_s=0.25)

        assert healthy_ticks > 3
        assert scheduler.state("kaputt").restarts > 1

    async def test_duplicate_name_is_rejected(self) -> None:
        scheduler = Scheduler(EventBus())

        async def noop() -> None:
            return None

        scheduler.add("x", noop)
        with pytest.raises(ValueError, match="bereits registriert"):
            scheduler.add("x", noop)

    async def test_request_stop_ends_the_run(self) -> None:
        bus = EventBus()
        scheduler = Scheduler(bus)

        async def forever() -> None:
            await asyncio.sleep(3600)

        scheduler.add("forever", forever)
        task = asyncio.create_task(scheduler.run(duration_s=None))
        await asyncio.sleep(0.05)
        scheduler.request_stop()
        await asyncio.wait_for(task, timeout=2.0)


class TestReportGeneration:
    """Auswertung und Rendering - mit synthetischen, aber realistischen Daten."""

    @staticmethod
    def _populate(db: Database, name: str, firmware: str, outage_count: int) -> int:
        run = db.create_test_run(name, "{}", firmware, "FRITZ!Box 7590")
        base = time.time() - 3600

        measurements = []
        for index in range(60):
            timestamp = base + index * 60
            measurements.append(
                Measurement("ping", "rtt_avg", 12.0 + index % 5, "ms",
                            timestamp=timestamp, meta={"target": "cloudflare"})
            )
            measurements.append(
                Measurement("ping", "loss_pct", 0.0, "%",
                            timestamp=timestamp, meta={"target": "cloudflare"})
            )
        db.insert_measurements(run.id, measurements)
        db.insert_speedtests(
            run.id,
            [SpeedtestResult(down_mbps=95.0, up_mbps=40.0, latency_idle_ms=10.0,
                             latency_loaded_ms=38.0, timestamp=base + 1800)],
        )
        db.insert_events(
            run.id,
            [Event(EventType.ROUTER_REBOOT, "Neustart erkannt", Severity.CRITICAL,
                   timestamp=base + 900)],
        )
        for index in range(outage_count):
            outage_id = db.insert_outage(
                run.id, Outage(started_at=base + 600 + index * 300, scope=OutageScope.WAN)
            )
            db.close_outage(outage_id, base + 630 + index * 300, 30.0, "wan")
        db.finish_test_run(run.id)
        return run.id

    def test_analysis_produces_metrics(self, db: Database) -> None:
        run_id = self._populate(db, "Lauf A", "8.00", outage_count=2)
        analysis = analyze_run(db, run_id)

        labels = {metric.label for metric in analysis.summary}
        assert {"Verfuegbarkeit", "Ausfaelle", "Ungeplante Neustarts", "Latenz Ø"} <= labels
        assert analysis.raw["Ausfaelle"] == 2.0
        assert analysis.raw["Ungeplante Neustarts"] == 1.0
        assert analysis.raw["Verfuegbarkeit"] is not None
        assert 0.0 < analysis.raw["Verfuegbarkeit"] <= 100.0
        assert len(analysis.outages) == 2
        assert analysis.charts["latenz"].startswith("data:image/png;base64,")

    def test_dauerdownload_has_its_own_chart(self, db: Database) -> None:
        """Der Dauerdownload braucht eine eigene Kurve.

        Die Bandbreitenkurve zeigt den Speedtest: alle paar Minuten ein
        Spitzenwert unter Idealbedingungen. Was die Leitung unter Dauerlast
        tatsaechlich haelt, steht allein in den Runden des Downloads - ohne
        eigenes Diagramm bleibt genau dieser Verlauf im Bericht unsichtbar.
        """
        ohne_last = analyze_run(db, self._populate(db, "Ohne Last", "8.00", outage_count=0))

        run = db.create_test_run("Mit Last", "{}", "8.00", "FRITZ!Box 7590")
        base = time.time() - 3600
        db.insert_measurements(
            run.id,
            [
                Measurement("traffic", "download_rate", 40.0 + index % 7, "Mbit/s",
                            timestamp=base + index * 60, meta={"profile": "dauerlast#1"})
                for index in range(30)
            ],
        )
        db.finish_test_run(run.id)
        mit_last = analyze_run(db, run.id)

        assert mit_last.charts["dauerdownload"].startswith("data:image/png;base64,")
        assert mit_last.charts["dauerdownload"] != ohne_last.charts["dauerdownload"], (
            "Die Messwerte des Dauerdownloads landen nicht im Diagramm."
        )

    def test_empty_chart_explains_itself(self, db: Database) -> None:
        """Ein leeres Diagramm muss den Grund nennen, nicht nur die Leere.

        "Keine Daten vorhanden" sieht nach Defekt aus. Beim Dauerdownload ist
        die Ursache aber fast immer eine Einstellung: Der Messwert entsteht am
        Rundenende, und eine Runde ueber die ganze Testdatei kann laenger
        dauern als der Lauf. Ohne diesen Hinweis sucht der Leser den Fehler im
        Programm statt in 'restart_after_mb'.
        """
        ohne_hinweis = charts.timeseries_chart({}, "Titel", "Einheit")
        mit_hinweis = charts.timeseries_chart({}, "Titel", "Einheit", empty_hint="Warum leer.")
        assert mit_hinweis != ohne_hinweis, "Der Hinweis landet nicht im Platzhalterbild."

        # Und der Bericht muss ihn auch tatsaechlich mitgeben.
        leer = analyze_run(db, self._populate(db, "Ohne Last", "8.00", outage_count=0))
        assert leer.charts["dauerdownload"] != charts.timeseries_chart(
            {}, "Dauerdownload - erreichte Rate je Runde", "Mbit/s"
        ), "Das leere Dauerdownload-Diagramm nennt seinen Grund nicht."

    def test_analysis_of_unknown_run_raises(self, db: Database) -> None:
        with pytest.raises(ValueError, match="existiert nicht"):
            analyze_run(db, 999)

    def test_missing_values_say_why(self, db: Database) -> None:
        """Ein fehlender Wert muss seinen Grund nennen, nicht nur einen Strich.

        Der erzeugte Lauf misst nur den Download - genau die Lage beim
        Standard, in dem keine Upload-Gegenstelle hinterlegt ist. Ein blosser
        Strich sieht dort nach Defekt aus und schickt den Leser auf die Suche
        nach einem Fehler, den es nicht gibt.
        """
        run = db.create_test_run("Nur Download", "{}", "8.00", "FRITZ!Box 7590")
        base = time.time() - 600
        db.insert_speedtests(
            run.id,
            [SpeedtestResult(down_mbps=95.0, up_mbps=None, latency_idle_ms=10.0,
                             latency_loaded_ms=12.0, timestamp=base + 60)],
        )
        db.finish_test_run(run.id)

        summary = {metric.label: metric for metric in analyze_run(db, run.id).summary}

        upload = summary["Upload Ø"]
        assert upload.value == "nicht gemessen"
        assert upload.unit == "", "Ohne Wert darf keine Einheit danebenstehen."
        assert "upload_url" in upload.hint, "Der Grund fuer den fehlenden Wert fehlt."

        # Der Ping-Monitor lief in diesem Lauf gar nicht.
        assert summary["Latenz Ø"].value == "nicht gemessen"
        assert "Ping-Monitor" in summary["Latenz Ø"].hint

        # Vorhandene Werte bleiben unveraendert.
        assert summary["Download Ø"].value == "95.00"
        assert summary["Download Ø"].unit == "Mbit/s"

    def test_report_metrics_are_actually_recorded(self) -> None:
        """Jeder im Bericht abgefragte Metrikname muss von einem Modul stammen.

        Ein Tippfehler erzeugt keine Fehlermeldung, sondern ein dauerhaft
        leeres Diagramm - von "es gab keine Messwerte" nicht zu unterscheiden.
        Der Benutzer sucht die Ursache dann in seiner Leitung statt im Code.

        Geprueft wird auf Quelltextebene, weil ein Lauf mit synthetischen Daten
        genau die Metriken enthaelt, die der Test selbst eingefuellt hat - eine
        falsch geschriebene waere darin nicht zu bemerken.
        """
        source = Path(__file__).resolve().parent.parent / "src" / "fbtest"
        recorded: set[str] = set()
        for path in (source / "modules").glob("*.py"):
            recorded.update(
                re.findall(r'self\.measure\(\s*"([a-z0-9_]+)"', path.read_text(encoding="utf-8"))
            )
        assert recorded, "Keine measure()-Aufrufe gefunden - Test veraltet?"

        generator = (source / "report" / "generator.py").read_text(encoding="utf-8")
        requested = set(re.findall(r'metric="([a-z0-9_]+)"', generator))
        assert requested, "Keine Metrikabfragen im Bericht gefunden - Test veraltet?"
        assert requested <= recorded, (
            "Vom Bericht abgefragt, aber von keinem Modul geschrieben: "
            f"{sorted(requested - recorded)}"
        )

    def test_series_labels_name_the_host(self) -> None:
        """Die Legende muss verraten, wohin gemessen wurde.

        Zielnamen vergibt der Benutzer selbst und sie sind oft nur ein Zeichen
        lang. Ohne die Adresse dahinter laesst sich im Diagramm nicht mehr
        zuordnen, welche Kurve zu welchem Ziel gehoert.
        """
        rows = [
            {"meta_json": '{"target":"1","host":"1.1.1.1"}', "ts": 1.0, "value": 5.0},
            {"meta_json": '{"target":"fritzbox","host":"192.168.178.1"}', "ts": 2.0, "value": 3.0},
            {"meta_json": None, "ts": 3.0, "value": 4.0},
        ]
        series = _series_by_meta(rows, "target", detail="host")  # type: ignore[arg-type]

        assert set(series) == {"1 (1.1.1.1)", "fritzbox (192.168.178.1)", "gesamt"}
        assert series["1 (1.1.1.1)"] == ([1.0], [5.0])

    def test_render_report_is_self_contained(self, db: Database, tmp_path: Path) -> None:
        run_id = self._populate(db, "Lauf A", "8.00", outage_count=1)
        path = render_report(db, run_id, tmp_path)
        html = path.read_text(encoding="utf-8")

        assert path.exists()
        assert "Management-Summary" in html
        assert "8.00" in html
        # Diagramme sind eingebettet - der Bericht bleibt eine einzelne Datei.
        assert "data:image/png;base64," in html
        assert "<img src=\"http" not in html

    def test_comparison_report(self, db: Database, tmp_path: Path) -> None:
        run_a = self._populate(db, "Alt", "8.00", outage_count=4)
        run_b = self._populate(db, "Neu", "8.02", outage_count=1)
        path = render_comparison(db, run_a, run_b, tmp_path)
        html = path.read_text(encoding="utf-8")

        assert "Firmware-Vergleich" in html
        assert "8.00" in html and "8.02" in html
        # Weniger Ausfaelle in B muss als Verbesserung erkannt werden.
        assert "B ist besser" in html


class TestComparisonVerdicts:
    """Bewertungsrichtung der Kennzahlen im Vergleichsmodus."""

    @staticmethod
    def _rows(label: str, raw_a: float, raw_b: float) -> list[ComparisonRow]:
        template = next(m for m in _reference_metrics() if m.label == label)
        first = RunAnalysis(run=RunRecord(id=1, started_at=0.0))
        second = RunAnalysis(run=RunRecord(id=2, started_at=0.0))
        first.summary = [replace(template, value=str(raw_a), raw=raw_a)]
        second.summary = [replace(template, value=str(raw_b), raw=raw_b)]
        return _build_comparison(first, second)

    def test_fewer_outages_is_better(self) -> None:
        assert self._rows("Ausfaelle", 5.0, 2.0)[0].verdict == "besser"

    def test_more_outages_is_worse(self) -> None:
        assert self._rows("Ausfaelle", 2.0, 5.0)[0].verdict == "schlechter"

    def test_higher_availability_is_better(self) -> None:
        assert self._rows("Verfuegbarkeit", 99.0, 99.9)[0].verdict == "besser"

    def test_higher_bandwidth_is_better(self) -> None:
        assert self._rows("Download Ø", 90.0, 110.0)[0].verdict == "besser"

    def test_lower_latency_is_better(self) -> None:
        assert self._rows("Latenz Ø", 20.0, 12.0)[0].verdict == "besser"

    def test_identical_values_are_unchanged(self) -> None:
        assert self._rows("Latenz Ø", 12.0, 12.0)[0].verdict == "gleich"

    def test_data_volume_gets_no_verdict(self) -> None:
        """Mehr Datenvolumen ist weder gut noch schlecht - es haengt an der Laufzeit."""
        row = self._rows("Datenvolumen", 1e9, 2e9)[0]
        assert row.verdict == "neutral"
        assert row.delta == "+1.00 GB", "Die Differenz muss lesbar formatiert sein"

    def test_missing_value_is_not_comparable(self) -> None:
        template = next(m for m in _reference_metrics() if m.label == "Upload Ø")
        first = RunAnalysis(run=RunRecord(id=1, started_at=0.0))
        second = RunAnalysis(run=RunRecord(id=2, started_at=0.0))
        first.summary = [replace(template, value="-", raw=None)]
        second.summary = [replace(template, value="40.00", raw=40.0)]
        assert _build_comparison(first, second)[0].verdict == "n/a"


def _reference_metrics() -> list[Metric]:
    """Liefert die Kennzahlen-Vorlagen aus einer leeren Auswertung."""
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        database = Database(Path(folder) / "ref.sqlite")
        try:
            run_id = database.create_test_run("ref", "{}").id
            return analyze_run(database, run_id).summary
        finally:
            database.close()


def test_iso_timestamps_are_utc() -> None:
    assert iso_utc(0).startswith("1970-01-01T00:00:00")
    assert iso_utc(0).endswith("+00:00")
