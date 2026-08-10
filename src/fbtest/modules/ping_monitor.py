"""Erreichbarkeits-Monitoring per ICMP inklusive Ausfallklassifikation.

Das Modul beantwortet zwei Fragen gleichzeitig:

1. **Wie gut** ist die Verbindung? -> Latenz, Jitter, Paketverlust, aggregiert in
   Zeitfenstern.
2. **Wo** klemmt es, wenn nichts mehr geht? -> Antwortet die FRITZ!Box noch, das
   Internet aber nicht, liegt das Problem hinter dem Router (``scope=wan``).
   Antwortet auch die Box nicht mehr, liegt es davor (``scope=gateway``) - oder
   schlicht an der abgerissenen WLAN-Verbindung des Messgeraets (``scope=wlan``).

Diese Unterscheidung ist der eigentliche Nutzwert des Moduls: Ohne sie waere ein
Ausfallprotokoll nicht interpretierbar.

Die Auswertelogik (Aggregation, Ausfallerkennung, Ping-Ausgabe-Parser) steckt in
reinen Funktionen bzw. netzwerkfreien Klassen und ist damit ohne echtes Netz
testbar.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import platform
import re
import socket
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

from fbtest.config import PingConfig, PingTarget
from fbtest.core.events import EventBus
from fbtest.core.models import EventType, Outage, OutageScope, Severity, utc_now
from fbtest.core.state import RuntimeState
from fbtest.modules.base import MonitorModule
from fbtest.proc import hidden_process_kwargs

log = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"

#: Erkennt die Antwortzeit in der Ausgabe von ``ping`` - deutsch wie englisch,
#: mit ``=`` (genaue Zeit) wie mit ``<`` (unter 1 ms).
_PING_TIME_RE = re.compile(
    r"(?:time|zeit|tiempo|temps)\s*[=<]\s*(\d+(?:[.,]\d+)?)\s*ms",
    re.IGNORECASE,
)

#: Zeilen, die trotz Ausgabe einen Fehlschlag bedeuten (Windows meldet
#: "Zielhost nicht erreichbar" mit Exitcode 0!).
_PING_FAILURE_MARKERS = (
    "unreachable",
    "nicht erreichbar",
    "timed out",
    "zeitueberschreitung",
    "zeitüberschreitung",
    "100% packet loss",
    "100% loss",
)


# --------------------------------------------------------------------------
# Reine Auswertelogik (ohne Netzwerk - direkt testbar)
# --------------------------------------------------------------------------


def parse_ping_output(output: str) -> float | None:
    """Liest die Antwortzeit aus der Ausgabe des System-``ping``.

    Beruecksichtigt deutsche und englische Ausgaben sowie ``Zeit<1ms``.
    Fehlermeldungen wie "Zielhost nicht erreichbar" werden als Verlust gewertet,
    auch wenn der Prozess mit Exitcode 0 endet.

    Args:
        output: Vollstaendige Standardausgabe des Ping-Aufrufs.

    Returns:
        Antwortzeit in Millisekunden oder ``None`` bei Paketverlust.
    """
    lowered = output.lower()
    if any(marker in lowered for marker in _PING_FAILURE_MARKERS):
        return None
    match = _PING_TIME_RE.search(output)
    if not match:
        return None
    return float(match.group(1).replace(",", "."))


@dataclass(slots=True)
class WindowStats:
    """Kennzahlen eines Aggregationsfensters."""

    sent: int
    lost: int
    rtt_min: float | None
    rtt_avg: float | None
    rtt_max: float | None
    rtt_p95: float | None
    jitter_ms: float | None

    @property
    def loss_pct(self) -> float:
        """Paketverlust im Fenster in Prozent."""
        return 100.0 * self.lost / self.sent if self.sent else 0.0


def percentile(values: list[float], pct: float) -> float:
    """Berechnet ein Perzentil per naechstem Rang.

    Bewusst ohne Interpolation: Bei Latenzmessungen soll ein tatsaechlich
    gemessener Wert herauskommen, kein rechnerisches Zwischenergebnis.

    Args:
        values: Nicht leere Werteliste.
        pct: Perzentil zwischen 0 und 100.

    Returns:
        Der Wert an der entsprechenden Position.

    Raises:
        ValueError: Wenn ``values`` leer ist.
    """
    if not values:
        raise ValueError("Perzentil einer leeren Liste ist nicht definiert.")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct / 100.0 * (len(ordered) - 1))))
    return ordered[index]


def compute_jitter(rtts: list[float]) -> float | None:
    """Berechnet den Jitter als mittlere absolute Differenz aufeinanderfolgender RTTs.

    Args:
        rtts: Antwortzeiten in Reihenfolge der Messung.

    Returns:
        Jitter in Millisekunden oder ``None`` bei weniger als zwei Werten.
    """
    if len(rtts) < 2:
        return None
    deltas = [abs(b - a) for a, b in itertools.pairwise(rtts)]
    return statistics.fmean(deltas)


def aggregate_window(samples: list[float | None]) -> WindowStats | None:
    """Verdichtet die Rohmessungen eines Fensters zu Kennzahlen.

    Args:
        samples: Ein Eintrag je Ping; ``None`` steht fuer ein verlorenes Paket.

    Returns:
        Die Kennzahlen oder ``None``, wenn das Fenster leer war.
    """
    if not samples:
        return None
    rtts = [value for value in samples if value is not None]
    lost = len(samples) - len(rtts)
    if not rtts:
        return WindowStats(len(samples), lost, None, None, None, None, None)
    return WindowStats(
        sent=len(samples),
        lost=lost,
        rtt_min=min(rtts),
        rtt_avg=statistics.fmean(rtts),
        rtt_max=max(rtts),
        rtt_p95=percentile(rtts, 95),
        jitter_ms=compute_jitter(rtts),
    )


@dataclass(slots=True)
class _TargetState:
    """Fehlerzaehler eines einzelnen Ping-Ziels."""

    consecutive_failures: int = 0
    first_failure_ts: float | None = None
    first_failure_mono: float | None = None
    total_sent: int = 0
    total_lost: int = 0


class OutageTracker:
    """Erkennt und klassifiziert Ausfaelle ueber alle Ping-Ziele hinweg.

    Ein Ziel gilt als ausgefallen, sobald ``threshold`` Pings hintereinander
    fehlschlagen. Aus dem Zustand aller Ziele wird anschliessend die
    Gesamtdiagnose gebildet - siehe :class:`~fbtest.core.models.OutageScope`.

    Die Klasse enthaelt bewusst kein Netzwerk und keine Datenbank und ist damit
    vollstaendig mit synthetischen Messreihen testbar.
    """

    def __init__(
        self,
        threshold: int,
        state: RuntimeState | None = None,
    ) -> None:
        """Initialisiert den Tracker.

        Args:
            threshold: Anzahl aufeinanderfolgender Fehlschlaege bis zum Ausfall.
            state: Geteilter Laufzeitzustand fuer die WLAN-Zuordnung (optional).
        """
        self.threshold = threshold
        self._state = state
        self._targets: dict[str, _TargetState] = {}
        self._scopes: dict[str, str] = {}
        self.current: Outage | None = None
        self._started_mono: float | None = None

    def register(self, name: str, scope: str) -> None:
        """Meldet ein Ziel mit seiner Zuordnung (``gateway``/``internet``/``lan``) an."""
        self._targets[name] = _TargetState()
        self._scopes[name] = scope

    def record(self, name: str, success: bool, timestamp: float | None = None) -> None:
        """Verbucht das Ergebnis eines einzelnen Pings.

        Args:
            name: Zielname.
            success: True bei Antwort, False bei Verlust.
            timestamp: Zeitpunkt der Messung (Default: jetzt).
        """
        state = self._targets.setdefault(name, _TargetState())
        state.total_sent += 1
        if success:
            state.consecutive_failures = 0
            state.first_failure_ts = None
            state.first_failure_mono = None
        else:
            state.total_lost += 1
            state.consecutive_failures += 1
            if state.first_failure_ts is None:
                state.first_failure_ts = timestamp if timestamp is not None else utc_now()
                state.first_failure_mono = time.monotonic()

    def _is_down(self, name: str) -> bool:
        """True, wenn ein Ziel die Fehlerschwelle erreicht hat."""
        return self._targets[name].consecutive_failures >= self.threshold

    def _group_down(self, scope: str) -> bool | None:
        """True, wenn *alle* Ziele einer Gruppe ausgefallen sind.

        Returns:
            ``None``, wenn die Gruppe nicht konfiguriert ist.
        """
        names = [name for name, group in self._scopes.items() if group == scope]
        if not names:
            return None
        return all(self._is_down(name) for name in names)

    def classify(self) -> OutageScope | None:
        """Bestimmt die aktuelle Ausfallart.

        Returns:
            Die Klassifikation oder ``None``, wenn kein Ausfall vorliegt.
        """
        gateway_down = self._group_down("gateway")
        internet_down = self._group_down("internet")

        if gateway_down:
            if internet_down is False:
                # Kurios, aber real: Box antwortet nicht auf ICMP, Internet laeuft.
                return OutageScope.GATEWAY
            if self._state is not None and self._state.link.local_link_down:
                return OutageScope.WLAN
            return OutageScope.FULL if internet_down else OutageScope.GATEWAY
        if internet_down:
            return OutageScope.WAN
        return None

    def earliest_failure(self) -> tuple[float, float] | None:
        """Zeitpunkt des ersten zum Ausfall gehoerenden Fehlschlags.

        Returns:
            Tupel aus Wall-Clock-Zeitstempel und monotonem Zeitpunkt.
        """
        candidates = [
            (state.first_failure_ts, state.first_failure_mono)
            for state in self._targets.values()
            if state.first_failure_ts is not None and state.first_failure_mono is not None
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda item: item[0])

    def evaluate(self) -> tuple[str, Outage] | None:
        """Prueft nach jeder Messrunde, ob ein Ausfall beginnt oder endet.

        Returns:
            ``("start", outage)`` bzw. ``("end", outage)`` bei einem Wechsel,
            sonst ``None``. Bei laufendem Ausfall wird der Scope fortlaufend
            nachgeschaerft (z.B. wenn zusaetzlich das Gateway ausfaellt).
        """
        scope = self.classify()

        if scope is not None and self.current is None:
            earliest = self.earliest_failure()
            started_at, started_mono = earliest if earliest else (utc_now(), time.monotonic())
            self.current = Outage(started_at=started_at, scope=scope)
            self._started_mono = started_mono
            return "start", self.current

        if scope is not None and self.current is not None:
            # Ausfall dauert an - Klassifikation ggf. verschaerfen.
            if scope != self.current.scope:
                log.info(
                    "Ausfall-Klassifikation praezisiert: %s -> %s",
                    self.current.scope.value,
                    scope.value,
                )
                self.current.scope = scope
            return None

        if scope is None and self.current is not None:
            outage = self.current
            outage.ended_at = utc_now()
            # Dauer ueber die monotone Uhr - unabhaengig von Zeitumstellungen.
            if self._started_mono is not None:
                outage.duration_s = time.monotonic() - self._started_mono
            else:
                outage.duration_s = outage.ended_at - outage.started_at
            self.current = None
            self._started_mono = None
            return "end", outage

        return None

    def consecutive_failures(self, name: str) -> int:
        """Anzahl der aktuell aufeinanderfolgenden Fehlschlaege eines Ziels."""
        state = self._targets.get(name)
        return state.consecutive_failures if state else 0

    def stats(self) -> dict[str, tuple[int, int]]:
        """Liefert je Ziel die Anzahl gesendeter und verlorener Pakete."""
        return {
            name: (state.total_sent, state.total_lost) for name, state in self._targets.items()
        }


# --------------------------------------------------------------------------
# Ping-Backends
# --------------------------------------------------------------------------


class PingBackend:
    """Basisklasse eines Ping-Verfahrens."""

    name = "base"

    async def ping(self, host: str, timeout_s: float) -> float | None:
        """Sendet einen einzelnen Ping.

        Returns:
            Antwortzeit in Millisekunden oder ``None`` bei Verlust.
        """
        raise NotImplementedError


class IcmplibBackend(PingBackend):
    """Praeziser ICMP-Ping ueber ``icmplib`` (benoetigt je nach System Rechte)."""

    name = "icmplib"

    def __init__(self, privileged: bool) -> None:
        self.privileged = privileged

    async def ping(self, host: str, timeout_s: float) -> float | None:
        from icmplib import async_ping

        host_result = await async_ping(
            host, count=1, timeout=timeout_s, privileged=self.privileged
        )
        if not host_result.is_alive or not host_result.rtts:
            return None
        return float(host_result.rtts[0])


class SubprocessBackend(PingBackend):
    """Fallback ueber das System-Kommando ``ping`` - laeuft ohne Sonderrechte."""

    name = "subprocess"

    async def ping(self, host: str, timeout_s: float) -> float | None:
        if _IS_WINDOWS:
            args = ["ping", "-n", "1", "-w", str(int(timeout_s * 1000)), host]
        else:
            args = ["ping", "-c", "1", "-W", str(max(1, int(timeout_s))), host]

        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            **hidden_process_kwargs(),
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_s + 3)
        except TimeoutError:
            process.kill()
            with contextlib.suppress(ProcessLookupError):
                await process.wait()
            return None

        return parse_ping_output(stdout.decode(errors="replace"))


async def select_backend(prefer_icmplib: bool) -> PingBackend:
    """Waehlt das beste verfuegbare Ping-Verfahren.

    Reihenfolge: ``icmplib`` unprivilegiert -> ``icmplib`` privilegiert ->
    System-``ping``. Ein Wechsel wird protokolliert, damit im Bericht
    nachvollziehbar ist, womit gemessen wurde.
    """
    if prefer_icmplib:
        for privileged in (False, True):
            backend = IcmplibBackend(privileged=privileged)
            try:
                await backend.ping("127.0.0.1", 1.0)
            except Exception as exc:
                log.debug(
                    "icmplib (privileged=%s) nicht nutzbar: %s: %s",
                    privileged,
                    type(exc).__name__,
                    exc,
                )
                continue
            log.info("Ping-Verfahren: icmplib (privileged=%s).", privileged)
            return backend
        log.warning(
            "icmplib ist nicht nutzbar (fehlende Rechte fuer Raw-Sockets). "
            "Es wird auf das System-Kommando 'ping' umgeschaltet - die Latenzaufloesung "
            "ist dabei etwas grober."
        )
    else:
        log.info("Ping-Verfahren laut Konfiguration: System-Kommando 'ping'.")
    return SubprocessBackend()


# --------------------------------------------------------------------------
# Modul
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Window:
    """Sammelpuffer eines Aggregationsfensters."""

    samples: list[float | None] = field(default_factory=list)
    started_mono: float = field(default_factory=time.monotonic)


class PingMonitor(MonitorModule):
    """Misst Latenz und Paketverlust und erkennt Ausfaelle."""

    name = "ping"

    def __init__(
        self,
        bus: EventBus,
        config: PingConfig,
        state: RuntimeState,
        source: str = "master",
    ) -> None:
        super().__init__(bus, source)
        self.config = config
        self.state = state
        self.tracker = OutageTracker(config.outage_threshold, state)
        self._backend: PingBackend | None = None
        self._windows: dict[str, _Window] = {}
        self._lock = asyncio.Lock()
        #: Jeweils letzte Messung je Ziel - Grundlage der Live-Anzeige.
        self._latest: dict[str, tuple[float | None, float]] = {}

    def snapshot(self) -> dict[str, dict[str, object]]:
        """Liefert den aktuellen Messzustand je Ziel fuer die Live-Anzeige.

        Bewusst aus dem Arbeitsspeicher statt aus der Datenbank: Die Datenbank
        enthaelt nur die verdichteten Fensterwerte (Default alle 60 s), fuer eine
        Live-Anzeige waere das viel zu traege.

        Returns:
            Je Ziel ein Dictionary mit ``rtt_ms``, ``ok``, ``scope``, ``host``,
            ``consecutive_failures``, ``sent`` und ``lost``.
        """
        stats = self.tracker.stats()
        result: dict[str, dict[str, object]] = {}
        for target in self.config.targets:
            rtt, timestamp = self._latest.get(target.name, (None, 0.0))
            sent, lost = stats.get(target.name, (0, 0))

            result[target.name] = {
                "rtt_ms": round(rtt, 2) if rtt is not None else None,
                "ok": rtt is not None,
                "scope": target.scope,
                "host": target.host,
                "timestamp": timestamp,
                "consecutive_failures": self.tracker.consecutive_failures(target.name),
                "sent": sent,
                "lost": lost,
                "loss_pct": round(100.0 * lost / sent, 2) if sent else 0.0,
            }
        return result

    @property
    def backend_name(self) -> str:
        """Name des verwendeten Ping-Verfahrens."""
        return self._backend.name if self._backend else "unbekannt"

    async def run(self) -> None:
        """Startet je Ziel eine Messschleife plus die DNS-Messung."""
        self._backend = await select_backend(self.config.prefer_icmplib)
        self.emit(
            EventType.MODULE_START,
            f"Ping-Monitor gestartet (Verfahren: {self._backend.name}, "
            f"{len(self.config.targets)} Ziele, Intervall {self.config.interval_s} s).",
            Severity.INFO,
            backend=self._backend.name,
        )

        for target in self.config.targets:
            self.tracker.register(target.name, target.scope)
            self._windows[target.name] = _Window()

        try:
            async with asyncio.TaskGroup() as group:
                for target in self.config.targets:
                    group.create_task(self._target_loop(target), name=f"ping:{target.name}")
                if self.config.dns.enabled and self.config.dns.hostnames:
                    group.create_task(self._dns_loop(), name="ping:dns")
        finally:
            await self._close_open_outage()

    # -- Messschleifen -----------------------------------------------------

    async def _target_loop(self, target: PingTarget) -> None:
        """Pingt ein Ziel im konfigurierten Takt und fuellt dessen Fenster."""
        assert self._backend is not None
        next_run = time.monotonic()

        while True:
            timestamp = utc_now()
            try:
                rtt = await self._backend.ping(target.host, self.config.timeout_s)
            except Exception as exc:
                self._log.debug("Ping auf %s fehlgeschlagen: %s", target.host, exc)
                rtt = None

            self._latest[target.name] = (rtt, timestamp)

            async with self._lock:
                self._windows[target.name].samples.append(rtt)
                self.tracker.record(target.name, rtt is not None, timestamp)
                await self._handle_transition()
                await self._maybe_flush_window(target)

            if self.config.store_raw_samples and rtt is not None:
                self.measure(
                    "rtt", rtt, "ms", timestamp=timestamp, target=target.name, scope=target.scope
                )

            next_run += self.config.interval_s
            delay = next_run - time.monotonic()
            if delay < 0:
                next_run = time.monotonic()
                delay = 0
            await asyncio.sleep(delay)

    async def _maybe_flush_window(self, target: PingTarget) -> None:
        """Schliesst ein Aggregationsfenster ab, sobald es voll ist."""
        window = self._windows[target.name]
        if time.monotonic() - window.started_mono < self.config.aggregate_window_s:
            return

        stats = aggregate_window(window.samples)
        self._windows[target.name] = _Window()
        if stats is None:
            return

        meta: dict[str, Any] = {"target": target.name, "scope": target.scope,
                                "host": target.host}
        if stats.rtt_avg is not None:
            self.measure("rtt_avg", stats.rtt_avg, "ms", **meta)
            self.measure("rtt_min", stats.rtt_min or 0.0, "ms", **meta)
            self.measure("rtt_max", stats.rtt_max or 0.0, "ms", **meta)
            self.measure("rtt_p95", stats.rtt_p95 or 0.0, "ms", **meta)
        if stats.jitter_ms is not None:
            self.measure("jitter", stats.jitter_ms, "ms", **meta)
        self.measure("loss_pct", stats.loss_pct, "%", sent=stats.sent, lost=stats.lost, **meta)

    async def _dns_loop(self) -> None:
        """Misst die reine DNS-Aufloesungszeit getrennt von der Latenz.

        Die jeweils erste Aufloesung eines Namens wird gemessen, aber nicht
        gewertet. Sie faellt regelmaessig um Groessenordnungen aus dem Rahmen -
        beim Start laufen alle Module gleichzeitig an, der Lastgenerator belegt
        die Leitung bereits, und der Resolver hat den Namen noch nicht im
        Zwischenspeicher. Ein einzelner solcher Wert verschiebt den Mittelwert
        einer dreiminuetigen Messreihe um mehr als das Dreissigfache und macht
        die Kennzahl damit wertlos.

        Verworfen wird ausschliesslich der Zeitwert. Ein Fehlschlag zaehlt auch
        beim ersten Versuch: Dass ein Name gar nicht aufloesbar ist, ist ein
        Befund und kein Anlaufeffekt.
        """
        dns = self.config.dns
        warmed_up: set[str] = set()
        while True:
            for hostname in dns.hostnames:
                started = time.monotonic()
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(socket.getaddrinfo, hostname, None),
                        timeout=dns.timeout_s,
                    )
                except (TimeoutError, OSError) as exc:
                    self.emit(
                        EventType.DNS_FAILURE,
                        f"DNS-Aufloesung von {hostname} fehlgeschlagen: {exc}",
                        Severity.WARNING,
                        hostname=hostname,
                    )
                    self.measure("dns_failed", 1, "", hostname=hostname)
                    continue
                elapsed_ms = (time.monotonic() - started) * 1000.0
                if hostname not in warmed_up:
                    warmed_up.add(hostname)
                    log.debug(
                        "DNS-Warmlauf %s: %.0f ms (nicht gewertet)", hostname, elapsed_ms
                    )
                    continue
                self.measure("dns_resolve", elapsed_ms, "ms", hostname=hostname)
            await asyncio.sleep(dns.interval_s)

    # -- Ausfallbehandlung -------------------------------------------------

    async def _handle_transition(self) -> None:
        """Erzeugt Ereignisse, wenn ein Ausfall beginnt oder endet."""
        transition = self.tracker.evaluate()
        if transition is None:
            return

        kind, outage = transition
        if kind == "start":
            self.emit(
                EventType.OUTAGE_START,
                f"Ausfall erkannt ({_scope_text(outage.scope)}). "
                f"Erster Fehlschlag: {_clock(outage.started_at)}.",
                Severity.ERROR,
                scope=outage.scope.value,
                started_at=outage.started_at,
            )
        else:
            outage.cause_guess = _scope_text(outage.scope)
            outage.source = self.source
            self.bus.publish(outage)
            self.emit(
                EventType.OUTAGE_END,
                f"Ausfall beendet nach {outage.duration_s:.1f} s "
                f"({_scope_text(outage.scope)}).",
                Severity.WARNING,
                scope=outage.scope.value,
                duration_s=round(outage.duration_s or 0.0, 1),
            )
            self.measure("outage_duration", outage.duration_s or 0.0, "s", scope=outage.scope.value)

    async def _close_open_outage(self) -> None:
        """Sichert beim Beenden einen noch laufenden Ausfall in der Datenbank."""
        if self.tracker.current is None:
            return
        outage = self.tracker.current
        outage.cause_guess = "beim Beenden des Testlaufs noch offen"
        outage.source = self.source
        self.bus.publish(outage)


def _scope_text(scope: OutageScope) -> str:
    """Uebersetzt eine Ausfallart in einen deutschen Klartext fuer den Bericht."""
    return {
        OutageScope.WAN: "Internet weg, FRITZ!Box erreichbar - Problem auf der WAN-Strecke",
        OutageScope.GATEWAY: "FRITZ!Box antwortet nicht - Router- oder LAN-Problem",
        OutageScope.WLAN: "WLAN-Verbindung des Messgeraets getrennt",
        OutageScope.FULL: "weder FRITZ!Box noch Internet erreichbar",
        OutageScope.UNKNOWN: "Ursache nicht zuzuordnen",
    }[scope]


def _clock(timestamp: float) -> str:
    """Formatiert einen Zeitstempel als lokale Uhrzeit fuer Logmeldungen."""
    return time.strftime("%H:%M:%S", time.localtime(timestamp))
