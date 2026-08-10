"""Vorabpruefung (``fbtest check``).

Vor einem mehrtaegigen Lauf soll in wenigen Sekunden klar sein, ob alles passt.
Jede Pruefung liefert eine von drei Bewertungen:

* ``ok``    - alles in Ordnung
* ``warn``  - der Testlauf funktioniert, aber eingeschraenkt
* ``fail``  - so ist ein sinnvoller Testlauf nicht moeglich

Jede Meldung nennt bei Problemen konkret, was zu tun ist.
"""

from __future__ import annotations

import asyncio
import platform
import shutil
import socket
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import httpx

from fbtest.config import (
    AppConfig,
    DownloadProfile,
    IperfProfile,
    StreamingProfile,
    UploadProfile,
    WebProfile,
)
from fbtest.modules.ping_monitor import select_backend
from fbtest.modules.wlan_monitor import read_command_output, read_local_wlan
from fbtest.network import detect_default_routes
from fbtest.router.fritzbox import TR064_HINT, FritzBoxClient

_IS_WINDOWS = platform.system() == "Windows"


class Status(StrEnum):
    """Bewertung einer einzelnen Pruefung."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(slots=True)
class CheckResult:
    """Ergebnis einer einzelnen Pruefung."""

    name: str
    status: Status
    message: str
    hint: str = ""


class Checker:
    """Fuehrt alle Vorabpruefungen aus."""

    def __init__(self, config: AppConfig, base_dir: Path, db_path: Path) -> None:
        self.config = config
        self.base_dir = base_dir
        self.db_path = db_path
        self.results: list[CheckResult] = []

    def _add(self, name: str, status: Status, message: str, hint: str = "") -> CheckResult:
        result = CheckResult(name, status, message, hint)
        self.results.append(result)
        return result

    async def run_all(self) -> list[CheckResult]:
        """Fuehrt alle Pruefungen der Reihe nach aus."""
        self._check_directories()
        await self._check_ping_backend()
        await self._check_network_path()
        await self._check_router_reachable()
        await self._check_tr064()
        await self._check_ping_targets()
        await self._check_dns()
        await self._check_test_servers()
        await self._check_wlan()
        self._check_iperf()
        self._check_power_settings()
        return self.results

    @property
    def worst(self) -> Status:
        """Schlechteste Bewertung ueber alle Pruefungen."""
        if any(result.status is Status.FAIL for result in self.results):
            return Status.FAIL
        if any(result.status is Status.WARN for result in self.results):
            return Status.WARN
        return Status.OK

    # -- Einzelpruefungen --------------------------------------------------

    def _check_directories(self) -> None:
        """Prueft Schreibrechte und freien Speicherplatz."""
        try:
            self.config.ensure_directories(self.base_dir)
            probe = self.db_path.parent / ".fbtest_write_test"
            probe.parent.mkdir(parents=True, exist_ok=True)
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            self._add(
                "Arbeitsverzeichnisse",
                Status.FAIL,
                f"Kein Schreibzugriff: {exc}",
                "Projektordner an einen Ort mit Schreibrechten legen.",
            )
            return

        free_gb = shutil.disk_usage(self.base_dir).free / 1e9
        # Grobe Abschaetzung: aggregierte Messwerte brauchen wenige MB pro Tag.
        status = Status.OK if free_gb >= 1.0 else Status.WARN
        self._add(
            "Arbeitsverzeichnisse",
            status,
            f"Schreibzugriff vorhanden, {free_gb:.1f} GB frei "
            f"(Datenbank: {self.db_path}).",
            "Fuer mehrtaegige Laeufe mindestens 1 GB frei halten." if status is Status.WARN else "",
        )

    async def _check_ping_backend(self) -> None:
        """Ermittelt, welches Ping-Verfahren zur Verfuegung steht."""
        backend = await select_backend(self.config.ping.prefer_icmplib)
        if backend.name == "icmplib":
            self._add("Ping-Verfahren", Status.OK, "icmplib nutzbar (praezise Latenzmessung).")
        else:
            self._add(
                "Ping-Verfahren",
                Status.WARN,
                "icmplib nicht nutzbar, es wird das System-Kommando 'ping' verwendet.",
                "Fuer praezisere Messungen das Programm als Administrator bzw. mit "
                "'sudo setcap cap_net_raw+ep $(which python3)' starten. Der Testlauf "
                "funktioniert auch ohne.",
            )

    async def _check_network_path(self) -> None:
        """Meldet, ueber welche Verbindung gemessen wird.

        Sind Kabel und WLAN gleichzeitig verbunden, benutzt das Betriebssystem
        stillschweigend die Route mit der niedrigsten Metrik - in aller Regel
        das Kabel. Wer meint, gerade das WLAN zu vermessen, misst dann in
        Wirklichkeit den Kabelanschluss, und der anschliessende Vergleich
        zweier Laeufe zeigt keinen Unterschied, weil es keinen gibt.

        Bewusst nur eine Warnung: Zwei aktive Verbindungen sind erlaubt und
        manchmal gewollt. Der Benutzer muss es nur wissen.
        """
        routes = await asyncio.to_thread(detect_default_routes)
        if not routes:
            self._add(
                "Netzwerkweg",
                Status.WARN,
                "Die Routing-Tabelle liess sich nicht lesen.",
                "Die Messung laeuft trotzdem - es ist nur unklar, ueber welche "
                "Verbindung.",
            )
            return

        used = routes[0]
        if len(routes) == 1:
            self._add(
                "Netzwerkweg",
                Status.OK,
                f"Eine aktive Verbindung: ueber {used.interface} zu {used.gateway}.",
            )
            return

        others = ", ".join(f"{route.interface} (Metrik {route.metric})" for route in routes[1:])
        self._add(
            "Netzwerkweg",
            Status.WARN,
            f"{len(routes)} aktive Verbindungen. Gemessen wird ueber "
            f"{used.interface} (Metrik {used.metric}), nicht benutzt: {others}.",
            "Fuer einen eindeutigen Vergleich WLAN gegen Kabel die jeweils "
            "andere Verbindung abschalten - sonst messen beide Laeufe dasselbe.",
        )

    async def _check_router_reachable(self) -> None:
        """Prueft, ob die FRITZ!Box ueberhaupt antwortet."""
        host = self.config.router.host
        backend = await select_backend(self.config.ping.prefer_icmplib)
        rtt = None
        for _ in range(3):
            rtt = await backend.ping(host, 2.0)
            if rtt is not None:
                break
        if rtt is None:
            self._add(
                "FRITZ!Box erreichbar",
                Status.FAIL,
                f"{host} antwortet nicht auf Ping.",
                "IP-Adresse in der Konfiguration pruefen (Standard 192.168.178.1) "
                "und sicherstellen, dass der Rechner im richtigen Netz haengt.",
            )
        else:
            self._add(
                "FRITZ!Box erreichbar", Status.OK, f"{host} antwortet in {rtt:.1f} ms."
            )

    async def _check_tr064(self) -> None:
        """Prueft TR-064-Zugang und Zugangsdaten."""
        router = self.config.router
        resolved = router.resolve_password_source()
        if not resolved:
            self._add(
                "Router-Zugangsdaten",
                Status.WARN,
                "Kein Passwort hinterlegt.",
                "In der Anwendung unter Einstellungen setzen (es landet im "
                "Schluesselspeicher des Systems), oder auf der Kommandozeile "
                f"ueber die Umgebungsvariable {router.password_env}.",
            )
        else:
            self._add(
                "Router-Zugangsdaten",
                Status.OK,
                f"Passwort vorhanden (Quelle: {resolved.source.describe()}).",
            )

        client = FritzBoxClient(
            host=router.host,
            username=router.username,
            password=resolved.value,
            port=router.port,
            use_tls=router.use_tls,
            timeout_s=router.timeout_s,
        )
        if not await client.connect():
            self._add(
                "TR-064",
                Status.WARN,
                f"Kein TR-064-Zugriff: {client.last_error}",
                TR064_HINT,
            )
            return

        firmware, model = await client.identify()
        status = await client.poll_status()
        self._add(
            "TR-064",
            Status.OK,
            f"Verbunden mit {model or 'FRITZ!Box'}, Firmware {firmware or 'unbekannt'}, "
            f"Laufzeit {(status.router_uptime_s or 0) // 3600} h, "
            f"WAN-Status '{status.connection_status or 'unbekannt'}'.",
        )

        statuses, clients = await client.poll_wlan()
        if statuses:
            detail = ", ".join(
                f"{s.band}: '{s.ssid}' (Kanal {s.channel}, {s.client_count} Geraete)"
                for s in statuses
            )
            same_ssid = len({s.ssid for s in statuses if s.ssid}) < len(
                [s for s in statuses if s.ssid]
            )
            self._add(
                "WLAN (Router-Sicht)",
                Status.WARN if same_ssid and self.config.wlan.band_switch_enabled else Status.OK,
                f"{len(statuses)} Baender, {len(clients)} angemeldete Geraete. {detail}",
                "Mehrere Baender nutzen dieselbe SSID (Band-Steering). Ein gezielter "
                "Bandwechsel-Test ist damit nicht moeglich - siehe README."
                if same_ssid and self.config.wlan.band_switch_enabled
                else "",
            )

    async def _check_ping_targets(self) -> None:
        """Prueft alle konfigurierten Ping-Ziele."""
        backend = await select_backend(self.config.ping.prefer_icmplib)
        failed: list[str] = []
        details: list[str] = []
        for target in self.config.ping.targets:
            rtt = await backend.ping(target.host, 2.0)
            if rtt is None:
                failed.append(f"{target.name} ({target.host})")
            else:
                details.append(f"{target.name} {rtt:.0f} ms")

        if not failed:
            self._add("Ping-Ziele", Status.OK, ", ".join(details))
        elif len(failed) == len(self.config.ping.targets):
            self._add(
                "Ping-Ziele",
                Status.FAIL,
                f"Kein einziges Ziel antwortet: {', '.join(failed)}.",
                "Ohne erreichbare Ziele ist keine Ausfallerkennung moeglich.",
            )
        else:
            self._add(
                "Ping-Ziele",
                Status.WARN,
                f"Erreichbar: {', '.join(details) or 'keine'}. Ohne Antwort: {', '.join(failed)}.",
                "Manche Hosts blockieren ICMP grundsaetzlich - solche Ziele besser ersetzen, "
                "sonst werden dauerhaft Ausfaelle gemeldet.",
            )

    async def _check_dns(self) -> None:
        """Prueft die DNS-Aufloesung."""
        if not self.config.ping.dns.enabled or not self.config.ping.dns.hostnames:
            return
        hostname = self.config.ping.dns.hostnames[0]
        started = time.monotonic()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(socket.getaddrinfo, hostname, None), timeout=5.0
            )
        except (TimeoutError, OSError) as exc:
            self._add(
                "DNS",
                Status.FAIL,
                f"{hostname} kann nicht aufgeloest werden: {exc}",
                "DNS-Einstellungen bzw. Internetverbindung pruefen.",
            )
            return
        elapsed_ms = (time.monotonic() - started) * 1000
        self._add("DNS", Status.OK, f"{hostname} in {elapsed_ms:.0f} ms aufgeloest.")

    async def _check_test_servers(self) -> None:
        """Prueft die Erreichbarkeit aller konfigurierten Testserver."""
        urls: list[tuple[str, str]] = []
        for profile in self.config.traffic.profiles:
            if not profile.enabled:
                continue
            match profile:
                case WebProfile():
                    urls.extend((profile.name, url) for url in profile.urls)
                case StreamingProfile() | DownloadProfile() | UploadProfile():
                    urls.append((profile.name, profile.url))
                case IperfProfile():
                    pass
        if self.config.speedtest.enabled:
            urls.append(("speedtest-down", self.config.speedtest.download_url))
            if self.config.speedtest.upload_url:
                urls.append(("speedtest-up", self.config.speedtest.upload_url))

        if not urls:
            self._add("Testserver", Status.WARN, "Keine Testserver konfiguriert.")
            return

        problems: list[str] = []
        ok_count = 0
        # Ohne erkennbaren User-Agent lehnen manche Server pauschal mit 403 ab -
        # das waere ein Fehlalarm und kein echtes Erreichbarkeitsproblem.
        async with httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=True,
            headers={"User-Agent": "fbtest/0.1 (FRITZ!Box-Langzeittest)"},
        ) as client:
            for name, url in urls:
                try:
                    response = await client.head(url)
                    if response.status_code >= 400:
                        # Manche Server verweigern HEAD, erlauben aber GET.
                        response = await client.get(url, headers={"Range": "bytes=0-1023"})
                    if response.status_code >= 400:
                        problems.append(f"{name}: HTTP {response.status_code} ({url})")
                    else:
                        ok_count += 1
                except httpx.HTTPError as exc:
                    problems.append(f"{name}: {type(exc).__name__} ({url})")

        if not problems:
            self._add("Testserver", Status.OK, f"Alle {ok_count} Endpunkte erreichbar.")
        else:
            self._add(
                "Testserver",
                Status.FAIL if ok_count == 0 else Status.WARN,
                f"{ok_count} von {len(urls)} erreichbar. Probleme: " + "; ".join(problems),
                "Nur oeffentliche Endpunkte verwenden, die fuer Bandbreitentests vorgesehen "
                "sind (z.B. speed.hetzner.de, proof.ovh.net) oder einen eigenen Server im LAN.",
            )

    async def _check_wlan(self) -> None:
        """Prueft die lokale WLAN-Sicht und die konfigurierten Profile."""
        if not self.config.wlan.enabled or not self.config.wlan.client_view:
            return

        info = await read_local_wlan(self.config.wlan.interface)
        if info.connected:
            self._add(
                "WLAN (Client-Sicht)",
                Status.OK,
                f"Verbunden mit '{info.ssid}' ({info.band or 'Band unbekannt'}, "
                f"{info.rssi_dbm} dBm, {info.rx_mbps} Mbit/s).",
            )
        else:
            self._add(
                "WLAN (Client-Sicht)",
                Status.WARN,
                "Dieses Geraet haengt nicht per WLAN am Netz (oder es wurde kein "
                "WLAN-Adapter gefunden).",
                "Die Client-Sicht bleibt leer. Bei LAN-Betrieb ist das normal; fuer "
                "WLAN-Messungen den Laptop per WLAN verbinden oder einen Agenten nutzen.",
            )

        if not self.config.wlan.band_switch_enabled:
            return

        if _IS_WINDOWS:
            code, output = await read_command_output("netsh", "wlan", "show", "profiles")
        else:
            code, output = await read_command_output(
                "nmcli", "-t", "-f", "NAME", "connection", "show"
            )

        missing = [
            profile.profile_name
            for profile in self.config.wlan.profiles
            if code != 0 or profile.profile_name not in output
        ]
        if missing:
            self._add(
                "WLAN-Profile",
                Status.FAIL,
                f"Nicht gefunden: {', '.join(missing)}",
                "Die Profile muessen im Betriebssystem existieren (einmal manuell mit "
                "jeder Band-SSID verbinden). Voraussetzung: getrennte SSIDs je Band.",
            )
        else:
            self._add(
                "WLAN-Profile",
                Status.OK,
                f"{len(self.config.wlan.profiles)} Profile vorhanden.",
            )

    def _check_iperf(self) -> None:
        """Prueft, ob ``iperf3`` verfuegbar ist - nur wenn konfiguriert."""
        needs_iperf = any(
            isinstance(profile, IperfProfile) and profile.enabled
            for profile in self.config.traffic.profiles
        )
        if not needs_iperf:
            return
        if shutil.which("iperf3"):
            self._add("iperf3", Status.OK, "Programm gefunden.")
        else:
            self._add(
                "iperf3",
                Status.FAIL,
                "Ein lan_iperf-Profil ist aktiv, 'iperf3' wurde aber nicht gefunden.",
                "iperf3 installieren und in den PATH aufnehmen oder das Profil deaktivieren.",
            )

    def _check_power_settings(self) -> None:
        """Weist bei langen Laeufen auf die Energieeinstellungen hin."""
        if self.config.run.duration_s < 3 * 3600:
            return
        self._add(
            "Energieeinstellungen",
            Status.WARN,
            f"Geplante Laufzeit: {self.config.run.duration_s / 3600:.1f} h.",
            "Standby und Energiesparmodus deaktivieren, sonst entstehen Messluecken. "
            "Windows: Systemsteuerung -> Energieoptionen -> 'Energie sparen: Nie'. "
            "Das Tool erkennt solche Luecken zwar und markiert sie als SYSTEM_GAP, "
            "aber gemessen wird in dieser Zeit nichts.",
        )
