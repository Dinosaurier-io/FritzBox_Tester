"""SQLite-Persistenz fuer Messwerte, Ereignisse und Testlauf-Metadaten.

Auslegung fuer Langzeittests: WAL-Modus (gleichzeitiges Lesen waehrend des
Schreibens), gebuendelte Inserts alle paar Sekunden statt eines Commits je
Messwert, und Indizes auf ``(test_run_id, timestamp)``. Damit bleiben auch
mehrere Millionen Messpunkte handhabbar.

Jeder Zeitstempel wird doppelt gespeichert: als Unix-Timestamp (``ts``) fuer
Sortierung und Rechnung sowie als ISO8601-String in UTC (``ts_iso``) fuer
Lesbarkeit und Export.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any, Self

from fbtest.core.models import (
    Event,
    EventType,
    Measurement,
    Outage,
    OutageScope,
    RouterStatus,
    Severity,
    SpeedtestResult,
    TestRun,
    WlanStatus,
    iso_utc,
    to_json,
    utc_now,
)

log = logging.getLogger(__name__)

#: Exportierbare Zeitreihen-Tabellen und ihre Sortierspalte.
#:
#: Dient zugleich als Positivliste: Tabellennamen lassen sich nicht als
#: Parameter binden, sie werden in den SQL-Text eingesetzt. Nur ein Abgleich
#: gegen diese Liste haelt fremde Bezeichner davon fern.
_TABLE_ORDER = {
    "measurements": "ts",
    "events": "ts",
    "router_status": "ts",
    "wlan_status": "ts",
    "speedtests": "ts",
    "outages": "started_at",
}

#: Version des Datenbankschemas. Wird in ``schema_meta`` abgelegt.
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS test_runs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT    NOT NULL DEFAULT '',
    started_at           REAL    NOT NULL,
    started_at_iso       TEXT    NOT NULL,
    ended_at             REAL,
    ended_at_iso         TEXT,
    firmware_version     TEXT,
    router_model         TEXT,
    config_snapshot_json TEXT,
    notes                TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS measurements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    test_run_id INTEGER NOT NULL REFERENCES test_runs(id),
    ts          REAL    NOT NULL,
    ts_iso      TEXT    NOT NULL,
    source      TEXT    NOT NULL DEFAULT 'master',
    module      TEXT    NOT NULL,
    metric      TEXT    NOT NULL,
    value       REAL,
    unit        TEXT    NOT NULL DEFAULT '',
    meta_json   TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    test_run_id INTEGER NOT NULL REFERENCES test_runs(id),
    ts          REAL    NOT NULL,
    ts_iso      TEXT    NOT NULL,
    source      TEXT    NOT NULL DEFAULT 'master',
    severity    TEXT    NOT NULL,
    type        TEXT    NOT NULL,
    message     TEXT    NOT NULL,
    meta_json   TEXT
);

CREATE TABLE IF NOT EXISTS router_status (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    test_run_id       INTEGER NOT NULL REFERENCES test_runs(id),
    ts                REAL    NOT NULL,
    ts_iso            TEXT    NOT NULL,
    reachable         INTEGER NOT NULL DEFAULT 1,
    router_uptime_s   INTEGER,
    wan_uptime_s      INTEGER,
    connection_status TEXT,
    last_error        TEXT,
    sync_down_kbps    INTEGER,
    sync_up_kbps      INTEGER,
    bytes_sent        INTEGER,
    bytes_received    INTEGER,
    host_count        INTEGER
);

CREATE TABLE IF NOT EXISTS wlan_status (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    test_run_id     INTEGER NOT NULL REFERENCES test_runs(id),
    ts              REAL    NOT NULL,
    ts_iso          TEXT    NOT NULL,
    source          TEXT    NOT NULL DEFAULT 'master',
    view            TEXT    NOT NULL DEFAULT 'router',
    band            TEXT    NOT NULL,
    ssid            TEXT,
    channel         INTEGER,
    client_count    INTEGER,
    rssi_dbm        INTEGER,
    link_speed_mbps REAL
);

CREATE TABLE IF NOT EXISTS speedtests (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    test_run_id       INTEGER NOT NULL REFERENCES test_runs(id),
    ts                REAL    NOT NULL,
    ts_iso            TEXT    NOT NULL,
    source            TEXT    NOT NULL DEFAULT 'master',
    down_mbps         REAL,
    up_mbps           REAL,
    latency_idle_ms   REAL,
    latency_loaded_ms REAL
);

CREATE TABLE IF NOT EXISTS outages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    test_run_id    INTEGER NOT NULL REFERENCES test_runs(id),
    started_at     REAL    NOT NULL,
    started_at_iso TEXT    NOT NULL,
    ended_at       REAL,
    ended_at_iso   TEXT,
    duration_s     REAL,
    scope          TEXT    NOT NULL DEFAULT 'unknown',
    cause_guess    TEXT    NOT NULL DEFAULT '',
    source         TEXT    NOT NULL DEFAULT 'master'
);

CREATE INDEX IF NOT EXISTS idx_measurements_run_ts ON measurements(test_run_id, ts);
CREATE INDEX IF NOT EXISTS idx_measurements_metric ON measurements(test_run_id, module, metric, ts);
CREATE INDEX IF NOT EXISTS idx_events_run_ts        ON events(test_run_id, ts);
CREATE INDEX IF NOT EXISTS idx_router_status_run_ts ON router_status(test_run_id, ts);
CREATE INDEX IF NOT EXISTS idx_wlan_status_run_ts   ON wlan_status(test_run_id, ts);
CREATE INDEX IF NOT EXISTS idx_speedtests_run_ts    ON speedtests(test_run_id, ts);
CREATE INDEX IF NOT EXISTS idx_outages_run          ON outages(test_run_id, started_at);
"""


class Database:
    """Kapselt alle Datenbankzugriffe des Testsystems.

    Die Klasse ist bewusst synchron gehalten. Der asynchrone Teil der Anwendung
    ruft die schreibenden Methoden ueber ``asyncio.to_thread`` auf, damit die
    Ereignisschleife nicht blockiert.
    """

    def __init__(self, path: Path) -> None:
        """Oeffnet (und erstellt bei Bedarf) die Datenbank unter ``path``."""
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._create_schema()

    def _configure(self) -> None:
        """Setzt die fuer Langzeitbetrieb noetigen PRAGMAs."""
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    def _create_schema(self) -> None:
        """Legt Tabellen und Indizes an (idempotent)."""
        with self._conn:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)",
                (str(SCHEMA_VERSION),),
            )

    # -- Kontextmanager ----------------------------------------------------

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Schliesst die Verbindung."""
        try:
            self._conn.commit()
        finally:
            self._conn.close()

    # -- Testlaeufe --------------------------------------------------------

    def create_test_run(
        self,
        name: str,
        config_snapshot_json: str,
        firmware_version: str | None = None,
        router_model: str | None = None,
        notes: str = "",
    ) -> TestRun:
        """Legt einen neuen Testlauf an und gibt ihn zurueck."""
        started = utc_now()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO test_runs
                    (name, started_at, started_at_iso, firmware_version,
                     router_model, config_snapshot_json, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    started,
                    iso_utc(started),
                    firmware_version,
                    router_model,
                    config_snapshot_json,
                    notes,
                ),
            )
        run_id = int(cur.lastrowid or 0)
        log.info("Testlauf #%d angelegt: %s", run_id, name or "(ohne Namen)")
        return TestRun(
            id=run_id,
            started_at=started,
            name=name,
            firmware_version=firmware_version,
            router_model=router_model,
            notes=notes,
        )

    def finish_test_run(self, run_id: int) -> None:
        """Setzt ``ended_at`` eines Testlaufs auf jetzt."""
        ended = utc_now()
        with self._conn:
            self._conn.execute(
                "UPDATE test_runs SET ended_at = ?, ended_at_iso = ? WHERE id = ?",
                (ended, iso_utc(ended), run_id),
            )

    def delete_test_run(self, run_id: int) -> int:
        """Loescht einen Testlauf mitsamt allen zugehoerigen Daten.

        Die Kindtabellen werden ausdruecklich zuerst geleert. Das Schema
        verzichtet bewusst auf ``ON DELETE CASCADE``: Messdaten sollen nicht
        beilaeufig verschwinden koennen, sondern nur, wenn genau das gemeint
        ist.

        Args:
            run_id: ID des zu loeschenden Testlaufs.

        Returns:
            Anzahl geloeschter Datenzeilen (ohne den Testlauf selbst).
        """
        removed = 0
        with self._conn:
            for table in ("measurements", "events", "router_status",
                          "wlan_status", "speedtests", "outages"):
                cursor = self._conn.execute(
                    f"DELETE FROM {table} WHERE test_run_id = ?", (run_id,)
                )
                removed += cursor.rowcount
            self._conn.execute("DELETE FROM test_runs WHERE id = ?", (run_id,))
        return removed

    def last_activity(self, run_id: int) -> float | None:
        """Zeitstempel des juengsten Datenpunkts eines Testlaufs.

        Bei einem abgebrochenen Lauf ist das der Zeitpunkt, an dem das Programm
        endete - die einzige belastbare Angabe darueber, wie viel Messzeit
        tatsaechlich vorliegt.

        Returns:
            Unix-Zeitstempel oder ``None``, wenn keine Daten vorliegen.
        """
        row: sqlite3.Row | None = self._conn.execute(
            """
            SELECT MAX(ts) AS ts FROM (
                SELECT MAX(ts) AS ts FROM measurements WHERE test_run_id = :id
                UNION ALL SELECT MAX(ts) FROM events        WHERE test_run_id = :id
                UNION ALL SELECT MAX(ts) FROM router_status WHERE test_run_id = :id
            )
            """,
            {"id": run_id},
        ).fetchone()
        return float(row["ts"]) if row is not None and row["ts"] is not None else None

    def update_run_router_info(
        self, run_id: int, firmware_version: str | None, router_model: str | None
    ) -> None:
        """Traegt Firmware-Version und Modell nach, sobald sie bekannt sind."""
        with self._conn:
            self._conn.execute(
                """
                UPDATE test_runs
                   SET firmware_version = COALESCE(?, firmware_version),
                       router_model     = COALESCE(?, router_model)
                 WHERE id = ?
                """,
                (firmware_version, router_model, run_id),
            )

    def get_open_test_run(self) -> TestRun | None:
        """Liefert den zuletzt gestarteten, noch offenen Testlauf (Crash-Recovery)."""
        row = self._conn.execute(
            "SELECT * FROM test_runs WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return _row_to_test_run(row) if row else None

    def get_test_run(self, run_id: int) -> TestRun | None:
        """Liefert einen Testlauf anhand seiner ID."""
        row = self._conn.execute("SELECT * FROM test_runs WHERE id = ?", (run_id,)).fetchone()
        return _row_to_test_run(row) if row else None

    def list_test_runs(self) -> list[TestRun]:
        """Liefert alle Testlaeufe, neueste zuerst."""
        rows = self._conn.execute("SELECT * FROM test_runs ORDER BY started_at DESC").fetchall()
        return [_row_to_test_run(row) for row in rows]

    # -- Schreiben ---------------------------------------------------------

    def insert_measurements(self, run_id: int, items: Sequence[Measurement]) -> None:
        """Schreibt mehrere Messwerte in einem Rutsch."""
        if not items:
            return
        with self._conn:
            self._conn.executemany(
                """
                INSERT INTO measurements
                    (test_run_id, ts, ts_iso, source, module, metric, value, unit, meta_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        m.timestamp,
                        iso_utc(m.timestamp),
                        m.source,
                        m.module,
                        m.metric,
                        m.value,
                        m.unit,
                        to_json(m.meta),
                    )
                    for m in items
                ],
            )

    def insert_events(self, run_id: int, items: Sequence[Event]) -> None:
        """Schreibt mehrere Ereignisse in einem Rutsch."""
        if not items:
            return
        with self._conn:
            self._conn.executemany(
                """
                INSERT INTO events
                    (test_run_id, ts, ts_iso, source, severity, type, message, meta_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        e.timestamp,
                        iso_utc(e.timestamp),
                        e.source,
                        str(e.severity),
                        str(e.type),
                        e.message,
                        to_json(e.meta),
                    )
                    for e in items
                ],
            )

    def insert_router_status(self, run_id: int, items: Sequence[RouterStatus]) -> None:
        """Schreibt Router-Telemetrie."""
        if not items:
            return
        with self._conn:
            self._conn.executemany(
                """
                INSERT INTO router_status
                    (test_run_id, ts, ts_iso, reachable, router_uptime_s, wan_uptime_s,
                     connection_status, last_error, sync_down_kbps, sync_up_kbps,
                     bytes_sent, bytes_received, host_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        s.timestamp,
                        iso_utc(s.timestamp),
                        int(s.reachable),
                        s.router_uptime_s,
                        s.wan_uptime_s,
                        s.connection_status,
                        s.last_error,
                        s.sync_down_kbps,
                        s.sync_up_kbps,
                        s.bytes_sent,
                        s.bytes_received,
                        s.host_count,
                    )
                    for s in items
                ],
            )

    def insert_wlan_status(self, run_id: int, items: Sequence[WlanStatus]) -> None:
        """Schreibt WLAN-Status (Router- oder Clientsicht)."""
        if not items:
            return
        with self._conn:
            self._conn.executemany(
                """
                INSERT INTO wlan_status
                    (test_run_id, ts, ts_iso, source, view, band, ssid, channel,
                     client_count, rssi_dbm, link_speed_mbps)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        w.timestamp,
                        iso_utc(w.timestamp),
                        w.source,
                        w.view,
                        w.band,
                        w.ssid,
                        w.channel,
                        w.client_count,
                        w.rssi_dbm,
                        w.link_speed_mbps,
                    )
                    for w in items
                ],
            )

    def insert_speedtests(self, run_id: int, items: Sequence[SpeedtestResult]) -> None:
        """Schreibt Speedtest-Ergebnisse."""
        if not items:
            return
        with self._conn:
            self._conn.executemany(
                """
                INSERT INTO speedtests
                    (test_run_id, ts, ts_iso, source, down_mbps, up_mbps,
                     latency_idle_ms, latency_loaded_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        s.timestamp,
                        iso_utc(s.timestamp),
                        s.source,
                        s.down_mbps,
                        s.up_mbps,
                        s.latency_idle_ms,
                        s.latency_loaded_ms,
                    )
                    for s in items
                ],
            )

    def insert_outage(self, run_id: int, outage: Outage) -> int:
        """Legt einen begonnenen Ausfall an und liefert dessen ID."""
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO outages
                    (test_run_id, started_at, started_at_iso, scope, cause_guess, source)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    outage.started_at,
                    iso_utc(outage.started_at),
                    str(outage.scope),
                    outage.cause_guess,
                    outage.source,
                ),
            )
        return int(cur.lastrowid or 0)

    def close_outage(self, outage_id: int, ended_at: float, duration_s: float, scope: str) -> None:
        """Schliesst einen laufenden Ausfall ab."""
        with self._conn:
            self._conn.execute(
                """
                UPDATE outages
                   SET ended_at = ?, ended_at_iso = ?, duration_s = ?, scope = ?
                 WHERE id = ?
                """,
                (ended_at, iso_utc(ended_at), duration_s, scope, outage_id),
            )

    def close_dangling_outages(self, run_id: int) -> int:
        """Schliesst beim Wiederanlauf alle offenen Ausfaelle eines Testlaufs.

        Returns:
            Anzahl der geschlossenen Eintraege.
        """
        now = utc_now()
        with self._conn:
            cur = self._conn.execute(
                """
                UPDATE outages
                   SET ended_at = ?, ended_at_iso = ?, duration_s = ? - started_at,
                       cause_guess = CASE WHEN cause_guess = '' THEN 'beim Wiederanlauf geschlossen'
                                          ELSE cause_guess END
                 WHERE test_run_id = ? AND ended_at IS NULL
                """,
                (now, iso_utc(now), now, run_id),
            )
        return cur.rowcount

    # -- Lesen (Auswertung / Export) ---------------------------------------

    def fetch_measurements(
        self,
        run_id: int,
        module: str | None = None,
        metric: str | None = None,
        source: str | None = None,
    ) -> list[sqlite3.Row]:
        """Liest Messwerte eines Testlaufs, optional gefiltert."""
        sql = "SELECT * FROM measurements WHERE test_run_id = ?"
        params: list[Any] = [run_id]
        if module:
            sql += " AND module = ?"
            params.append(module)
        if metric:
            sql += " AND metric = ?"
            params.append(metric)
        if source:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY ts"
        return self._conn.execute(sql, params).fetchall()

    def fetch_events(self, run_id: int) -> list[sqlite3.Row]:
        """Liest alle Ereignisse eines Testlaufs chronologisch."""
        return self._conn.execute(
            "SELECT * FROM events WHERE test_run_id = ? ORDER BY ts", (run_id,)
        ).fetchall()

    def fetch_latest_events(self, run_id: int, limit: int = 50) -> list[sqlite3.Row]:
        """Liest die juengsten Ereignisse eines Testlaufs (neueste zuerst).

        Wird vom Dashboard fuer den Ereignis-Ticker verwendet.
        """
        return self._conn.execute(
            "SELECT * FROM events WHERE test_run_id = ? ORDER BY ts DESC LIMIT ?",
            (run_id, limit),
        ).fetchall()

    def fetch_series(
        self,
        run_id: int,
        module: str,
        metric: str,
        since_ts: float = 0.0,
        limit: int = 5000,
    ) -> list[sqlite3.Row]:
        """Liest eine Messreihe ab einem Zeitpunkt (fuer Live-Diagramme).

        Args:
            run_id: Testlauf.
            module: Modulname, z.B. ``ping``.
            metric: Metrikname, z.B. ``rtt_avg``.
            since_ts: Nur Werte ab diesem Unix-Timestamp.
            limit: Obergrenze, damit ein langer Lauf den Browser nicht ueberlaedt.

        Returns:
            Zeilen in chronologischer Reihenfolge.
        """
        rows = self._conn.execute(
            """
            SELECT ts, value, meta_json FROM measurements
             WHERE test_run_id = ? AND module = ? AND metric = ? AND ts >= ?
                   AND value IS NOT NULL
             ORDER BY ts DESC LIMIT ?
            """,
            (run_id, module, metric, since_ts, limit),
        ).fetchall()
        return list(reversed(rows))

    def fetch_latest_speedtest(self, run_id: int) -> sqlite3.Row | None:
        """Liest die juengste Bandbreitenmessung eines Testlaufs."""
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM speedtests WHERE test_run_id = ? ORDER BY ts DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return row

    def fetch_latest_router_status(self, run_id: int) -> sqlite3.Row | None:
        """Liest die juengste Router-Telemetrie eines Testlaufs."""
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM router_status WHERE test_run_id = ? ORDER BY ts DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return row

    def fetch_open_outage(self, run_id: int) -> sqlite3.Row | None:
        """Liest einen aktuell laufenden (noch nicht beendeten) Ausfall."""
        row: sqlite3.Row | None = self._conn.execute(
            """
            SELECT * FROM outages
             WHERE test_run_id = ? AND ended_at IS NULL
             ORDER BY started_at DESC LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        return row

    def fetch_table(self, table: str, run_id: int) -> list[sqlite3.Row]:
        """Liest eine der Zeitreihen-Tabellen eines Testlaufs.

        Args:
            table: Tabellenname (nur aus einer festen Positivliste erlaubt).
            run_id: ID des Testlaufs.

        Raises:
            ValueError: Bei unbekanntem Tabellennamen.
        """
        return list(self.iter_table(table, run_id))

    def iter_table(self, table: str, run_id: int) -> Iterator[sqlite3.Row]:
        """Liest eine Zeitreihen-Tabelle zeilenweise.

        Fuer den Tabellenexport: Ein Lauf ueber drei Tage bringt es auf einige
        hunderttausend Messwerte. Die alle gleichzeitig im Speicher zu halten,
        nur um sie unmittelbar danach in eine Datei zu schreiben, ist unnoetig.

        Args:
            table: Tabellenname (nur aus einer festen Positivliste erlaubt).
            run_id: ID des Testlaufs.

        Yields:
            Die Zeilen in zeitlicher Reihenfolge.

        Raises:
            ValueError: Bei unbekanntem Tabellennamen.
        """
        if table not in _TABLE_ORDER:
            raise ValueError(f"Unbekannte Tabelle: {table}")
        cursor = self._conn.execute(
            f"SELECT * FROM {table} WHERE test_run_id = ? ORDER BY {_TABLE_ORDER[table]}",
            (run_id,),
        )
        yield from cursor

    def list_metric_series(self, run_id: int) -> list[sqlite3.Row]:
        """Liefert alle Messreihen eines Laufs mit Modul, Metrik und Einheit.

        Returns:
            Zeilen mit den Spalten ``module``, ``metric`` und ``unit``,
            sortiert nach Modul und Metrik.
        """
        return self._conn.execute(
            """
            SELECT module, metric, COALESCE(MAX(unit), '') AS unit
              FROM measurements
             WHERE test_run_id = ?
             GROUP BY module, metric
             ORDER BY module, metric
            """,
            (run_id,),
        ).fetchall()

    def count_rows(self, table: str, run_id: int) -> int:
        """Zaehlt die Zeilen einer Tabelle fuer einen Testlauf."""
        if table not in _TABLE_ORDER:
            raise ValueError(f"Unbekannte Tabelle: {table}")
        row = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE test_run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row["n"])

    def metric_stats(self, run_id: int, module: str, metric: str) -> dict[str, float] | None:
        """Berechnet Minimum, Mittelwert und Maximum einer Messreihe."""
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS n, MIN(value) AS vmin, AVG(value) AS vavg, MAX(value) AS vmax
              FROM measurements
             WHERE test_run_id = ? AND module = ? AND metric = ? AND value IS NOT NULL
            """,
            (run_id, module, metric),
        ).fetchone()
        if not row or not row["n"]:
            return None
        return {
            "count": float(row["n"]),
            "min": float(row["vmin"]),
            "avg": float(row["vavg"]),
            "max": float(row["vmax"]),
        }

    def percentile(self, run_id: int, module: str, metric: str, pct: float) -> float | None:
        """Berechnet ein Perzentil einer Messreihe direkt in SQLite.

        Args:
            run_id: Testlauf.
            module: Modulname.
            metric: Metrikname.
            pct: Perzentil zwischen 0 und 100 (z.B. ``95``).
        """
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS n FROM measurements
             WHERE test_run_id = ? AND module = ? AND metric = ? AND value IS NOT NULL
            """,
            (run_id, module, metric),
        ).fetchone()
        n = int(row["n"]) if row else 0
        if n == 0:
            return None
        offset = min(n - 1, max(0, round(pct / 100.0 * (n - 1))))
        value_row = self._conn.execute(
            """
            SELECT value FROM measurements
             WHERE test_run_id = ? AND module = ? AND metric = ? AND value IS NOT NULL
             ORDER BY value LIMIT 1 OFFSET ?
            """,
            (run_id, module, metric, offset),
        ).fetchone()
        return float(value_row["value"]) if value_row else None

    def vacuum_check(self) -> int:
        """Liefert die Dateigroesse der Datenbank in Bytes (fuer Diagnosezwecke)."""
        return self.path.stat().st_size if self.path.exists() else 0


def _row_to_test_run(row: sqlite3.Row) -> TestRun:
    """Wandelt eine Datenbankzeile in ein :class:`TestRun`-Objekt."""
    return TestRun(
        id=int(row["id"]),
        started_at=float(row["started_at"]),
        ended_at=float(row["ended_at"]) if row["ended_at"] is not None else None,
        name=str(row["name"] or ""),
        firmware_version=row["firmware_version"],
        router_model=row["router_model"],
        notes=str(row["notes"] or ""),
    )


def severity_of(row: sqlite3.Row) -> Severity:
    """Liest den Schweregrad einer Ereigniszeile robust aus."""
    try:
        return Severity(str(row["severity"]))
    except ValueError:
        return Severity.INFO


def event_type_of(row: sqlite3.Row) -> str:
    """Liefert den Ereignistyp einer Zeile als String."""
    return str(row["type"])


def scope_of(value: str | None) -> OutageScope:
    """Wandelt einen gespeicherten Scope-String in das Enum (robust)."""
    try:
        return OutageScope(str(value))
    except ValueError:
        return OutageScope.UNKNOWN


def known_event_types() -> Iterable[str]:
    """Alle bekannten Ereignistypen als Strings (fuer Filter in der CLI)."""
    return (t.value for t in EventType)
