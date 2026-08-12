"""Auswertung eines Testlaufs und Erzeugung des HTML-Berichts.

Der Bericht beantwortet die Frage, um die es im ganzen Projekt geht: *War diese
Firmware-Version stabil?* Dafuer werden aus den Rohdaten Kennzahlen verdichtet
(Verfuegbarkeit, Ausfaelle, Latenz, Bandbreite) und mit Diagrammen und einer
lueckenlosen Ereignistabelle belegt.

Der Vergleichsmodus stellt zwei Laeufe - also in der Regel zwei
Firmware-Versionen - direkt gegenueber. Das ist die eigentliche Kernaussage des
Systems, deshalb bekommt jede Kennzahl dort auch eine Bewertung, in welche
Richtung sie sich veraendert hat.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import sqlite3
import statistics
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from jinja2 import Environment, FileSystemLoader, select_autoescape

from fbtest import __version__
from fbtest.config import format_duration
from fbtest.core.models import TestRun, utc_now
from fbtest.paths import resource_path
from fbtest.report import charts
from fbtest.storage.database import Database

log = logging.getLogger(__name__)

#: Ordner der Jinja2-Vorlagen (siehe :func:`fbtest.paths.resource_path`).
TEMPLATE_DIR = resource_path("report", "templates")

#: In welche Richtung eine Kennzahl "besser" wird.
#:   ``lower``   - kleiner ist besser (Ausfaelle, Latenz, Verlust)
#:   ``higher``  - groesser ist besser (Verfuegbarkeit, Bandbreite, MTBF)
#:   ``neutral`` - keine Wertung moeglich (z.B. das uebertragene Datenvolumen:
#:                 es haengt vor allem an der Laufzeit, nicht an der Qualitaet)
Direction = Literal["lower", "higher", "neutral"]


@dataclass(slots=True)
class Metric:
    """Eine Kennzahl mit Wert, Einheit und optionaler Erlaeuterung."""

    label: str
    value: str
    unit: str = ""
    hint: str = ""
    raw: float | None = None
    direction: Direction = "neutral"
    #: Formatiert die Differenz zweier Laeufe (Default: zwei Nachkommastellen).
    delta_format: Callable[[float], str] | None = None


@dataclass(slots=True)
class OutageRow:
    """Ein Ausfall fuer die Ereignistabelle."""

    started: str
    ended: str
    duration: str
    scope: str
    cause: str


@dataclass(slots=True)
class EventRow:
    """Ein Ereignis fuer die chronologische Tabelle."""

    time: str
    severity: str
    type: str
    source: str
    message: str


@dataclass(slots=True)
class RunAnalysis:
    """Vollstaendige Auswertung eines Testlaufs."""

    run: TestRun
    summary: list[Metric] = field(default_factory=list)
    outages: list[OutageRow] = field(default_factory=list)
    events: list[EventRow] = field(default_factory=list)
    charts: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    raw: dict[str, float | None] = field(default_factory=dict)

    @property
    def duration_text(self) -> str:
        """Laufzeit des Testlaufs als lesbarer Text."""
        return format_duration(self.run.duration_s)


# --------------------------------------------------------------------------
# Auswertung
# --------------------------------------------------------------------------


def _local(timestamp: float | None, fallback: str = "-") -> str:
    """Formatiert einen Zeitstempel als lokale Zeit.

    Args:
        timestamp: Unix-Zeitstempel oder ``None``.
        fallback: Text fuer einen fehlenden Zeitstempel. Ein fehlendes Ende
            heisst je nach Zusammenhang etwas anderes - beim Ausfall "dauert
            an", beim Testlauf "laeuft noch". Ein Strich sagt beides nicht.

    Returns:
        Der formatierte Zeitpunkt oder ``fallback``.
    """
    if timestamp is None:
        return fallback
    return datetime.fromtimestamp(timestamp, tz=UTC).astimezone().strftime("%d.%m.%Y %H:%M:%S")


def _series_by_meta(
    rows: list[sqlite3.Row],
    key: str,
    fallback: str = "gesamt",
    detail: str | None = None,
) -> dict[str, tuple[list[float], list[float]]]:
    """Gruppiert Messwerte nach einem Feld aus ``meta_json``.

    Args:
        rows: Zeilen aus ``measurements``.
        key: Schluessel innerhalb von ``meta_json`` (z.B. ``target``).
        fallback: Gruppenname, wenn der Schluessel fehlt.
        detail: Zweiter Schluessel, der in Klammern angehaengt wird (z.B.
            ``host``). Zielnamen vergibt der Benutzer selbst und sie sind oft
            so knapp wie "1" - in der Legende ist damit nicht zu erkennen,
            welche Kurve wohin gemessen hat. "1 (1.1.1.1)" beantwortet das.

    Returns:
        Gruppenname -> (Zeitstempel, Werte).
    """
    import json

    grouped: dict[str, tuple[list[float], list[float]]] = defaultdict(lambda: ([], []))
    for row in rows:
        name = fallback
        if row["meta_json"]:
            try:
                meta = json.loads(row["meta_json"])
                name = str(meta.get(key, fallback))
                extra = str(meta.get(detail, "")) if detail else ""
                if extra and extra != name:
                    name = f"{name} ({extra})"
            except (ValueError, TypeError, AttributeError):
                name = fallback
        if row["value"] is None:
            continue
        timestamps, values = grouped[name]
        timestamps.append(float(row["ts"]))
        values.append(float(row["value"]))
    return dict(grouped)


def _values(rows: list[sqlite3.Row]) -> list[float]:
    """Extrahiert alle nicht leeren Werte aus Messzeilen."""
    return [float(row["value"]) for row in rows if row["value"] is not None]


def analyze_run(db: Database, run_id: int) -> RunAnalysis:
    """Wertet einen Testlauf vollstaendig aus.

    Args:
        db: Geoeffnete Datenbank.
        run_id: ID des Testlaufs.

    Returns:
        Die Auswertung inklusive Diagrammen.

    Raises:
        ValueError: Wenn der Testlauf nicht existiert.
    """
    run = db.get_test_run(run_id)
    if run is None:
        raise ValueError(f"Testlauf #{run_id} existiert nicht.")

    analysis = RunAnalysis(run=run)

    outage_rows = db.fetch_table("outages", run_id)
    event_rows = db.fetch_events(run_id)
    router_rows = db.fetch_table("router_status", run_id)
    speed_rows = db.fetch_table("speedtests", run_id)
    wlan_rows = db.fetch_table("wlan_status", run_id)

    duration = _analysis_window(run, outage_rows, event_rows)

    # -- Ausfaelle ---------------------------------------------------------
    durations = [float(row["duration_s"]) for row in outage_rows if row["duration_s"] is not None]
    downtime = min(sum(durations), duration)
    availability = max(0.0, 100.0 * (1.0 - downtime / duration))

    analysis.outages = [
        OutageRow(
            started=_local(row["started_at"]),
            ended=_local(row["ended_at"], "dauert an"),
            duration=format_duration(row["duration_s"] or 0.0),
            scope=str(row["scope"]),
            cause=str(row["cause_guess"] or ""),
        )
        for row in outage_rows
    ]

    # -- Ereignisse --------------------------------------------------------
    analysis.events = [
        EventRow(
            time=_local(row["ts"]),
            severity=str(row["severity"]),
            type=str(row["type"]),
            source=str(row["source"]),
            message=str(row["message"]),
        )
        for row in event_rows
    ]
    reboots = [row for row in event_rows if row["type"] == "ROUTER_REBOOT"]
    reconnects = [row for row in event_rows if row["type"] == "WAN_RECONNECT"]

    # -- Latenz / Verlust --------------------------------------------------
    rtt_rows = db.fetch_measurements(run_id, module="ping", metric="rtt_avg")
    p95_rows = db.fetch_measurements(run_id, module="ping", metric="rtt_p95")
    loss_rows = db.fetch_measurements(run_id, module="ping", metric="loss_pct")
    jitter_rows = db.fetch_measurements(run_id, module="ping", metric="jitter")

    rtt_values = _values(rtt_rows)
    loss_values = _values(loss_rows)
    jitter_values = _values(jitter_rows)
    p95_values = _values(p95_rows)

    # -- Bandbreite --------------------------------------------------------
    down_values = [float(r["down_mbps"]) for r in speed_rows if r["down_mbps"] is not None]
    up_values = [float(r["up_mbps"]) for r in speed_rows if r["up_mbps"] is not None]
    bloat_values = [
        float(r["latency_loaded_ms"]) - float(r["latency_idle_ms"])
        for r in speed_rows
        if r["latency_loaded_ms"] is not None and r["latency_idle_ms"] is not None
    ]

    # -- Dauerdownload -----------------------------------------------------
    # Eigene Reihe neben der Speedtest-Bandbreite: Der Speedtest misst alle
    # paar Minuten die Spitzenleistung, der Dauerdownload zeigt, was die
    # Leitung unter Dauerlast tatsaechlich haelt. Beides in ein Diagramm zu
    # legen wuerde zwei verschiedene Aussagen vermischen.
    download_rows = db.fetch_measurements(run_id, module="traffic", metric="download_rate")

    # -- Datenvolumen ------------------------------------------------------
    volume_bytes = _traffic_volume(db, run_id, router_rows)

    # -- Begruendungen fuer fehlende Werte ---------------------------------
    # Abgeleitet aus den Daten, nicht aus der Konfiguration: Der Bericht soll
    # beschreiben, was tatsaechlich gemessen wurde. Die Einstellungen koennen
    # sich seit dem Lauf laengst geaendert haben.
    no_ping = (
        "Der Ping-Monitor hat in diesem Lauf keinen Wert geliefert - "
        "das Modul war abgeschaltet oder ist nicht gestartet."
    )
    no_speedtest = (
        "Es liegt keine Bandbreitenmessung vor - das Modul war abgeschaltet, "
        "oder der Lauf war kuerzer als ein Messintervall."
    )
    no_upload = (
        "Nur die Download-Richtung wurde gemessen. Fuer den Upload fehlt die "
        "Gegenstelle: 'speedtest.upload_url' ist in den Einstellungen leer."
    )

    # Bewusst ohne Wertung: Das Volumen sagt nur, wie viel Last erzeugt wurde,
    # nichts ueber die Qualitaet der Verbindung.
    volume_metric = Metric(
        "Datenvolumen",
        _volume_text(volume_bytes) if volume_bytes is not None else NOT_MEASURED,
        "",
        raw=volume_bytes,
        direction="neutral",
        delta_format=_signed_volume,
        hint=(
            "Erzeugte bzw. gemessene Last - keine Qualitaetsaussage."
            if volume_bytes is not None
            else "Weder der Traffic-Generator noch der Router haben ein Volumen gemeldet."
        ),
    )

    # -- Kennzahlen --------------------------------------------------------
    mean = statistics.fmean
    analysis.summary = [
        Metric("Verfuegbarkeit", f"{availability:.3f}", "%", raw=availability,
               direction="higher",
               hint="Anteil der Testzeit ohne erkannten Ausfall."),
        Metric("Ausfaelle", str(len(outage_rows)), "", raw=float(len(outage_rows)),
               direction="lower"),
        Metric("Gesamte Ausfallzeit", format_duration(downtime), "", raw=downtime,
               direction="lower", delta_format=_signed_duration),
        Metric("Laengster Ausfall", format_duration(max(durations, default=0.0)), "",
               raw=max(durations, default=0.0), direction="lower",
               delta_format=_signed_duration),
        Metric("MTBF", _mtbf_text(outage_rows), "", direction="higher",
               hint="Mittlere Zeit zwischen zwei Ausfaellen."),
        Metric("Ungeplante Neustarts", str(len(reboots)), "", raw=float(len(reboots)),
               direction="lower",
               hint="Erkannt am Rueckgang der Router-Laufzeit."),
        Metric("WAN-Reconnects", str(len(reconnects)), "", raw=float(len(reconnects)),
               direction="lower",
               hint="Internetverbindung neu aufgebaut, Router lief weiter."),
        _metric("Latenz Ø", mean(rtt_values) if rtt_values else None, "ms",
                "lower", missing=no_ping),
        _metric("Latenz p95", max(p95_values) if p95_values else None, "ms",
                "lower", missing=no_ping),
        _metric("Jitter Ø", mean(jitter_values) if jitter_values else None, "ms",
                "lower", missing=no_ping),
        _metric("Paketverlust", mean(loss_values) if loss_values else None, "%",
                "lower", missing=no_ping),
        _metric("Download Ø", mean(down_values) if down_values else None, "Mbit/s",
                "higher", missing=no_speedtest),
        _metric("Upload Ø", mean(up_values) if up_values else None, "Mbit/s",
                "higher", missing=no_upload if speed_rows else no_speedtest),
        _metric("Bufferbloat Ø", mean(bloat_values) if bloat_values else None, "ms",
                "lower", missing=no_speedtest,
                hint="Latenzanstieg unter Volllast gegenueber Leerlauf."),
        volume_metric,
    ]
    analysis.raw = {metric.label: metric.raw for metric in analysis.summary}

    analysis.counts = {
        "Messwerte": db.count_rows("measurements", run_id),
        "Ereignisse": len(event_rows),
        "Router-Abfragen": len(router_rows),
        "WLAN-Abfragen": len(wlan_rows),
        "Speedtests": len(speed_rows),
    }

    # -- Diagramme ---------------------------------------------------------
    outage_spans = [
        (float(row["started_at"]), float(row["ended_at"] or row["started_at"]))
        for row in outage_rows
    ]
    reboot_markers = [(float(row["ts"]), "Neustart") for row in reboots]

    analysis.charts = {
        "latenz": charts.timeseries_chart(
            _series_by_meta(rtt_rows, "target", detail="host"),
            "Latenz ueber die Zeit - je Ping-Ziel",
            "RTT in ms",
            outages=outage_spans,
            markers=reboot_markers,
        ),
        "verlust": charts.timeseries_chart(
            _series_by_meta(loss_rows, "target", detail="host"),
            "Paketverlust ueber die Zeit - je Ping-Ziel",
            "Verlust in %",
            outages=outage_spans,
        ),
        "bandbreite": charts.timeseries_chart(
            {
                "Download": (
                    [float(r["ts"]) for r in speed_rows if r["down_mbps"] is not None],
                    down_values,
                ),
                "Upload": (
                    [float(r["ts"]) for r in speed_rows if r["up_mbps"] is not None],
                    up_values,
                ),
            },
            "Bandbreite ueber die Zeit",
            "Mbit/s",
            outages=outage_spans,
        ),
        "dauerdownload": charts.timeseries_chart(
            _series_by_meta(download_rows, "profile"),
            "Dauerdownload - erreichte Rate je Runde",
            "Mbit/s",
            outages=outage_spans,
            markers=reboot_markers,
            empty_hint=(
                "Ein Messwert entsteht am Ende einer Download-Runde. Dauert eine Runde "
                "laenger als der ganze Lauf, bleibt die Kurve leer:\n"
                "eine 1-GiB-Datei bei 10 Mbit/s braucht rund 14 Minuten. "
                "Kuerzere Runden ueber 'restart_after_mb' im Download-Profil."
            ),
        ),
        "uptime": charts.timeseries_chart(
            {
                "Router-Laufzeit": (
                    [float(r["ts"]) for r in router_rows if r["router_uptime_s"] is not None],
                    [
                        float(r["router_uptime_s"]) / 3600
                        for r in router_rows
                        if r["router_uptime_s"] is not None
                    ],
                ),
                "WAN-Laufzeit": (
                    [float(r["ts"]) for r in router_rows if r["wan_uptime_s"] is not None],
                    [
                        float(r["wan_uptime_s"]) / 3600
                        for r in router_rows
                        if r["wan_uptime_s"] is not None
                    ],
                ),
            },
            "Laufzeit von Router und WAN-Verbindung (ein Absturz auf 0 = Neustart)",
            "Stunden",
            markers=reboot_markers,
        ),
        "wlan": charts.timeseries_chart(
            _series_by_meta(
                db.fetch_measurements(run_id, module="wlan", metric="local_rssi"), "band"
            )
            or _series_by_meta(
                db.fetch_measurements(run_id, module="wlan", metric="client_rssi"), "band"
            ),
            "WLAN-Signalstaerke ueber die Zeit",
            "RSSI in dBm",
            outages=outage_spans,
        ),
        "latenz_verteilung": charts.histogram_chart(
            rtt_values, "Verteilung der Latenz", "RTT in ms"
        ),
    }

    return analysis


def _analysis_window(
    run: TestRun, outage_rows: list[sqlite3.Row], event_rows: list[sqlite3.Row]
) -> float:
    """Bestimmt den auszuwertenden Zeitraum in Sekunden.

    Normalerweise ist das schlicht die Laufzeit des Testlaufs. Bei fortgesetzten
    Laeufen und importierten Daten kann es aber Datenpunkte ausserhalb dieses
    Fensters geben; dann wird der tatsaechliche Datenbereich verwendet. Ohne
    diese Korrektur koennte die Summe der Ausfallzeiten die Bezugsdauer
    ueberschreiten und die Verfuegbarkeit auf 0 % rechnen.

    Returns:
        Dauer in Sekunden, mindestens 1 s.
    """
    timestamps: list[float] = [run.started_at]
    timestamps.append(run.ended_at if run.ended_at is not None else utc_now())
    for row in outage_rows:
        timestamps.append(float(row["started_at"]))
        if row["ended_at"] is not None:
            timestamps.append(float(row["ended_at"]))
    timestamps.extend(float(row["ts"]) for row in event_rows)
    return max(1.0, max(timestamps) - min(timestamps))


def _traffic_volume(db: Database, run_id: int, router_rows: list[sqlite3.Row]) -> float | None:
    """Ermittelt das uebertragene Datenvolumen in Bytes.

    Bevorzugt werden die Byte-Zaehler der FRITZ!Box (sie erfassen den gesamten
    Verkehr im Netz). Fehlen sie, wird auf die Summe der Traffic-Profile
    zurueckgegriffen - die erfasst nur den selbst erzeugten Verkehr.
    """
    sent = [float(r["bytes_sent"]) for r in router_rows if r["bytes_sent"] is not None]
    received = [float(r["bytes_received"]) for r in router_rows if r["bytes_received"] is not None]
    if sent and received:
        delta = (max(sent) - min(sent)) + (max(received) - min(received))
        if delta > 0:
            return delta

    rows = db.fetch_measurements(run_id, module="traffic", metric="bytes_total")
    if not rows:
        return None
    import json

    per_profile: dict[str, float] = {}
    for row in rows:
        if row["value"] is None:
            continue
        profile = "?"
        if row["meta_json"]:
            with contextlib.suppress(ValueError, TypeError):
                profile = str(json.loads(row["meta_json"]).get("profile", "?"))
        per_profile[profile] = max(per_profile.get(profile, 0.0), float(row["value"]))
    return sum(per_profile.values()) or None


def _signed_duration(delta: float) -> str:
    """Formatiert eine Zeitdifferenz mit Vorzeichen."""
    return ("+" if delta >= 0 else "-") + format_duration(abs(delta))


def _signed_volume(delta: float) -> str:
    """Formatiert eine Volumendifferenz mit Vorzeichen und passender Einheit."""
    return ("+" if delta >= 0 else "-") + _volume_text(abs(delta))


def _mtbf_text(outage_rows: list[sqlite3.Row]) -> str:
    """Formatiert die mittlere Zeit zwischen Ausfaellen."""
    if len(outage_rows) < 2:
        return "n/a (weniger als 2 Ausfaelle)"
    starts = sorted(float(row["started_at"]) for row in outage_rows)
    gaps = [b - a for a, b in itertools.pairwise(starts)]
    return format_duration(statistics.fmean(gaps))


#: Text anstelle eines Wertes, der nicht erhoben wurde.
NOT_MEASURED = "nicht gemessen"


def _metric(
    label: str,
    value: float | None,
    unit: str,
    direction: Direction,
    *,
    missing: str,
    hint: str = "",
    digits: int = 2,
) -> Metric:
    """Baut eine Kennzahl und erklaert sie, falls kein Wert vorliegt.

    Ein blosser Strich sieht aus wie ein Defekt. Tatsaechlich fehlt ein Wert
    fast immer, weil etwas nicht eingerichtet oder nicht aktiv war - und das
    gehoert in den Bericht, sonst sucht der Leser einen Fehler, den es nicht
    gibt. Die Einheit entfaellt dann: "nicht gemessen Mbit/s" waere Unsinn.

    Args:
        label: Beschriftung der Kachel.
        value: Messwert oder ``None``.
        unit: Einheit, nur bei vorhandenem Wert.
        direction: Bewertungsrichtung fuer den Vergleichsmodus.
        missing: Begruendung, die bei fehlendem Wert angezeigt wird.
        hint: Erlaeuterung bei vorhandenem Wert.
        digits: Nachkommastellen.

    Returns:
        Die fertige Kennzahl.
    """
    if value is None:
        return Metric(label, NOT_MEASURED, "", hint=missing, direction=direction)
    return Metric(
        label, f"{value:.{digits}f}", unit, hint=hint, raw=value, direction=direction
    )


def _volume_text(value: float | None) -> str:
    """Formatiert ein Datenvolumen mit passender Einheit."""
    if value is None:
        return "-"
    for unit, factor in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if value >= factor:
            return f"{value / factor:.2f} {unit}"
    return f"{value:.0f} B"


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _environment() -> Environment:
    """Erzeugt die Jinja2-Umgebung fuer die Berichtsvorlagen."""
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_report(db: Database, run_id: int, target_dir: Path) -> Path:
    """Erzeugt den HTML-Bericht eines Testlaufs.

    Args:
        db: Geoeffnete Datenbank.
        run_id: ID des Testlaufs.
        target_dir: Zielordner.

    Returns:
        Pfad der geschriebenen HTML-Datei.
    """
    analysis = analyze_run(db, run_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    template = _environment().get_template("report.html.j2")
    html = template.render(
        a=analysis,
        run=analysis.run,
        generated=_local(datetime.now(tz=UTC).timestamp()),
        version=__version__,
        started=_local(analysis.run.started_at),
        ended=_local(analysis.run.ended_at, "laeuft noch"),
    )

    path = target_dir / f"bericht_run{run_id:04d}.html"
    path.write_text(html, encoding="utf-8")
    log.info("Bericht geschrieben: %s", path)
    return path


def render_comparison(db: Database, run_a: int, run_b: int, target_dir: Path) -> Path:
    """Erzeugt einen Vergleichsbericht zweier Testlaeufe.

    Args:
        db: Geoeffnete Datenbank.
        run_a: ID des ersten Laufs (Referenz).
        run_b: ID des zweiten Laufs (Vergleich).
        target_dir: Zielordner.

    Returns:
        Pfad der geschriebenen HTML-Datei.
    """
    first = analyze_run(db, run_a)
    second = analyze_run(db, run_b)
    target_dir.mkdir(parents=True, exist_ok=True)

    comparison = _build_comparison(first, second)
    overlays = _build_overlays(db, run_a, run_b, first, second)

    template = _environment().get_template("compare.html.j2")
    html = template.render(
        a=first,
        b=second,
        rows=comparison,
        overlays=overlays,
        generated=_local(datetime.now(tz=UTC).timestamp()),
        version=__version__,
    )

    path = target_dir / f"vergleich_run{run_a:04d}_run{run_b:04d}.html"
    path.write_text(html, encoding="utf-8")
    log.info("Vergleichsbericht geschrieben: %s", path)
    return path


@dataclass(slots=True)
class ComparisonRow:
    """Eine Kennzahl im direkten Vergleich zweier Laeufe."""

    label: str
    unit: str
    value_a: str
    value_b: str
    delta: str
    verdict: str  # "besser", "schlechter", "gleich" oder "n/a"


def _build_comparison(first: RunAnalysis, second: RunAnalysis) -> list[ComparisonRow]:
    """Stellt die Kennzahlen beider Laeufe gegenueber und bewertet die Richtung."""
    rows: list[ComparisonRow] = []
    lookup_b = {metric.label: metric for metric in second.summary}

    for metric_a in first.summary:
        metric_b = lookup_b.get(metric_a.label)
        if metric_b is None:
            continue

        delta_text = "-"
        verdict = "n/a"
        if metric_a.raw is not None and metric_b.raw is not None:
            delta = metric_b.raw - metric_a.raw
            formatter = metric_a.delta_format
            delta_text = formatter(delta) if formatter else f"{delta:+.2f}"
            if metric_a.direction == "neutral":
                verdict = "neutral"
            elif abs(delta) < 1e-9:
                verdict = "gleich"
            else:
                improved = delta < 0 if metric_a.direction == "lower" else delta > 0
                verdict = "besser" if improved else "schlechter"

        rows.append(
            ComparisonRow(
                label=metric_a.label,
                unit=metric_a.unit,
                value_a=metric_a.value,
                value_b=metric_b.value,
                delta=delta_text,
                verdict=verdict,
            )
        )
    return rows


def _build_overlays(
    db: Database, run_a: int, run_b: int, first: RunAnalysis, second: RunAnalysis
) -> dict[str, str]:
    """Erzeugt die Overlay-Diagramme des Vergleichsmodus.

    Da beide Laeufe zu unterschiedlichen Zeiten stattfanden, werden die
    Zeitachsen auf den jeweiligen Startzeitpunkt normiert - erst dadurch sind
    die Verlaeufe uebereinanderlegbar.
    """
    label_a = _run_label(first)
    label_b = _run_label(second)

    def relative(run_id: int, start: float, metric: str) -> tuple[list[float], list[float]]:
        rows = db.fetch_measurements(run_id, module="ping", metric=metric)
        base = first.run.started_at
        return (
            [base + (float(row["ts"]) - start) for row in rows if row["value"] is not None],
            [float(row["value"]) for row in rows if row["value"] is not None],
        )

    latency = {
        label_a: relative(run_a, first.run.started_at, "rtt_avg"),
        label_b: relative(run_b, second.run.started_at, "rtt_avg"),
    }
    loss = {
        label_a: relative(run_a, first.run.started_at, "loss_pct"),
        label_b: relative(run_b, second.run.started_at, "loss_pct"),
    }

    return {
        "latenz": charts.timeseries_chart(
            latency, "Latenz im Vergleich (Zeitachse auf den Laufbeginn normiert)", "RTT in ms"
        ),
        "verlust": charts.timeseries_chart(
            loss, "Paketverlust im Vergleich", "Verlust in %"
        ),
        "verfuegbarkeit": charts.bar_chart(
            [label_a, label_b],
            [
                first.raw.get("Verfuegbarkeit") or 0.0,
                second.raw.get("Verfuegbarkeit") or 0.0,
            ],
            "Verfuegbarkeit im Vergleich",
            "%",
        ),
        "ausfaelle": charts.bar_chart(
            [label_a, label_b],
            [first.raw.get("Ausfaelle") or 0.0, second.raw.get("Ausfaelle") or 0.0],
            "Anzahl Ausfaelle im Vergleich",
            "Anzahl",
        ),
        "neustarts": charts.bar_chart(
            [label_a, label_b],
            [
                first.raw.get("Ungeplante Neustarts") or 0.0,
                second.raw.get("Ungeplante Neustarts") or 0.0,
            ],
            "Ungeplante Router-Neustarts im Vergleich",
            "Anzahl",
        ),
    }


def _run_label(analysis: RunAnalysis) -> str:
    """Kurzbezeichnung eines Laufs fuer Diagrammlegenden."""
    firmware = analysis.run.firmware_version or "Firmware unbekannt"
    return f"#{analysis.run.id} {firmware}"


def to_dict(analysis: RunAnalysis) -> dict[str, Any]:
    """Wandelt eine Auswertung in ein Dictionary (fuer JSON-Ausgaben)."""
    return {
        "run_id": analysis.run.id,
        "name": analysis.run.name,
        "firmware_version": analysis.run.firmware_version,
        "router_model": analysis.run.router_model,
        "duration_s": round(analysis.run.duration_s, 1),
        "metrics": {metric.label: metric.raw for metric in analysis.summary},
        "counts": analysis.counts,
    }
