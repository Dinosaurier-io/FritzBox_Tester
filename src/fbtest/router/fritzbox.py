"""Defensiver Wrapper um ``fritzconnection`` (TR-064).

Grundhaltung dieses Moduls: **Eine nicht erreichbare FRITZ!Box ist der Normalfall
und kein Fehler.** Genau darauf zielt der Test ja ab - waehrend eines Neustarts
oder eines Ausfalls antwortet die Box nicht. Deshalb gibt jede Methode im
Fehlerfall ``None`` bzw. einen als "nicht erreichbar" markierten Status zurueck,
statt eine Ausnahme nach oben durchzureichen.

Da ``fritzconnection`` synchron arbeitet, laufen alle Aufrufe ueber
``asyncio.to_thread`` und blockieren die Ereignisschleife nicht.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from fbtest.core.models import RouterStatus, WlanStatus, utc_now

log = logging.getLogger(__name__)

#: Hinweistext, wenn TR-064 in der Box deaktiviert ist.
TR064_HINT = (
    "TR-064 scheint deaktiviert oder die Zugangsdaten sind falsch.\n"
    "In der FRITZ!Box-Oberflaeche pruefen:\n"
    "  Heimnetz -> Netzwerk -> Netzwerkeinstellungen ->\n"
    "  'Zugriff fuer Anwendungen zulassen' aktivieren\n"
    "  sowie unter System -> FRITZ!Box-Benutzer einen Benutzer mit Rechten anlegen.\n"
    "Das Testsystem laeuft ohne TR-064 im eingeschraenkten Betrieb weiter "
    "(Ping, Traffic und Speedtest funktionieren, Router-Telemetrie fehlt)."
)

#: Zuordnung der WLAN-Dienstindizes zu Baendern (FRITZ!Box-Konvention).
WLAN_BANDS: dict[int, str] = {1: "2.4GHz", 2: "5GHz", 3: "6GHz"}


class TR064UnavailableError(Exception):
    """TR-064 ist nicht nutzbar (deaktiviert, falsche Zugangsdaten, kein Zugriff)."""


@dataclass(slots=True)
class WlanClient:
    """Ein am WLAN angemeldetes Geraet aus Sicht der FRITZ!Box."""

    mac: str
    ip: str
    name: str
    band: str
    rssi_dbm: int | None
    link_speed_mbps: float | None
    authenticated: bool


class FritzBoxClient:
    """Kapselt alle TR-064-Aufrufe gegen eine FRITZ!Box."""

    def __init__(
        self,
        host: str,
        username: str = "",
        password: str = "",
        port: int | None = None,
        use_tls: bool = False,
        timeout_s: float = 5.0,
    ) -> None:
        """Erzeugt einen Client (die Verbindung wird erst in :meth:`connect` aufgebaut)."""
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.use_tls = use_tls
        self.timeout_s = timeout_s

        self._conn: Any | None = None
        self._available = False
        self._last_error: str = ""
        self._wlan_indices: list[int] = []
        #: Merkt sich, ob die Box zuletzt erreichbar war (fuer Flankenerkennung).
        self.was_reachable: bool | None = None

    # -- Verbindung --------------------------------------------------------

    @property
    def available(self) -> bool:
        """True, wenn TR-064 grundsaetzlich nutzbar ist."""
        return self._available

    @property
    def last_error(self) -> str:
        """Letzte Fehlermeldung (leer, wenn alles in Ordnung)."""
        return self._last_error

    async def connect(self) -> bool:
        """Baut die TR-064-Verbindung auf und ermittelt die verfuegbaren Dienste.

        Returns:
            True, wenn TR-064 nutzbar ist. Bei ``False`` steht der Grund in
            :attr:`last_error`; das System laeuft dann im eingeschraenkten
            Betrieb weiter.
        """
        try:
            await asyncio.to_thread(self._connect_blocking)
        except Exception as exc:
            self._available = False
            self._last_error = f"{type(exc).__name__}: {exc}"
            log.warning("TR-064-Verbindung zu %s fehlgeschlagen: %s", self.host, self._last_error)
            return False

        self._available = True
        self._last_error = ""
        log.info(
            "TR-064-Verbindung zu %s hergestellt (WLAN-Dienste: %s).",
            self.host,
            ", ".join(WLAN_BANDS.get(i, str(i)) for i in self._wlan_indices) or "keine",
        )
        return True

    def _connect_blocking(self) -> None:
        """Erzeugt die ``FritzConnection`` (synchron, laeuft im Worker-Thread)."""
        from fritzconnection import FritzConnection

        kwargs: dict[str, Any] = {
            "address": self.host,
            "timeout": self.timeout_s,
            "use_tls": self.use_tls,
        }
        if self.port:
            kwargs["port"] = self.port
        if self.username:
            kwargs["user"] = self.username
        if self.password:
            kwargs["password"] = self.password

        conn = FritzConnection(**kwargs)
        # Ein echter Aufruf ist die einzige zuverlaessige Pruefung: das reine
        # Erzeugen des Objekts laedt nur die Geraetebeschreibung.
        conn.call_action("DeviceInfo1", "GetInfo")
        self._conn = conn
        self._wlan_indices = [
            index for index in WLAN_BANDS if f"WLANConfiguration{index}" in conn.services
        ]

    # -- Basisaufruf -------------------------------------------------------

    async def _call(self, service: str, action: str, **kwargs: Any) -> dict[str, Any] | None:
        """Fuehrt einen TR-064-Aufruf aus und faengt jeden Fehler ab.

        Returns:
            Das Antwort-Dictionary oder ``None``, wenn der Aufruf scheiterte.
        """
        if self._conn is None:
            return None
        try:
            return await asyncio.to_thread(self._conn.call_action, service, action, **kwargs)
        except Exception as exc:
            self._last_error = f"{service}:{action} -> {type(exc).__name__}: {exc}"
            log.debug("TR-064-Aufruf fehlgeschlagen: %s", self._last_error)
            return None

    # -- Einzelabfragen ----------------------------------------------------

    async def get_device_info(self) -> dict[str, Any] | None:
        """Liest Geraeteinformationen inklusive Router-Laufzeit und Firmware."""
        return await self._call("DeviceInfo1", "GetInfo")

    async def get_wan_status(self) -> dict[str, Any] | None:
        """Liest den WAN-Verbindungsstatus (Status, Uptime, letzter Fehler)."""
        result = await self._call("WANIPConn1", "GetStatusInfo")
        if result is None:
            # Aeltere bzw. anders benannte Dienstinstanz.
            result = await self._call("WANIPConnection1", "GetStatusInfo")
        return result

    async def get_link_properties(self) -> dict[str, Any] | None:
        """Liest die Sync-Raten der WAN-Leitung."""
        return await self._call("WANCommonIFC1", "GetCommonLinkProperties")

    async def get_byte_counters(self) -> tuple[int | None, int | None]:
        """Liest die uebertragenen Bytes (gesendet, empfangen).

        Bevorzugt werden die 64-Bit-Zaehler aus ``GetAddonInfos``; die klassischen
        32-Bit-Zaehler laufen bereits nach 4 GB ueber und sind fuer Langzeittests
        unbrauchbar.
        """
        addon = await self._call("WANCommonIFC1", "GetAddonInfos")
        if addon:
            sent = addon.get("NewX_AVM_DE_TotalBytesSent64")
            received = addon.get("NewX_AVM_DE_TotalBytesReceived64")
            if sent is not None and received is not None:
                return _as_int(sent), _as_int(received)

        sent_result = await self._call("WANCommonIFC1", "GetTotalBytesSent")
        recv_result = await self._call("WANCommonIFC1", "GetTotalBytesReceived")
        return (
            _as_int(sent_result.get("NewTotalBytesSent")) if sent_result else None,
            _as_int(recv_result.get("NewTotalBytesReceived")) if recv_result else None,
        )

    async def get_host_count(self) -> int | None:
        """Liest die Anzahl der im Heimnetz bekannten Geraete."""
        result = await self._call("Hosts1", "GetHostNumberOfEntries")
        return _as_int(result.get("NewHostNumberOfEntries")) if result else None

    async def get_wlan_info(self, index: int) -> dict[str, Any] | None:
        """Liest SSID, Kanal und Status eines WLAN-Bandes."""
        return await self._call(f"WLANConfiguration{index}", "GetInfo")

    async def get_wlan_client_count(self, index: int) -> int | None:
        """Liest die Anzahl der auf einem Band angemeldeten Geraete."""
        result = await self._call(f"WLANConfiguration{index}", "GetTotalAssociations")
        return _as_int(result.get("NewTotalAssociations")) if result else None

    async def get_wlan_clients(self, index: int) -> list[WlanClient]:
        """Liest alle auf einem Band angemeldeten Geraete inklusive RSSI.

        Returns:
            Liste der Clients; leer, wenn das Band aus ist oder die Box nicht antwortet.
        """
        count = await self.get_wlan_client_count(index)
        if not count:
            return []

        band = WLAN_BANDS.get(index, str(index))
        clients: list[WlanClient] = []
        for position in range(count):
            entry = await self._call(
                f"WLANConfiguration{index}",
                "GetGenericAssociatedDeviceInfo",
                NewAssociatedDeviceIndex=position,
            )
            if not entry:
                continue
            clients.append(
                WlanClient(
                    mac=str(entry.get("NewAssociatedDeviceMACAddress", "")),
                    ip=str(entry.get("NewAssociatedDeviceIPAddress", "")),
                    # Einen Klarnamen liefert dieser Aufruf nicht - dafuer waere
                    # eine zusaetzliche Hosts-Abfrage je Geraet noetig.
                    name=str(entry.get("NewAssociatedDeviceIPAddress", "")),
                    band=band,
                    rssi_dbm=_as_int(entry.get("NewX_AVM-DE_SignalStrength")),
                    link_speed_mbps=_as_float(entry.get("NewX_AVM-DE_Speed")),
                    authenticated=bool(entry.get("NewAssociatedDeviceAuthState", 0)),
                )
            )
        return clients

    # -- Zusammengesetzte Abfragen ----------------------------------------

    async def poll_status(self) -> RouterStatus:
        """Fragt den kompletten Router-Status in einem Durchgang ab.

        Returns:
            Ein :class:`RouterStatus`. Ist die Box nicht erreichbar, ist
            ``reachable`` False und alle Werte sind ``None`` - der Aufrufer
            entscheidet, was das bedeutet.
        """
        timestamp = utc_now()
        device = await self.get_device_info()
        if device is None:
            self.was_reachable = False
            return RouterStatus(reachable=False, timestamp=timestamp, last_error=self._last_error)

        wan = await self.get_wan_status()
        link = await self.get_link_properties()
        sent, received = await self.get_byte_counters()
        hosts = await self.get_host_count()

        self.was_reachable = True
        return RouterStatus(
            timestamp=timestamp,
            reachable=True,
            router_uptime_s=_as_int(device.get("NewUpTime")),
            firmware_version=_as_str(device.get("NewSoftwareVersion")),
            router_model=_as_str(device.get("NewModelName")),
            wan_uptime_s=_as_int(wan.get("NewUptime")) if wan else None,
            connection_status=_as_str(wan.get("NewConnectionStatus")) if wan else None,
            last_error=_as_str(wan.get("NewLastConnectionError")) if wan else None,
            sync_down_kbps=(
                _kbps(link.get("NewLayer1DownstreamMaxBitRate")) if link else None
            ),
            sync_up_kbps=_kbps(link.get("NewLayer1UpstreamMaxBitRate")) if link else None,
            bytes_sent=sent,
            bytes_received=received,
            host_count=hosts,
        )

    async def poll_wlan(self) -> tuple[list[WlanStatus], list[WlanClient]]:
        """Fragt alle WLAN-Baender aus Router-Sicht ab.

        Returns:
            Tupel aus Band-Statuslisten und allen angemeldeten Clients.
        """
        statuses: list[WlanStatus] = []
        all_clients: list[WlanClient] = []

        for index in self._wlan_indices:
            info = await self.get_wlan_info(index)
            if info is None:
                continue
            band = WLAN_BANDS.get(index, str(index))
            enabled = bool(_as_int(info.get("NewEnable")))
            clients = await self.get_wlan_clients(index) if enabled else []
            all_clients.extend(clients)
            statuses.append(
                WlanStatus(
                    band=band,
                    ssid=_as_str(info.get("NewSSID")),
                    channel=_as_int(info.get("NewChannel")),
                    client_count=len(clients) if enabled else 0,
                    view="router",
                )
            )
        return statuses, all_clients

    async def identify(self) -> tuple[str | None, str | None]:
        """Liest Firmware-Version und Modellbezeichnung.

        Returns:
            Tupel ``(firmware_version, router_model)``; Eintraege koennen
            ``None`` sein, wenn die Box nicht antwortet.
        """
        info = await self.get_device_info()
        if not info:
            return None, None
        return _as_str(info.get("NewSoftwareVersion")), _as_str(info.get("NewModelName"))


# --------------------------------------------------------------------------
# Konvertierung - TR-064 liefert je nach Firmware Strings oder Zahlen
# --------------------------------------------------------------------------


def _as_int(value: object) -> int | None:
    """Wandelt einen TR-064-Wert defensiv in ``int`` um."""
    if value is None or isinstance(value, bool):
        return int(value) if isinstance(value, bool) else None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_float(value: object) -> float | None:
    """Wandelt einen TR-064-Wert defensiv in ``float`` um."""
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_str(value: object) -> str | None:
    """Wandelt einen TR-064-Wert in einen bereinigten String um."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


#: Groesster Wert eines vorzeichenlosen 32-Bit-Feldes.
#:
#: An Glasfaseranschluessen kennt die Box keine Sync-Rate im Sinne von DSL. Statt
#: das Feld wegzulassen, liefert TR-064 den Maximalwert des Datentyps. Ungeprueft
#: uebernommen ergaebe das eine Leitungsgeschwindigkeit von 4.3 Tbit/s - eine
#: Zahl, die im Bericht ueberzeugend aussieht und trotzdem frei erfunden ist.
_UINT32_MAX = 2**32 - 1


def _kbps(value: object) -> int | None:
    """Rechnet eine Sync-Rate von Bit/s in kbit/s um.

    Returns:
        Die Rate in kbit/s oder ``None``, wenn die Box keine meldet.
    """
    raw = _as_int(value)
    if raw is None or raw >= _UINT32_MAX:
        return None
    return raw // 1000
