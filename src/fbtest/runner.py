"""Orchestrierung eines Testlaufs.

Bringt Konfiguration, Datenbank, Event-Bus, Scheduler und Module zusammen und
sorgt fuer einen sauberen Ab- und Wiederanlauf:

* Beim Start wird geprueft, ob ein Testlauf offen ist (``ended_at IS NULL``).
  Ist das der Fall, kann er fortgesetzt werden - die Luecke wird als
  ``SYSTEM_GAP`` protokolliert, statt sie stillschweigend zu verschlucken.
* Bei Strg+C werden alle Module abgebrochen, der Schreibpuffer geleert und
  ``ended_at`` gesetzt.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from fbtest.config import AppConfig, format_duration
from fbtest.core.events import EventBus, TrafficGate
from fbtest.core.models import EventType, Severity, TestRun, utc_now
from fbtest.core.scheduler import Scheduler
from fbtest.core.state import RuntimeState
from fbtest.modules.base import MonitorModule
from fbtest.modules.ping_monitor import PingMonitor
from fbtest.modules.speedtest import SpeedtestModule
from fbtest.modules.traffic_generator import TrafficGenerator
from fbtest.modules.uptime_monitor import UptimeMonitor
from fbtest.modules.wlan_monitor import WlanMonitor
from fbtest.network import detect_default_routes
from fbtest.power import PowerKeeper
from fbtest.router.fritzbox import TR064_HINT, FritzBoxClient
from fbtest.storage.database import Database
from fbtest.storage.writer import DatabaseWriter

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RunSummary:
    """Ergebnis eines beendeten Testlaufs (fuer die CLI-Ausgabe)."""

    run: TestRun
    planned_duration_s: float
    actual_duration_s: float
    rows_written: int
    interrupted: bool


@dataclass(slots=True)
class ModuleStatus:
    """Zustand eines Moduls fuer die Live-Anzeige."""

    name: str
    running: bool
    uptime_s: float
    restarts: int
    last_error: str


@dataclass(slots=True)
class RunnerStatus:
    """Momentaufnahme eines laufenden Testlaufs.

    Wird sowohl von der Terminal-Anzeige als auch vom Web-Dashboard genutzt -
    beide sehen damit garantiert dieselben Zahlen.
    """

    run_id: int | None
    run_name: str
    firmware_version: str | None
    router_model: str | None
    elapsed_s: float
    planned_duration_s: float
    remaining_s: float
    buffered: int
    dropped: int
    rows_written: int
    tr064_available: bool
    traffic_paused: bool
    traffic_pause_reason: str
    #: Wird der Energiesparmodus gerade unterdrueckt?
    standby_prevented: bool
    #: Einzeiler dazu fuer die Statusanzeige.
    standby_text: str
    modules: list[ModuleStatus]
    #: Letzter Ping je Ziel: ``{"cloudflare": {"rtt_ms": 12.4, "ok": True, ...}}``
    ping: dict[str, dict[str, object]]
    #: Uebertragene Bytes je Traffic-Profil.
    traffic: dict[str, int]
    wlan: dict[str, object]


class TestRunner:
    """Fuehrt einen kompletten Testlauf aus."""

    def __init__(
        self,
        config: AppConfig,
        base_dir: Path,
        db_path: Path,
        duration_s: float | None = None,
        name: str | None = None,
        resume: bool | None = None,
        handle_signals: bool = True,
    ) -> None:
        """Initialisiert den Testlauf.

        Args:
            config: Validierte Konfiguration.
            base_dir: Projektordner (Basis fuer relative Pfade).
            db_path: Pfad zur SQLite-Datei.
            duration_s: Laufzeit; ``None`` = Wert aus der Konfiguration.
            name: Name des Testlaufs; ``None`` = Wert aus der Konfiguration.
            resume: Offenen Testlauf fortsetzen; ``None`` = Wert aus der Konfiguration.
            handle_signals: Strg+C selbst abfangen. Das Dashboard setzt ``False``,
                weil dort der Webserver die Signale verwaltet.
        """
        self.config = config
        self.base_dir = base_dir
        self.db_path = db_path
        self.duration_s = duration_s if duration_s is not None else config.run.duration_s
        self.name = name if name is not None else config.run.name
        self.resume = resume if resume is not None else config.run.resume_open_run
        self.handle_signals = handle_signals

        self.bus = EventBus()
        self.gate = TrafficGate()
        self.state = RuntimeState()
        self.scheduler = Scheduler(
            self.bus,
            backoff_start_s=config.traffic.backoff_start_s,
            backoff_max_s=config.traffic.backoff_max_s,
            gap_threshold_s=config.run.time_gap_threshold_s,
        )
        self.db: Database | None = None
        self.writer: DatabaseWriter | None = None
        self.run: TestRun | None = None
        self.fritz: FritzBoxClient | None = None
        #: Aktive Modulinstanzen - erlaubt dem Dashboard einen Live-Blick
        #: auf die Messwerte, ohne sie erst aus der Datenbank zu lesen.
        self.modules: dict[str, MonitorModule] = {}
        #: Haelt den Rechner waehrend des Laufs wach. Bewusst hier und nicht in
        #: der Oberflaeche, damit auch 'fbtest run' davon profitiert.
        self.power = PowerKeeper(enabled=config.run.prevent_standby)
        self._interrupted = False
        self._started_mono = time.monotonic()

    # -- Lebenszyklus ------------------------------------------------------

    async def execute(self) -> RunSummary:
        """Fuehrt den Testlauf vollstaendig aus."""
        self.db = Database(self.db_path)
        try:
            await self._prepare_run()
            if self.handle_signals:
                self._install_signal_handlers()
            self._prevent_standby()
            await self._build_modules()
            await self._run_all()
        finally:
            self.power.release()
            await self._finalize()

        assert self.run is not None
        return RunSummary(
            run=self.run,
            planned_duration_s=self.duration_s,
            actual_duration_s=time.monotonic() - self._started_mono,
            rows_written=self.writer.written_rows if self.writer else 0,
            interrupted=self._interrupted,
        )

    def request_stop(self) -> None:
        """Fordert das geordnete Beenden des Testlaufs an.

        Wirkt identisch zu Strg+C: Module werden abgebrochen, der Puffer wird
        geschrieben und ``ended_at`` gesetzt. Wird vom Dashboard aufgerufen.
        """
        if self._interrupted:
            return
        self._interrupted = True
        log.warning("Abbruch angefordert - Module werden geordnet beendet ...")
        self.scheduler.request_stop()

    def status(self) -> RunnerStatus:
        """Liefert eine Momentaufnahme des laufenden Testlaufs.

        Die Werte stammen direkt aus den Modulinstanzen und nicht aus der
        Datenbank - die Anzeige ist damit sekundenaktuell, unabhaengig vom
        Schreibintervall des Writers.
        """
        elapsed = time.monotonic() - self._started_mono
        ping_snapshot: dict[str, dict[str, object]] = {}
        traffic_snapshot: dict[str, int] = {}
        wlan_snapshot: dict[str, object] = {}

        ping_module = self.modules.get("ping")
        if isinstance(ping_module, PingMonitor):
            ping_snapshot = ping_module.snapshot()

        traffic_module = self.modules.get("traffic")
        if isinstance(traffic_module, TrafficGenerator):
            traffic_snapshot = {
                name: stats.bytes_transferred for name, stats in traffic_module.stats.items()
            }

        link = self.state.link
        wlan_snapshot = {
            "is_wireless": link.is_wireless,
            "connected": link.wlan_connected,
            "ssid": link.wlan_ssid,
        }

        return RunnerStatus(
            run_id=self.run.id if self.run else None,
            run_name=self.run.name if self.run else self.name,
            firmware_version=self.run.firmware_version if self.run else None,
            router_model=self.run.router_model if self.run else None,
            elapsed_s=elapsed,
            planned_duration_s=self.duration_s,
            remaining_s=max(0.0, self.duration_s - elapsed),
            buffered=self.bus.pending,
            dropped=self.bus.dropped,
            rows_written=self.writer.written_rows if self.writer else 0,
            tr064_available=self.state.tr064_available,
            traffic_paused=not self.gate.is_open,
            traffic_pause_reason=self.gate.reason,
            standby_prevented=self.power.active,
            standby_text=self.power.status_text,
            modules=[
                ModuleStatus(
                    name=state.name,
                    running=state.running,
                    uptime_s=time.monotonic() - state.started_at,
                    restarts=state.restarts,
                    last_error=state.last_error,
                )
                for state in self.scheduler.states
            ],
            ping=ping_snapshot,
            traffic=traffic_snapshot,
            wlan=wlan_snapshot,
        )

    async def _prepare_run(self) -> None:
        """Legt den Testlauf an oder setzt einen offenen fort."""
        assert self.db is not None

        # Router moeglichst frueh ansprechen, damit die Firmware-Version im
        # Testlauf-Datensatz steht - sie ist der Schluessel fuer den Vergleich.
        self.fritz = FritzBoxClient(
            host=self.config.router.host,
            username=self.config.router.username,
            password=self.config.router.resolve_password(),
            port=self.config.router.port,
            use_tls=self.config.router.use_tls,
            timeout_s=self.config.router.timeout_s,
        )
        tr064_ok = await self.fritz.connect()
        self.state.tr064_available = tr064_ok
        firmware, model = (await self.fritz.identify()) if tr064_ok else (None, None)

        open_run = self.db.get_open_test_run() if self.resume else None
        if open_run is not None:
            gap = utc_now() - open_run.started_at
            self.run = open_run
            closed = self.db.close_dangling_outages(open_run.id)
            self.db.update_run_router_info(open_run.id, firmware, model)
            self.bus.emit(
                EventType.RUN_RESUMED,
                f"Offener Testlauf #{open_run.id} wird fortgesetzt "
                f"(begonnen vor {format_duration(gap)}).",
                Severity.WARNING,
                run_id=open_run.id,
            )
            self.bus.emit(
                EventType.SYSTEM_GAP,
                "Zwischen dem letzten Datenpunkt und dem Wiederanlauf liegt eine Luecke. "
                f"{closed} offene Ausfall-Eintraege wurden geschlossen.",
                Severity.WARNING,
                closed_outages=closed,
            )
        else:
            self.run = self.db.create_test_run(
                name=self.name,
                config_snapshot_json=self.config.snapshot_json(),
                firmware_version=firmware,
                router_model=model,
                notes=self.config.run.notes,
            )

        if not tr064_ok:
            self.bus.emit(
                EventType.TR064_DEGRADED,
                "Kein TR-064-Zugriff auf die FRITZ!Box - der Testlauf startet im "
                f"eingeschraenkten Betrieb. Grund: {self.fritz.last_error}",
                Severity.ERROR,
            )
            log.warning("%s", TR064_HINT)

        self.writer = DatabaseWriter(
            self.bus,
            self.db,
            self.run.id,
            interval_s=self.config.storage.batch_interval_s,
            max_rows=self.config.storage.batch_max_rows,
        )
        self.bus.emit(
            EventType.RUN_START,
            f"Testlauf #{self.run.id} gestartet"
            + (f" ('{self.run.name}')" if self.run.name else "")
            + f", geplante Dauer {format_duration(self.duration_s)}"
            + (
                f", Router {model} mit Firmware {firmware}"
                if firmware
                else ", Router-Telemetrie nicht verfuegbar"
            )
            + ".",
            Severity.INFO,
            run_id=self.run.id,
            duration_s=self.duration_s,
        )
        self._record_network_path()

    def _record_network_path(self) -> None:
        """Haelt fest, ueber welche Verbindung gemessen wird.

        Ohne diese Angabe laesst sich Monate spaeter nicht mehr feststellen, ob
        ein Lauf ueber Kabel oder WLAN entstanden ist - und damit auch nicht,
        ob ein Vergleich zweier Laeufe ueberhaupt zulaessig war. Sind mehrere
        Verbindungen aktiv, ist das eine Warnung wert: Das Betriebssystem
        waehlt dann selbst, und zwar unabhaengig davon, was der Benutzer
        gerade zu messen glaubt.
        """
        routes = detect_default_routes()
        if not routes:
            return

        used = routes[0]
        if len(routes) == 1:
            self.bus.emit(
                EventType.NETWORK_PATH,
                f"Gemessen wird ueber {used.interface} zu {used.gateway}.",
                Severity.INFO,
                interface=used.interface,
                gateway=used.gateway,
                routes=len(routes),
            )
            return

        others = ", ".join(f"{route.interface} (Metrik {route.metric})" for route in routes[1:])
        self.bus.emit(
            EventType.NETWORK_PATH,
            f"{len(routes)} aktive Verbindungen. Gemessen wird ueber "
            f"{used.interface} (Metrik {used.metric}), nicht benutzt: {others}.",
            Severity.WARNING,
            interface=used.interface,
            gateway=used.gateway,
            routes=len(routes),
        )

    def _prevent_standby(self) -> None:
        """Aktiviert die Standby-Sperre und protokolliert das Ergebnis.

        Ein Fehlschlag bricht den Testlauf nicht ab - er wird als Ereignis
        festgehalten. Bei der spaeteren Auswertung einer Messluecke ist damit
        nachvollziehbar, ob der Rechner schlafen gehen konnte.
        """
        if not self.config.run.prevent_standby:
            return

        self.power.acquire()
        self.bus.emit(
            EventType.POWER_KEEPALIVE,
            self.power.status_text,
            Severity.INFO if self.power.active else Severity.WARNING,
            standby_prevented=self.power.active,
        )

    async def _build_modules(self) -> None:
        """Registriert alle aktivierten Module beim Scheduler."""
        config = self.config

        if config.ping.enabled:
            monitor = PingMonitor(self.bus, config.ping, self.state)
            self.scheduler.add("ping", monitor.run)
            self.modules["ping"] = monitor

        if self.fritz is not None and self.fritz.available:
            uptime = UptimeMonitor(
                self.bus, self.fritz, self.state, config.router.poll_interval_s
            )
            self.scheduler.add("uptime", uptime.run)
            self.modules["uptime"] = uptime
        else:
            log.warning("Modul 'uptime' wird uebersprungen: kein TR-064-Zugriff.")

        if config.traffic.enabled and config.traffic.profiles:
            traffic = TrafficGenerator(self.bus, config.traffic, self.gate)
            self.scheduler.add("traffic", traffic.run)
            self.modules["traffic"] = traffic

        if config.speedtest.enabled:
            speedtest = SpeedtestModule(self.bus, config.speedtest, self.gate)
            self.scheduler.add("speedtest", speedtest.run)
            self.modules["speedtest"] = speedtest

        if config.wlan.enabled and (config.wlan.client_view or self.state.tr064_available):
            wlan = WlanMonitor(self.bus, config.wlan, self.fritz, self.state)
            self.scheduler.add("wlan", wlan.run)
            self.modules["wlan"] = wlan

        log.info("Aktive Module: %s", ", ".join(self.scheduler.task_names) or "keine")

    async def _run_all(self) -> None:
        """Startet Writer und Scheduler und wartet auf das Ende des Laufs."""
        assert self.writer is not None
        writer_task = asyncio.create_task(self.writer.run(), name="db-writer")
        try:
            await self.scheduler.run(self.duration_s)
        finally:
            writer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer_task

    async def _finalize(self) -> None:
        """Schliesst Testlauf und Datenbank sauber ab."""
        if self.run is not None:
            self.bus.emit(
                EventType.RUN_END,
                f"Testlauf #{self.run.id} beendet"
                + (" (durch Benutzer abgebrochen)" if self._interrupted else "")
                + f", Laufzeit {format_duration(time.monotonic() - self._started_mono)}.",
                Severity.INFO,
                run_id=self.run.id,
            )

        if self.writer is not None:
            # Alles, was noch im Bus liegt, muss in die Datenbank.
            with contextlib.suppress(Exception):
                while await self.writer.flush():
                    pass

        if self.db is not None and self.run is not None:
            self.db.finish_test_run(self.run.id)
        if self.db is not None:
            self.db.close()

    # -- Signale -----------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        """Faengt Strg+C ab, um geordnet statt hart zu beenden."""
        loop = asyncio.get_running_loop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except NotImplementedError:
                # Windows unterstuetzt add_signal_handler nicht - dort greift
                # der KeyboardInterrupt in der CLI.
                signal.signal(sig, lambda *_: self.request_stop())
