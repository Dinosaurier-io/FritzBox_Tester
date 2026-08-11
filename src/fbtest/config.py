"""Konfigurationsmodelle und YAML-Laden.

Die gesamte Konfiguration wird beim Start mit *pydantic v2* validiert. Ein
fehlerhaftes ``config.yaml`` fuehrt damit sofort zu einer verstaendlichen
Fehlermeldung und nicht erst nach Stunden Laufzeit zu einem Absturz.

Geheimnisse (Router-Passwort) werden bevorzugt aus einer Umgebungsvariablen
gelesen. Im YAML steht dann nur der *Name* der Variablen.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from fbtest.secrets_store import ResolvedPassword

# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------

_DURATION_PATTERN = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*([smhd])\s*$", re.IGNORECASE)
_DURATION_FACTORS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: str | int | float) -> float:
    """Wandelt eine Dauerangabe in Sekunden um.

    Erlaubt sind reine Zahlen (= Sekunden) sowie Angaben mit Einheit,
    z.B. ``"90s"``, ``"30m"``, ``"24h"``, ``"3d"``.

    Args:
        value: Dauer als Zahl oder als String mit Einheit.

    Returns:
        Dauer in Sekunden.

    Raises:
        ValueError: Wenn das Format nicht erkannt wird.
    """
    if isinstance(value, int | float):
        return float(value)

    match = _DURATION_PATTERN.match(value)
    if not match:
        raise ValueError(
            f"Ungueltige Dauerangabe {value!r}. Erwartet z.B. '30s', '15m', '24h' oder '3d'."
        )
    amount = float(match.group(1).replace(",", "."))
    return amount * _DURATION_FACTORS[match.group(2).lower()]


def format_duration(seconds: float) -> str:
    """Formatiert Sekunden als lesbare Dauer (z.B. ``2 d 03:15:07``)."""
    seconds = max(0.0, float(seconds))
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days} d {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class _Base(BaseModel):
    """Basisklasse: verbietet unbekannte Schluessel, damit Tippfehler auffallen."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------


class RouterConfig(_Base):
    """Zugang zur FRITZ!Box ueber TR-064."""

    host: str = Field(default="192.168.178.1", description="IP oder Hostname der FRITZ!Box")
    port: int | None = Field(default=None, description="TR-064-Port, None = Standard")
    username: str = Field(default="", description="Benutzername (leer = Standardbenutzer)")
    password: str = Field(default="", description="Passwort im Klartext - besser: password_env")
    password_env: str = Field(
        default="FRITZ_PASSWORD",
        description="Name der Umgebungsvariablen, aus der das Passwort gelesen wird",
    )
    use_tls: bool = Field(default=False, description="TR-064 ueber HTTPS (Port 49443)")
    timeout_s: float = Field(default=5.0, gt=0, description="Timeout je TR-064-Aufruf")
    poll_interval_s: float = Field(
        default=10.0, gt=0, description="Abstand zwischen zwei Router-Abfragen"
    )

    def resolve_password(self) -> str:
        """Liefert das effektive Passwort.

        Rangfolge: Umgebungsvariable, dann Schluesselspeicher des Systems,
        dann der Klartext-Eintrag im YAML. Einzelheiten und Begruendung siehe
        :mod:`fbtest.secrets_store`.
        """
        return self.resolve_password_source().value

    def resolve_password_source(self) -> ResolvedPassword:
        """Wie :meth:`resolve_password`, liefert zusaetzlich die Herkunft.

        Die Oberflaeche und ``fbtest check`` zeigen dem Nutzer damit an,
        *woher* das Passwort stammt - bei drei moeglichen Quellen ist das der
        Unterschied zwischen einer brauchbaren und einer raetselhaften
        Fehlersuche.
        """
        # Ortsgebunden importiert: Der Schluesselspeicher ist eine Frage der
        # Umgebung, nicht des Konfigurationsmodells. So bleibt dieses Modul
        # frei von optionalen Abhaengigkeiten.
        from fbtest.secrets_store import resolve_password

        return resolve_password(
            host=self.host,
            username=self.username,
            password_env=self.password_env,
            plaintext=self.password,
        )


# --------------------------------------------------------------------------
# Ping-Monitoring
# --------------------------------------------------------------------------

PingScope = Literal["gateway", "internet", "lan"]


class PingTarget(_Base):
    """Ein einzelnes Ping-Ziel."""

    name: str = Field(description="Anzeigename, z.B. 'fritzbox' oder 'cloudflare'")
    host: str = Field(description="IP-Adresse oder Hostname")
    scope: PingScope = Field(
        default="internet",
        description="gateway = Router selbst, internet = ausserhalb, lan = anderes Geraet im LAN",
    )


class DnsCheckConfig(_Base):
    """Getrennte Messung der DNS-Aufloesungszeit."""

    enabled: bool = Field(default=True, description="DNS-Aufloesung getrennt vom Ping messen")
    hostnames: list[str] = Field(
        default_factory=lambda: ["www.google.com", "www.sbb.ch"],
        description="Namen, die aufgeloest werden; je Durchgang alle nacheinander",
    )
    interval_s: float = Field(default=30.0, gt=0, description="Abstand zwischen zwei Durchgaengen")
    timeout_s: float = Field(default=5.0, gt=0, description="Timeout je Namensaufloesung")


class PingConfig(_Base):
    """Konfiguration des Ping-Monitors."""

    enabled: bool = Field(
        default=True,
        description="Ping-Ueberwachung aktiv; ohne sie gibt es keine Ausfallerkennung",
    )
    interval_s: float = Field(default=1.0, gt=0, description="Abstand zwischen zwei Pings je Ziel")
    timeout_s: float = Field(default=1.0, gt=0, description="Timeout je Ping")
    aggregate_window_s: float = Field(
        default=60.0, gt=0, description="Fenstergroesse fuer min/avg/max/p95/Jitter"
    )
    outage_threshold: int = Field(
        default=3, ge=1, description="Anzahl aufeinanderfolgender Fehlschlaege bis OUTAGE_START"
    )
    prefer_icmplib: bool = Field(
        default=True,
        description="icmplib bevorzugen; ohne Rechte automatisch Fallback auf System-Ping",
    )
    store_raw_samples: bool = Field(
        default=False,
        description="Jeden einzelnen Ping speichern (sehr viele Zeilen) statt nur die Fenster",
    )
    targets: list[PingTarget] = Field(
        default_factory=list,
        description="Ueberwachte Ziele; mindestens eines, sinnvoll sind Gateway und Internet",
    )
    dns: DnsCheckConfig = Field(
        default_factory=DnsCheckConfig, description="Getrennte Messung der Namensaufloesung"
    )

    @field_validator("targets")
    @classmethod
    def _at_least_one_target(cls, value: list[PingTarget]) -> list[PingTarget]:
        if not value:
            raise ValueError("Mindestens ein Ping-Ziel muss konfiguriert sein.")
        return value


# --------------------------------------------------------------------------
# Traffic-Generator
# --------------------------------------------------------------------------


class _TrafficProfileBase(_Base):
    """Gemeinsame Felder aller Traffic-Profile."""

    name: str = Field(description="Freier Name, erscheint so in der Auswertung")
    enabled: bool = Field(default=True, description="Profil laeuft mit; false = bleibt untaetig")
    clients: int = Field(default=1, ge=1, le=64, description="Anzahl paralleler virtueller Clients")
    target_rate_mbps: float = Field(
        default=0.0,
        ge=0,
        description="Ziel-Datenrate je Client in Mbit/s; 0 = unbegrenzt (Token-Bucket)",
    )


class WebProfile(_TrafficProfileBase):
    """Simuliert Surfen: periodische HTTP-Requests, misst TTFB und Gesamtzeit."""

    type: Literal["web"] = Field(
        default="web", description="Profiltyp - bestimmt die uebrigen Felder"
    )
    urls: list[str] = Field(
        default_factory=list,
        description="Aufgerufene Seiten; geladen wird nur das HTML, keine Bilder oder Skripte",
    )
    interval_s: float = Field(default=15.0, gt=0, description="Pause zwischen zwei Surf-Runden")

    @field_validator("urls")
    @classmethod
    def _urls_not_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("Ein web-Profil benoetigt mindestens eine URL.")
        return value


class StreamingProfile(_TrafficProfileBase):
    """Simuliert einen Videostream: konstante Rate, erkennt Einbrueche (Stalls)."""

    type: Literal["streaming"] = Field(
        default="streaming", description="Profiltyp - bestimmt die uebrigen Felder"
    )
    url: str = Field(description="Testdatei, die fortlaufend gelesen wird")
    target_rate_mbps: float = Field(
        default=5.0, gt=0, description="Konstante Rate je Client - die Bitrate des 'Videos'"
    )
    stall_threshold_pct: float = Field(
        default=60.0,
        gt=0,
        le=100,
        description="Faellt die erreichte Rate unter diesen Anteil der Zielrate: STREAM_STALL",
    )
    stall_window_s: float = Field(
        default=5.0, gt=0, description="Messfenster fuer die Stall-Pruefung"
    )


class DownloadProfile(_TrafficProfileBase):
    """Dauerdownload einer grossen Testdatei.

    Der Download laeuft ohne Unterbrechung: Ist die Datei durch, beginnt sofort
    die naechste Runde. ``restart_after_mb`` bricht eine Runde schon vorher ab
    und faengt neu an - noetig bei sehr grossen Testdateien, die sonst pro
    Runde stundenlang laufen und nur einen einzigen Messwert liefern.
    """

    type: Literal["download"] = Field(
        default="download", description="Profiltyp - bestimmt die uebrigen Felder"
    )
    url: str = Field(
        description="Testdatei; wird endlos wiederholt geladen und nirgends gespeichert"
    )
    restart_after_mb: float = Field(
        default=0.0,
        ge=0,
        description=(
            "Nach dieser Datenmenge beginnt der Download von vorn; 0 = ganze Datei laden"
        ),
    )
    pause_s: float = Field(
        default=0.0,
        ge=0,
        description="Pause zwischen zwei Runden; 0 = sofort weiterladen",
    )


class UploadProfile(_TrafficProfileBase):
    """HTTP-POST von generierten Zufallsdaten."""

    type: Literal["upload"] = Field(
        default="upload", description="Profiltyp - bestimmt die uebrigen Felder"
    )
    url: str = Field(description="Endpunkt, der HTTP-POST annimmt - moeglichst im eigenen LAN")
    chunk_size_kb: int = Field(
        default=256, ge=1, le=8192, description="Groesse des wiederholt gesendeten Zufallsblocks"
    )


class IperfProfile(_TrafficProfileBase):
    """Reiner LAN-Durchsatz gegen einen iperf3-Server (ohne WAN-Einfluss)."""

    type: Literal["lan_iperf"] = Field(
        default="lan_iperf", description="Profiltyp - bestimmt die uebrigen Felder"
    )
    server: str = Field(description="IP des iperf3-Servers im LAN")
    port: int = Field(default=5201, ge=1, le=65535, description="Port des iperf3-Servers")
    duration_s: float = Field(default=10.0, gt=0, description="Dauer einer einzelnen Messung")
    interval_s: float = Field(default=300.0, gt=0, description="Abstand zwischen zwei Messungen")
    reverse: bool = Field(default=False, description="True = Download-Richtung messen")


TrafficProfile = Annotated[
    WebProfile | StreamingProfile | DownloadProfile | UploadProfile | IperfProfile,
    Field(discriminator="type"),
]


class TrafficConfig(_Base):
    """Konfiguration des Traffic-Generators."""

    enabled: bool = Field(
        default=True, description="Kuenstliche Last erzeugen; false = nur beobachten"
    )
    profiles: list[TrafficProfile] = Field(
        default_factory=list,
        description="Virtuelle Clients; jedes Profil laeuft unabhaengig mit eigener Statistik",
    )
    backoff_start_s: float = Field(default=1.0, gt=0, description="Startwert des Fehler-Backoffs")
    backoff_max_s: float = Field(default=60.0, gt=0, description="Obergrenze des Fehler-Backoffs")


# --------------------------------------------------------------------------
# Speedtest
# --------------------------------------------------------------------------


class SpeedtestConfig(_Base):
    """Periodische WAN-Bandbreitenmessung inklusive Bufferbloat-Indikator."""

    enabled: bool = Field(default=True, description="Periodische Bandbreitenmessung durchfuehren")
    interval_s: float = Field(default=1800.0, gt=0, description="Abstand zwischen zwei Messungen")
    download_url: str = Field(
        default="https://speed.hetzner.de/100MB.bin",
        description="Testdatei; ueber den ganzen Lauf dieselbe, sonst sind Werte unvergleichbar",
    )
    upload_url: str = Field(default="", description="Leer = Upload-Messung ueberspringen")
    connections: int = Field(default=4, ge=1, le=16, description="Parallele Verbindungen")
    measure_duration_s: float = Field(default=10.0, gt=0, description="Netto-Messfenster")
    warmup_s: float = Field(default=3.0, ge=0, description="Slow-Start-Phase, wird nicht gewertet")
    upload_size_mb: int = Field(
        default=20, ge=1, le=1024, description="Datenmenge je Upload-Messung"
    )
    latency_host: str = Field(default="1.1.1.1", description="Ziel fuer Latenz unter Last")
    pause_traffic: bool = Field(
        default=True, description="Traffic-Profile waehrend der Messung pausieren"
    )


# --------------------------------------------------------------------------
# WLAN
# --------------------------------------------------------------------------


class WlanBandProfile(_Base):
    """Ein WLAN-Profil des Betriebssystems, das genau einem Band zugeordnet ist."""

    band: Literal["2.4GHz", "5GHz", "6GHz"] = Field(description="Frequenzband dieses Profils")
    profile_name: str = Field(description="Windows: Profilname; Linux: SSID/Verbindungsname")
    ssid: str = Field(default="", description="Netzname, falls er vom Profilnamen abweicht")


class WlanConfig(_Base):
    """WLAN-Ueberwachung aus Router- und Clientsicht."""

    enabled: bool = Field(default=True, description="WLAN ueberwachen")
    interval_s: float = Field(default=30.0, gt=0, description="Abstand zwischen zwei WLAN-Abfragen")
    router_view: bool = Field(default=True, description="TR-064-Sicht: Bands, Clients, RSSI")
    client_view: bool = Field(default=True, description="Lokale Sicht via netsh/iw")
    interface: str = Field(default="", description="Interface-Name; leer = automatisch")
    band_switch_enabled: bool = Field(
        default=False,
        description="Zyklischer Bandwechsel - setzt getrennte SSIDs je Band voraus!",
    )
    band_switch_interval_s: float = Field(
        default=900.0, gt=0, description="Verweildauer je Band vor dem Wechsel"
    )
    profiles: list[WlanBandProfile] = Field(
        default_factory=list, description="WLAN-Profile des Betriebssystems, je Band eines"
    )

    @model_validator(mode="after")
    def _check_switch_profiles(self) -> WlanConfig:
        if self.band_switch_enabled and len(self.profiles) < 2:
            raise ValueError(
                "Fuer den Bandwechsel-Test werden mindestens zwei WLAN-Profile benoetigt "
                "(getrennte SSIDs je Band, Band-Steering in der FRITZ!Box deaktivieren)."
            )
        return self


# --------------------------------------------------------------------------
# Infrastruktur
# --------------------------------------------------------------------------


class StorageConfig(_Base):
    """Speicherorte und Schreibverhalten."""

    data_dir: Path = Field(
        default=Path("data"), description="Ort der SQLite-Datenbank; relativ zum Arbeitsverzeichnis"
    )
    export_dir: Path = Field(default=Path("exports"), description="Ziel der CSV- und XLSX-Exporte")
    report_dir: Path = Field(default=Path("reports"), description="Ziel der HTML-Berichte")
    log_dir: Path = Field(default=Path("logs"), description="Ort der rotierenden Protokolldateien")
    batch_interval_s: float = Field(
        default=5.0, gt=0, description="Abstand der Sammel-Schreibvorgaenge in die SQLite-DB"
    )
    batch_max_rows: int = Field(default=500, ge=1, description="Maximale Zeilen je Schreibvorgang")


class LoggingConfig(_Base):
    """Rotierende Logdateien."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", description="Ab welcher Dringlichkeit protokolliert wird"
    )
    max_bytes: int = Field(
        default=10 * 1024 * 1024, ge=1024, description="Groesse einer Logdatei bis zur Rotation"
    )
    backup_count: int = Field(
        default=5, ge=0, description="Anzahl aufbewahrter aelterer Logdateien"
    )


class DashboardConfig(_Base):
    """Web-Dashboard im Browser."""

    enabled: bool = Field(
        default=False, description="Dashboard beim Start eines Laufs automatisch mitstarten"
    )
    host: str = Field(
        default="127.0.0.1",
        description="Adresse des Dashboards; bewusst nur lokal, es gibt keine Anmeldung",
    )
    port: int = Field(default=8080, ge=1, le=65535, description="Port des Dashboards")


class DesktopConfig(_Base):
    """Verhalten der Desktop-Anwendung (Fenster, Tray, Meldungen)."""

    minimize_to_tray: bool = Field(
        default=True,
        description="Fenster schliessen minimiert in den Infobereich statt zu beenden",
    )
    notifications: bool = Field(
        default=True, description="Systemmeldungen bei Neustarts und laengeren Ausfaellen"
    )
    notify_outage_after_s: float = Field(
        default=60.0,
        gt=0,
        description="Ab dieser Ausfalldauer wird gemeldet - kurze Aussetzer nicht",
    )
    window_width: int = Field(default=1280, ge=800, description="Fensterbreite beim Start")
    window_height: int = Field(default=860, ge=600, description="Fensterhoehe beim Start")


class RunConfig(_Base):
    """Vorgaben fuer einen Testlauf."""

    duration_s: float = Field(default=86400.0, gt=0, description="Gesamtdauer des Testlaufs")
    name: str = Field(default="", description="Freier Name, z.B. 'FRITZ!OS 8.02 - Wohnung'")
    notes: str = Field(
        default="", description="Freie Notiz zum Lauf; erscheint im Bericht unter dem Namen"
    )
    resume_open_run: bool = Field(
        default=True, description="Offenen Testlauf beim Start fortsetzen statt neu beginnen"
    )
    time_gap_threshold_s: float = Field(
        default=120.0,
        gt=0,
        description="Groessere Luecke der monotonen Uhr gilt als Standby, nicht als Ausfall",
    )
    prevent_standby: bool = Field(
        default=True,
        description="Waehrend des Testlaufs den Energiesparmodus des Rechners unterdruecken",
    )

    @field_validator("duration_s", mode="before")
    @classmethod
    def _parse_duration(cls, value: object) -> object:
        if isinstance(value, str):
            return parse_duration(value)
        return value


# --------------------------------------------------------------------------
# Wurzelmodell
# --------------------------------------------------------------------------


class AppConfig(_Base):
    """Gesamtkonfiguration der Anwendung."""

    router: RouterConfig = Field(default_factory=RouterConfig)
    ping: PingConfig
    traffic: TrafficConfig = Field(default_factory=TrafficConfig)
    speedtest: SpeedtestConfig = Field(default_factory=SpeedtestConfig)
    wlan: WlanConfig = Field(default_factory=WlanConfig)
    run: RunConfig = Field(default_factory=RunConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    desktop: DesktopConfig = Field(default_factory=DesktopConfig)

    def snapshot_json(self) -> str:
        """Konfiguration als JSON fuer die Ablage im Testlauf-Datensatz.

        Das Passwort wird dabei entfernt - der Snapshot landet in der Datenbank
        und darf keine Geheimnisse enthalten.
        """
        data = self.model_dump(mode="json")
        data["router"]["password"] = "***"
        import json

        return json.dumps(data, ensure_ascii=False, indent=2)

    def ensure_directories(self, base: Path) -> None:
        """Legt alle konfigurierten Arbeitsverzeichnisse an (relativ zu ``base``)."""
        for path in (
            self.storage.data_dir,
            self.storage.export_dir,
            self.storage.report_dir,
            self.storage.log_dir,
        ):
            (base / path if not path.is_absolute() else path).mkdir(parents=True, exist_ok=True)

    def resolve_dir(self, path: Path, base: Path) -> Path:
        """Macht einen konfigurierten Pfad absolut (Basis = Projektordner)."""
        return path if path.is_absolute() else (base / path)


# --------------------------------------------------------------------------
# Laden
# --------------------------------------------------------------------------


class ConfigError(Exception):
    """Fehler beim Laden oder Validieren der Konfiguration."""


def load_config(path: Path) -> AppConfig:
    """Laedt und validiert eine YAML-Konfigurationsdatei.

    Args:
        path: Pfad zur ``config.yaml``.

    Returns:
        Das validierte Konfigurationsobjekt.

    Raises:
        ConfigError: Wenn die Datei fehlt, kein gueltiges YAML enthaelt oder
            die Validierung fehlschlaegt.
    """
    if not path.exists():
        raise ConfigError(
            f"Konfigurationsdatei nicht gefunden: {path}\n"
            "Tipp: 'fbtest init' legt eine kommentierte Beispielkonfiguration an."
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML-Syntaxfehler in {path}:\n{exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{path} enthaelt kein YAML-Objekt (erwartet Schluessel/Wert-Paare).")

    try:
        return AppConfig.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError und Untervalidierungen
        raise ConfigError(f"Konfiguration ungueltig ({path}):\n{exc}") from exc
