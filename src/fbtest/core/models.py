"""Datenmodelle des Testsystems.

Alle Zeitpunkte werden doppelt gefuehrt: als Unix-Timestamp (fuer Sortierung und
Rechnen) und als UTC-ISO8601-String (fuer Lesbarkeit und Export). Dauern werden
dagegen ueber ``time.monotonic()`` gemessen, damit Zeitumstellung oder
Systemschlaf die Messung nicht verfaelschen.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def utc_now() -> float:
    """Aktueller Zeitpunkt als Unix-Timestamp (UTC)."""
    return time.time()


def iso_utc(timestamp: float) -> str:
    """Wandelt einen Unix-Timestamp in einen ISO8601-String in UTC."""
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat(timespec="milliseconds")


def to_json(data: dict[str, Any] | None) -> str | None:
    """Serialisiert ein Meta-Dictionary als kompaktes JSON (oder ``None``)."""
    if not data:
        return None
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


class Severity(StrEnum):
    """Schweregrad eines Ereignisses."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class EventType(StrEnum):
    """Alle vom System erzeugten Ereignistypen.

    Die Namen sind bewusst stabil gehalten - sie erscheinen im Bericht, im
    Export und in der Datenbank und muessen zwischen Testlaeufen vergleichbar
    bleiben.
    """

    # Systemlebenszyklus
    RUN_START = "RUN_START"
    RUN_END = "RUN_END"
    RUN_RESUMED = "RUN_RESUMED"
    SYSTEM_GAP = "SYSTEM_GAP"
    MODULE_START = "MODULE_START"
    MODULE_CRASH = "MODULE_CRASH"
    MODULE_RESTART = "MODULE_RESTART"
    MODULE_STOP = "MODULE_STOP"
    POWER_KEEPALIVE = "POWER_KEEPALIVE"
    """Zustand der Standby-Sperre. Bei einer spaeteren Messluecke ist damit
    nachvollziehbar, ob der Rechner ueberhaupt schlafen gehen konnte."""
    NETWORK_PATH = "NETWORK_PATH"
    """Ueber welche Verbindung gemessen wurde. Sind Kabel und WLAN gleichzeitig
    aktiv, entscheidet das Betriebssystem - ohne diesen Eintrag laesst sich ein
    Vergleich zweier Laeufe spaeter nicht mehr auf Zulaessigkeit pruefen."""

    # Router
    ROUTER_UNREACHABLE = "ROUTER_UNREACHABLE"
    ROUTER_REACHABLE = "ROUTER_REACHABLE"
    ROUTER_REBOOT = "ROUTER_REBOOT"
    WAN_RECONNECT = "WAN_RECONNECT"
    WAN_DOWN = "WAN_DOWN"
    WAN_UP = "WAN_UP"
    TR064_DEGRADED = "TR064_DEGRADED"

    # Erreichbarkeit
    OUTAGE_START = "OUTAGE_START"
    OUTAGE_END = "OUTAGE_END"
    DNS_FAILURE = "DNS_FAILURE"

    # Traffic / Speedtest
    STREAM_STALL = "STREAM_STALL"
    STREAM_RECOVERED = "STREAM_RECOVERED"
    TRAFFIC_ERROR = "TRAFFIC_ERROR"
    SPEEDTEST_DONE = "SPEEDTEST_DONE"
    SPEEDTEST_FAILED = "SPEEDTEST_FAILED"

    # WLAN
    WLAN_CLIENT_LOST = "WLAN_CLIENT_LOST"
    WLAN_CLIENT_NEW = "WLAN_CLIENT_NEW"
    WLAN_DISCONNECT = "WLAN_DISCONNECT"
    WLAN_RECONNECT = "WLAN_RECONNECT"
    WLAN_BAND_SWITCH = "WLAN_BAND_SWITCH"


class OutageScope(StrEnum):
    """Klassifikation eines Ausfalls - die zentrale Diagnose-Trennung.

    Attributes:
        WAN: Der Router antwortet, das Internet ist nicht erreichbar - das
            Problem liegt auf der WAN-Strecke bzw. beim Provider.
        GATEWAY: Der Router selbst antwortet nicht, die lokale Verbindung des
            Messgeraets besteht aber weiterhin - Router- oder LAN-Problem.
        WLAN: Der Router antwortet nicht, *weil* die WLAN-Verbindung des
            Messgeraets getrennt ist (vom WLAN-Monitor bestaetigt).
        FULL: Weder Router noch Internet erreichbar, ohne erkennbare lokale
            Ursache.
        UNKNOWN: Zuordnung (noch) nicht moeglich.
    """

    WAN = "wan"
    GATEWAY = "gateway"
    WLAN = "wlan"
    FULL = "full"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class Measurement:
    """Ein einzelner Messpunkt.

    Bewusst generisch (``module``/``metric``/``value``/``unit``) gehalten, damit
    neue Module keine Schemaaenderung erzwingen.
    """

    module: str
    metric: str
    value: float
    unit: str = ""
    source: str = "master"
    timestamp: float = field(default_factory=utc_now)
    meta: dict[str, Any] | None = None

    @property
    def iso(self) -> str:
        """Zeitstempel als ISO8601-String in UTC."""
        return iso_utc(self.timestamp)


@dataclass(slots=True)
class Event:
    """Ein Ereignis im Testlauf (Ausfall, Neustart, Modulabsturz, ...)."""

    type: EventType
    message: str
    severity: Severity = Severity.INFO
    source: str = "master"
    timestamp: float = field(default_factory=utc_now)
    meta: dict[str, Any] | None = None

    @property
    def iso(self) -> str:
        """Zeitstempel als ISO8601-String in UTC."""
        return iso_utc(self.timestamp)


@dataclass(slots=True)
class RouterStatus:
    """Momentaufnahme der Router-Telemetrie (TR-064)."""

    router_uptime_s: int | None = None
    wan_uptime_s: int | None = None
    connection_status: str | None = None
    last_error: str | None = None
    sync_down_kbps: int | None = None
    sync_up_kbps: int | None = None
    bytes_sent: int | None = None
    bytes_received: int | None = None
    firmware_version: str | None = None
    router_model: str | None = None
    host_count: int | None = None
    timestamp: float = field(default_factory=utc_now)
    reachable: bool = True


@dataclass(slots=True)
class WlanStatus:
    """Momentaufnahme eines WLAN-Bands bzw. der lokalen WLAN-Verbindung."""

    band: str
    ssid: str | None = None
    channel: int | None = None
    client_count: int | None = None
    rssi_dbm: int | None = None
    link_speed_mbps: float | None = None
    view: str = "router"
    source: str = "master"
    timestamp: float = field(default_factory=utc_now)


@dataclass(slots=True)
class SpeedtestResult:
    """Ergebnis einer Bandbreitenmessung."""

    down_mbps: float | None = None
    up_mbps: float | None = None
    latency_idle_ms: float | None = None
    latency_loaded_ms: float | None = None
    source: str = "master"
    timestamp: float = field(default_factory=utc_now)

    @property
    def bufferbloat_ms(self) -> float | None:
        """Latenzanstieg unter Last - Indikator fuer Bufferbloat."""
        if self.latency_idle_ms is None or self.latency_loaded_ms is None:
            return None
        return self.latency_loaded_ms - self.latency_idle_ms


@dataclass(slots=True)
class Outage:
    """Ein abgeschlossener oder laufender Ausfall."""

    started_at: float
    scope: OutageScope = OutageScope.UNKNOWN
    ended_at: float | None = None
    duration_s: float | None = None
    cause_guess: str = ""
    source: str = "master"

    @property
    def is_open(self) -> bool:
        """True, solange der Ausfall noch andauert."""
        return self.ended_at is None


@dataclass(slots=True)
class TestRun:
    """Metadaten eines Testlaufs."""

    id: int
    started_at: float
    ended_at: float | None = None
    name: str = ""
    firmware_version: str | None = None
    router_model: str | None = None
    notes: str = ""

    @property
    def duration_s(self) -> float:
        """Bisherige bzw. gesamte Dauer des Testlaufs in Sekunden."""
        end = self.ended_at if self.ended_at is not None else utc_now()
        return max(0.0, end - self.started_at)
