"""Gemeinsamer Laufzeitzustand, den mehrere Module lesen.

Bewusst klein gehalten: Hier steht nur, was ein Modul einem anderen zur
*Interpretation* seiner eigenen Messwerte mitteilen muss. Messdaten selbst
laufen ausschliesslich ueber den Event-Bus.

Konkretes Beispiel: Der Ping-Monitor sieht nur "Gateway antwortet nicht". Ob das
ein Router-Problem oder schlicht eine abgerissene WLAN-Verbindung des Laptops
ist, weiss allein der WLAN-Monitor - und hinterlegt es hier.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(slots=True)
class LinkState:
    """Zustand der lokalen Netzwerkanbindung des messenden Geraets."""

    #: True, wenn die Messung ueber WLAN laeuft (sonst LAN).
    is_wireless: bool = False
    #: True/False = WLAN verbunden bzw. getrennt, None = unbekannt.
    wlan_connected: bool | None = None
    wlan_ssid: str | None = None
    updated_at: float = field(default_factory=time.time)

    def update(self, *, is_wireless: bool, connected: bool | None, ssid: str | None) -> None:
        """Aktualisiert den Zustand (wird vom WLAN-Monitor aufgerufen)."""
        self.is_wireless = is_wireless
        self.wlan_connected = connected
        self.wlan_ssid = ssid
        self.updated_at = time.time()

    @property
    def local_link_down(self) -> bool:
        """True, wenn die lokale WLAN-Verbindung nachweislich getrennt ist.

        Nur dann darf ein Gateway-Ausfall dem WLAN zugeschrieben werden. Bei
        unbekanntem Zustand (``None``) wird bewusst nichts behauptet.
        """
        return self.is_wireless and self.wlan_connected is False


@dataclass(slots=True)
class RuntimeState:
    """Sammelobjekt fuer alle geteilten Zustaende eines Testlaufs."""

    link: LinkState = field(default_factory=LinkState)
    #: True, solange TR-064 nutzbar ist.
    tr064_available: bool = False
    #: Vom Uptime-Monitor gesetzt: die Box wurde soeben beim Neustart beobachtet.
    router_rebooting: bool = False
