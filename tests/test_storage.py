"""Tests der Persistenz: Schema, Testlaeufe, Batch-Inserts, Export."""

from __future__ import annotations

from pathlib import Path

import pytest

from fbtest.core.models import (
    Event,
    EventType,
    Measurement,
    Outage,
    OutageScope,
    RouterStatus,
    Severity,
    SpeedtestResult,
    WlanStatus,
    iso_utc,
)
from fbtest.storage.database import Database
from fbtest.storage.exporter import export_csv, export_json


class TestSchema:
    """Grundlegende Eigenschaften der Datenbank."""

    def test_wal_mode_is_active(self, db: Database) -> None:
        """WAL erlaubt Lesen waehrend des Schreibens - wichtig fuer Langzeitlaeufe."""
        mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_reopening_is_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "x.sqlite"
        Database(path).close()
        second = Database(path)
        second.close()

    def test_timestamps_are_stored_twice(self, db: Database) -> None:
        """Jeder Zeitpunkt liegt als Unix-Timestamp UND als ISO8601-UTC vor."""
        run = db.create_test_run("t", "{}")
        db.insert_measurements(run.id, [Measurement("ping", "rtt", 12.0, "ms", timestamp=1000.0)])
        row = db.fetch_measurements(run.id)[0]
        assert row["ts"] == 1000.0
        assert row["ts_iso"] == iso_utc(1000.0)
        assert row["ts_iso"].endswith("+00:00")


class TestTestRuns:
    """Anlegen, Abschliessen und Wiederfinden von Testlaeufen."""

    def test_create_and_fetch(self, db: Database) -> None:
        run = db.create_test_run("FW 8.02", '{"a":1}', "8.02", "FRITZ!Box 7590")
        loaded = db.get_test_run(run.id)
        assert loaded is not None
        assert loaded.name == "FW 8.02"
        assert loaded.firmware_version == "8.02"
        assert loaded.router_model == "FRITZ!Box 7590"
        assert loaded.ended_at is None

    def test_open_run_is_found_for_recovery(self, db: Database) -> None:
        run = db.create_test_run("offen", "{}")
        assert db.get_open_test_run() is not None
        db.finish_test_run(run.id)
        assert db.get_open_test_run() is None

    def test_router_info_is_filled_in_later(self, db: Database) -> None:
        """Beim Wiederanlauf darf eine bereits bekannte Firmware nicht verloren gehen."""
        run = db.create_test_run("r", "{}", firmware_version="8.02")
        db.update_run_router_info(run.id, None, "FRITZ!Box 7590")
        loaded = db.get_test_run(run.id)
        assert loaded is not None
        assert loaded.firmware_version == "8.02"
        assert loaded.router_model == "FRITZ!Box 7590"

    def test_list_is_sorted_newest_first(self, db: Database) -> None:
        first = db.create_test_run("a", "{}")
        second = db.create_test_run("b", "{}")
        ids = [run.id for run in db.list_test_runs()]
        assert ids.index(second.id) < ids.index(first.id)

    def test_missing_run(self, db: Database) -> None:
        assert db.get_test_run(999) is None


class TestInserts:
    """Batch-Inserts aller Tabellen."""

    def test_measurements(self, db: Database) -> None:
        run = db.create_test_run("m", "{}")
        db.insert_measurements(
            run.id,
            [
                Measurement("ping", "rtt_avg", 12.0, "ms", meta={"target": "cloudflare"}),
                Measurement("ping", "rtt_avg", 14.0, "ms", meta={"target": "google"}),
            ],
        )
        rows = db.fetch_measurements(run.id, module="ping", metric="rtt_avg")
        assert len(rows) == 2
        assert '"target":"cloudflare"' in rows[0]["meta_json"]

    def test_empty_insert_is_a_noop(self, db: Database) -> None:
        run = db.create_test_run("m", "{}")
        db.insert_measurements(run.id, [])
        assert db.count_rows("measurements", run.id) == 0

    def test_events(self, db: Database) -> None:
        run = db.create_test_run("e", "{}")
        db.insert_events(
            run.id,
            [Event(EventType.ROUTER_REBOOT, "Neustart", Severity.CRITICAL, meta={"planned": False})],
        )
        row = db.fetch_events(run.id)[0]
        assert row["type"] == "ROUTER_REBOOT"
        assert row["severity"] == "CRITICAL"

    def test_router_status(self, db: Database) -> None:
        run = db.create_test_run("r", "{}")
        db.insert_router_status(
            run.id, [RouterStatus(router_uptime_s=1000, wan_uptime_s=500, connection_status="Connected")]
        )
        row = db.fetch_table("router_status", run.id)[0]
        assert row["router_uptime_s"] == 1000
        assert row["reachable"] == 1

    def test_wlan_and_speedtests(self, db: Database) -> None:
        run = db.create_test_run("w", "{}")
        db.insert_wlan_status(run.id, [WlanStatus(band="5GHz", ssid="X", channel=36, rssi_dbm=-50)])
        db.insert_speedtests(
            run.id,
            [SpeedtestResult(down_mbps=100.0, up_mbps=40.0, latency_idle_ms=10.0,
                             latency_loaded_ms=45.0)],
        )
        assert db.fetch_table("wlan_status", run.id)[0]["band"] == "5GHz"
        assert db.fetch_table("speedtests", run.id)[0]["down_mbps"] == 100.0

    def test_outage_open_and_close(self, db: Database) -> None:
        run = db.create_test_run("o", "{}")
        outage_id = db.insert_outage(run.id, Outage(started_at=1000.0, scope=OutageScope.WAN))
        db.close_outage(outage_id, 1060.0, 60.0, "wan")
        row = db.fetch_table("outages", run.id)[0]
        assert row["duration_s"] == 60.0
        assert row["ended_at_iso"] is not None

    def test_dangling_outages_are_closed_on_restart(self, db: Database) -> None:
        run = db.create_test_run("o", "{}")
        db.insert_outage(run.id, Outage(started_at=1000.0, scope=OutageScope.FULL))
        assert db.close_dangling_outages(run.id) == 1
        row = db.fetch_table("outages", run.id)[0]
        assert row["ended_at"] is not None
        assert "Wiederanlauf" in row["cause_guess"]


class TestQueries:
    """Auswertungsabfragen."""

    def test_metric_stats(self, db: Database) -> None:
        run = db.create_test_run("s", "{}")
        db.insert_measurements(
            run.id,
            [Measurement("ping", "rtt_avg", value, "ms") for value in (10.0, 20.0, 30.0)],
        )
        stats = db.metric_stats(run.id, "ping", "rtt_avg")
        assert stats == {"count": 3.0, "min": 10.0, "avg": 20.0, "max": 30.0}

    def test_metric_stats_without_data(self, db: Database) -> None:
        run = db.create_test_run("s", "{}")
        assert db.metric_stats(run.id, "ping", "rtt_avg") is None

    def test_percentile(self, db: Database) -> None:
        run = db.create_test_run("p", "{}")
        db.insert_measurements(
            run.id,
            [Measurement("ping", "rtt_avg", float(i), "ms") for i in range(1, 101)],
        )
        assert db.percentile(run.id, "ping", "rtt_avg", 95) == pytest.approx(96.0, abs=1.0)

    def test_unknown_table_is_rejected(self, db: Database) -> None:
        """Tabellennamen kommen nicht ungeprueft in ein SQL-Statement."""
        with pytest.raises(ValueError, match="Unbekannte Tabelle"):
            db.fetch_table("measurements; DROP TABLE events", 1)


class TestExport:
    """CSV- und JSON-Export."""

    @staticmethod
    def _filled_run(db: Database) -> int:
        run = db.create_test_run("Export", "{}", "8.02", "FRITZ!Box 7590")
        db.insert_measurements(run.id, [Measurement("ping", "rtt_avg", 12.5, "ms")])
        db.insert_events(run.id, [Event(EventType.RUN_START, "Start")])
        db.finish_test_run(run.id)
        return run.id

    def test_csv_export_creates_one_file_per_table(self, db: Database, tmp_path: Path) -> None:
        run_id = self._filled_run(db)
        files = export_csv(db, run_id, tmp_path)
        names = {path.name for path in files}
        assert f"run{run_id:04d}_measurements.csv" in names
        assert f"run{run_id:04d}_events.csv" in names
        assert f"run{run_id:04d}_testrun.csv" in names

        content = (tmp_path / f"run{run_id:04d}_measurements.csv").read_text(encoding="utf-8-sig")
        assert "rtt_avg" in content
        assert content.count(";") > 0, "CSV soll Semikolon nutzen (Excel-freundlich, CH-Locale)"

    def test_json_export_is_one_file(self, db: Database, tmp_path: Path) -> None:
        import json

        run_id = self._filled_run(db)
        path = export_json(db, run_id, tmp_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["test_run"]["firmware_version"] == "8.02"
        assert len(payload["measurements"]) == 1
        assert len(payload["events"]) == 1

    def test_export_of_unknown_run_raises(self, db: Database, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="existiert nicht"):
            export_csv(db, 999, tmp_path)
