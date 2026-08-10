"""WLAN-Ueberwachung aus zwei Blickrichtungen.

**Router-Sicht** (immer verfuegbar, solange TR-064 laeuft): Je Band Status,
Kanal, SSID und die Liste der angemeldeten Geraete samt Signalstaerke. Damit
sieht man, ob die Box selbst Clients verliert.

**Client-Sicht** (nur auf dem Geraet, das per WLAN haengt): Was sieht der Laptop?
Unter Windows ueber ``netsh wlan show interfaces``, unter Linux ueber
``iw dev <if> link``. Beide Ausgaben werden von reinen Parser-Funktionen
verarbeitet, die ohne WLAN-Hardware testbar sind.

Beide Sichten zusammen erlauben die entscheidende Unterscheidung: Reisst die
Verbindung ab, weil die *Box* den Client wegwirft, oder weil der *Client*
weglaeuft?

Bekannte Einschraenkung des Bandwechsel-Tests: Er setzt **getrennte SSIDs je
Band** voraus. Bei aktivem Band-Steering (FRITZ!Box-Standard: gleiche SSID fuer
2,4 und 5 GHz) entscheidet die Box, auf welchem Band ein Client landet - eine
gezielte Bandwahl ist dann technisch nicht moeglich. Siehe README.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import platform
import re
import time
from dataclasses import dataclass

from fbtest.config import WlanConfig
from fbtest.core.events import EventBus
from fbtest.core.models import EventType, Severity, WlanStatus
from fbtest.core.state import RuntimeState
from fbtest.modules.base import IntervalModule
from fbtest.proc import hidden_process_kwargs
from fbtest.router.fritzbox import FritzBoxClient

log = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"


@dataclass(slots=True)
class LocalWlanInfo:
    """Die lokale WLAN-Verbindung aus Sicht des Betriebssystems."""

    connected: bool
    interface: str | None = None
    ssid: str | None = None
    channel: int | None = None
    band: str | None = None
    signal_pct: int | None = None
    rssi_dbm: int | None = None
    rx_mbps: float | None = None
    tx_mbps: float | None = None
    radio_type: str | None = None


# --------------------------------------------------------------------------
# Parser - reine Funktionen, ohne WLAN-Hardware testbar
# --------------------------------------------------------------------------


def band_from_channel(channel: int | None) -> str | None:
    """Ordnet eine Kanalnummer einem Frequenzband zu.

    Achtung: Die Kanalnummern von 6 GHz ueberschneiden sich mit denen von
    2,4 GHz. Ohne Zusatzinformation (Frequenz oder Funktyp) ist eine sichere
    Trennung nicht moeglich - diese Funktion nimmt daher fuer niedrige Kanaele
    2,4 GHz an. Fuer die exakte Zuordnung wird unter Linux die Frequenz
    verwendet (:func:`band_from_frequency`).
    """
    if channel is None:
        return None
    if 1 <= channel <= 14:
        return "2.4GHz"
    if 32 <= channel <= 196:
        return "5GHz"
    return "6GHz"


def band_from_frequency(mhz: float | None) -> str | None:
    """Ordnet eine Frequenz in MHz eindeutig einem Band zu."""
    if mhz is None:
        return None
    if 2400 <= mhz < 2500:
        return "2.4GHz"
    if 4900 <= mhz < 5900:
        return "5GHz"
    if 5925 <= mhz <= 7125:
        return "6GHz"
    return None


def signal_pct_to_dbm(percent: int | None) -> int | None:
    """Rechnet die Windows-Signalqualitaet in Prozent naeherungsweise in dBm um.

    Windows liefert ueber ``netsh`` nur eine Qualitaet in Prozent. Microsoft
    bildet dabei -100 dBm auf 0 % und -50 dBm auf 100 % linear ab; die
    Umkehrung lautet ``dBm = prozent / 2 - 100``.

    Es handelt sich um eine **Naeherung**, nicht um einen echten Messwert - im
    Bericht ist der Wert entsprechend gekennzeichnet.
    """
    if percent is None:
        return None
    return round(percent / 2.0 - 100.0)


def _netsh_field(block: str, *labels: str) -> str | None:
    """Liest ein Feld aus der ``netsh``-Ausgabe (deutsch oder englisch)."""
    for label in labels:
        pattern = re.compile(
            rf"^\s*{re.escape(label)}[^:\n]*:\s*(.+?)\s*$",
            re.IGNORECASE | re.MULTILINE,
        )
        match = pattern.search(block)
        if match:
            return match.group(1).strip()
    return None


def _to_int(value: str | None) -> int | None:
    """Wandelt einen Textwert defensiv in eine ganze Zahl um."""
    if value is None:
        return None
    match = re.search(r"-?\d+", value)
    return int(match.group()) if match else None


def _to_float(value: str | None) -> float | None:
    """Wandelt einen Textwert defensiv in eine Kommazahl um (auch mit Komma)."""
    if value is None:
        return None
    match = re.search(r"-?\d+(?:[.,]\d+)?", value)
    return float(match.group().replace(",", ".")) if match else None


def parse_netsh_interfaces(output: str) -> LocalWlanInfo:
    """Wertet die Ausgabe von ``netsh wlan show interfaces`` aus.

    Unterstuetzt deutsche und englische Windows-Ausgaben.

    Args:
        output: Vollstaendige Ausgabe des Kommandos.

    Returns:
        Die ausgelesene Verbindung. ``connected`` ist ``False``, wenn kein
        Adapter verbunden ist oder keiner existiert.
    """
    if not output.strip():
        return LocalWlanInfo(connected=False)

    state = _netsh_field(output, "Status", "State")
    connected = state is not None and state.strip().lower() in {"verbunden", "connected"}
    if not connected:
        return LocalWlanInfo(
            connected=False,
            interface=_netsh_field(output, "Name"),
        )

    signal_pct = _to_int(_netsh_field(output, "Signal"))
    channel = _to_int(_netsh_field(output, "Kanal", "Channel"))

    return LocalWlanInfo(
        connected=True,
        interface=_netsh_field(output, "Name"),
        ssid=_netsh_field(output, "SSID"),
        channel=channel,
        band=band_from_channel(channel),
        signal_pct=signal_pct,
        rssi_dbm=signal_pct_to_dbm(signal_pct),
        rx_mbps=_to_float(_netsh_field(output, "Empfangsrate", "Receive rate")),
        tx_mbps=_to_float(_netsh_field(output, "Uebertragungsrate", "Übertragungsrate",
                                       "Transmit rate")),
        radio_type=_netsh_field(output, "Funktyp", "Radio type"),
    )


def parse_iw_link(output: str, interface: str | None = None) -> LocalWlanInfo:
    """Wertet die Ausgabe von ``iw dev <if> link`` aus (Linux).

    Args:
        output: Vollstaendige Ausgabe des Kommandos.
        interface: Name des Interfaces, falls bekannt.

    Returns:
        Die ausgelesene Verbindung.
    """
    if "not connected" in output.lower():
        return LocalWlanInfo(connected=False, interface=interface)

    ssid_match = re.search(r"^\s*SSID:\s*(.+?)\s*$", output, re.MULTILINE)
    if not ssid_match:
        return LocalWlanInfo(connected=False, interface=interface)

    freq_match = re.search(r"^\s*freq:\s*(\d+(?:\.\d+)?)", output, re.MULTILINE)
    signal_match = re.search(r"^\s*signal:\s*(-?\d+)\s*dBm", output, re.MULTILINE)
    rx_match = re.search(r"^\s*rx bitrate:\s*([\d.]+)\s*MBit/s", output, re.MULTILINE)
    tx_match = re.search(r"^\s*tx bitrate:\s*([\d.]+)\s*MBit/s", output, re.MULTILINE)
    if_match = re.search(r"\(on (\S+)\)", output)

    frequency = float(freq_match.group(1)) if freq_match else None
    return LocalWlanInfo(
        connected=True,
        interface=interface or (if_match.group(1) if if_match else None),
        ssid=ssid_match.group(1),
        band=band_from_frequency(frequency),
        rssi_dbm=int(signal_match.group(1)) if signal_match else None,
        rx_mbps=float(rx_match.group(1)) if rx_match else None,
        tx_mbps=float(tx_match.group(1)) if tx_match else None,
    )


# --------------------------------------------------------------------------
# Kommandoaufrufe
# --------------------------------------------------------------------------


async def read_command_output(*args: str, timeout_s: float = 15.0) -> tuple[int, str]:
    """Fuehrt ein Kommando aus und liefert Exitcode und Ausgabe.

    Returns:
        Tupel aus Rueckgabewert und kombinierter Ausgabe. Existiert das
        Kommando nicht, ist der Rueckgabewert ``127``.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **hidden_process_kwargs(),
        )
    except FileNotFoundError:
        return 127, f"Kommando nicht gefunden: {args[0]}"

    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except TimeoutError:
        process.kill()
        with contextlib.suppress(ProcessLookupError):
            await process.wait()
        return 124, "Zeitueberschreitung"

    # Windows-Konsolenausgaben sind je nach Codepage nicht UTF-8.
    encoding = "cp850" if _IS_WINDOWS else "utf-8"
    try:
        text = stdout.decode(encoding)
    except UnicodeDecodeError:
        text = stdout.decode("utf-8", errors="replace")
    return process.returncode or 0, text


async def read_local_wlan(interface: str = "") -> LocalWlanInfo:
    """Liest die aktuelle lokale WLAN-Verbindung plattformabhaengig aus."""
    if _IS_WINDOWS:
        code, output = await read_command_output("netsh", "wlan", "show", "interfaces")
        if code != 0:
            return LocalWlanInfo(connected=False)
        return parse_netsh_interfaces(output)

    target = interface or await _detect_linux_interface()
    if not target:
        return LocalWlanInfo(connected=False)
    code, output = await read_command_output("iw", "dev", target, "link")
    if code != 0:
        return LocalWlanInfo(connected=False, interface=target)
    return parse_iw_link(output, target)


async def _detect_linux_interface() -> str | None:
    """Ermittelt das erste WLAN-Interface unter Linux."""
    code, output = await read_command_output("iw", "dev")
    if code != 0:
        return None
    match = re.search(r"^\s*Interface\s+(\S+)", output, re.MULTILINE)
    return match.group(1) if match else None


# --------------------------------------------------------------------------
# Modul
# --------------------------------------------------------------------------


class WlanMonitor(IntervalModule):
    """Ueberwacht das WLAN aus Router- und Clientsicht."""

    name = "wlan"

    def __init__(
        self,
        bus: EventBus,
        config: WlanConfig,
        client: FritzBoxClient | None,
        state: RuntimeState,
        source: str = "master",
    ) -> None:
        super().__init__(bus, config.interval_s, source)
        self.config = config
        self.client = client
        self.state = state
        self._known_clients: dict[str, str] = {}
        self._local_connected: bool | None = None
        self._last_switch_mono: float = time.monotonic()
        self._profile_index = 0

    async def tick(self) -> None:
        """Ein Durchgang: beide Sichten abfragen, Aenderungen melden."""
        if self.config.router_view and self.client is not None and self.client.available:
            await self._poll_router_view()
        if self.config.client_view:
            await self._poll_client_view()
        if self.config.band_switch_enabled:
            await self._maybe_switch_band()

    # -- Router-Sicht ------------------------------------------------------

    async def _poll_router_view(self) -> None:
        """Fragt Baender und angemeldete Geraete ueber TR-064 ab."""
        assert self.client is not None
        statuses, clients = await self.client.poll_wlan()

        for status in statuses:
            status.source = self.source
            self.bus.publish(status)
            if status.client_count is not None:
                self.measure(
                    "clients", status.client_count, "", band=status.band, ssid=status.ssid or ""
                )

        current = {c.mac: c.band for c in clients if c.mac}
        for mac, band in current.items():
            if mac not in self._known_clients:
                self.emit(
                    EventType.WLAN_CLIENT_NEW,
                    f"Neues WLAN-Geraet auf {band}: {mac}",
                    Severity.DEBUG,
                    mac=mac,
                    band=band,
                )
        for mac, band in self._known_clients.items():
            if mac not in current:
                self.emit(
                    EventType.WLAN_CLIENT_LOST,
                    f"WLAN-Geraet ist verschwunden ({band}): {mac}",
                    Severity.WARNING,
                    mac=mac,
                    band=band,
                )
        self._known_clients = current

        for wlan_client in clients:
            if wlan_client.rssi_dbm is not None:
                self.measure(
                    "client_rssi",
                    float(wlan_client.rssi_dbm),
                    "dBm",
                    mac=wlan_client.mac,
                    band=wlan_client.band,
                )
            if wlan_client.link_speed_mbps is not None:
                self.measure(
                    "client_link_speed",
                    wlan_client.link_speed_mbps,
                    "Mbit/s",
                    mac=wlan_client.mac,
                    band=wlan_client.band,
                )

    # -- Client-Sicht ------------------------------------------------------

    async def _poll_client_view(self) -> None:
        """Liest die lokale WLAN-Verbindung und meldet Abrisse."""
        info = await read_local_wlan(self.config.interface)

        # Der Ping-Monitor braucht diese Information, um einen Gateway-Ausfall
        # korrekt als WLAN-Problem statt als Router-Problem einzustufen.
        self.state.link.update(
            is_wireless=info.connected or self._local_connected is True,
            connected=info.connected,
            ssid=info.ssid,
        )

        if self._local_connected is not None and info.connected != self._local_connected:
            if info.connected:
                self.emit(
                    EventType.WLAN_RECONNECT,
                    f"WLAN wieder verbunden mit '{info.ssid}' ({info.band or 'Band unbekannt'}).",
                    Severity.WARNING,
                    ssid=info.ssid or "",
                    band=info.band or "",
                )
            else:
                self.emit(
                    EventType.WLAN_DISCONNECT,
                    "WLAN-Verbindung des Messgeraets getrennt.",
                    Severity.ERROR,
                )
        self._local_connected = info.connected

        if not info.connected:
            return

        self.bus.publish(
            WlanStatus(
                band=info.band or "unbekannt",
                ssid=info.ssid,
                channel=info.channel,
                rssi_dbm=info.rssi_dbm,
                link_speed_mbps=info.rx_mbps,
                view="client",
                source=self.source,
            )
        )
        if info.rssi_dbm is not None:
            self.measure(
                "local_rssi",
                float(info.rssi_dbm),
                "dBm",
                band=info.band or "",
                ssid=info.ssid or "",
                approximated=_IS_WINDOWS,
            )
        if info.rx_mbps is not None:
            self.measure("local_rx_rate", info.rx_mbps, "Mbit/s", band=info.band or "")
        if info.tx_mbps is not None:
            self.measure("local_tx_rate", info.tx_mbps, "Mbit/s", band=info.band or "")

    # -- Bandwechsel -------------------------------------------------------

    async def _maybe_switch_band(self) -> None:
        """Wechselt zyklisch das WLAN-Band und misst die Verbindungsaufbauzeit."""
        if time.monotonic() - self._last_switch_mono < self.config.band_switch_interval_s:
            return
        self._last_switch_mono = time.monotonic()

        self._profile_index = (self._profile_index + 1) % len(self.config.profiles)
        profile = self.config.profiles[self._profile_index]

        started = time.monotonic()
        if _IS_WINDOWS:
            args = ["netsh", "wlan", "connect", f"name={profile.profile_name}"]
            if self.config.interface:
                args.append(f"interface={self.config.interface}")
        else:
            args = ["nmcli", "connection", "up", profile.profile_name]

        code, output = await read_command_output(*args, timeout_s=30.0)
        if code != 0:
            self.emit(
                EventType.WLAN_BAND_SWITCH,
                f"Wechsel auf Profil '{profile.profile_name}' ({profile.band}) fehlgeschlagen: "
                f"{output.strip()[:200]}",
                Severity.ERROR,
                band=profile.band,
                profile=profile.profile_name,
            )
            return

        # Auf die tatsaechliche Verbindung warten (nicht nur auf den Kommandoerfolg).
        connect_time_s: float | None = None
        for _ in range(30):
            await asyncio.sleep(1.0)
            info = await read_local_wlan(self.config.interface)
            if info.connected and (not profile.ssid or info.ssid == profile.ssid):
                connect_time_s = time.monotonic() - started
                break

        if connect_time_s is None:
            self.emit(
                EventType.WLAN_BAND_SWITCH,
                f"Nach dem Wechsel auf '{profile.profile_name}' ({profile.band}) kam innerhalb "
                "von 30 s keine Verbindung zustande.",
                Severity.ERROR,
                band=profile.band,
                profile=profile.profile_name,
            )
            return

        self.emit(
            EventType.WLAN_BAND_SWITCH,
            f"Auf Profil '{profile.profile_name}' ({profile.band}) gewechselt - "
            f"Verbindung nach {connect_time_s:.1f} s.",
            Severity.INFO,
            band=profile.band,
            profile=profile.profile_name,
            connect_time_s=round(connect_time_s, 2),
        )
        self.measure("band_switch_time", connect_time_s, "s", band=profile.band)
