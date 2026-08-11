"""Welche Module ein Testlauf starten wird - und warum eines fehlt.

Die Frage stellen zwei Stellen: der :class:`~fbtest.runner.TestRunner`, wenn er
die Module aufsetzt, und die Oberflaeche, wenn sie vor dem Start eine Vorschau
zeigt. Beantwortet wurde sie bisher zweimal - einmal in Python, einmal in
JavaScript - mit Regeln, die auseinanderliefen: Die Vorschau kannte das
Uptime-Modul gar nicht, versprach WLAN auch ohne Clientsicht und Router-Zugriff
und zaehlte Profile statt virtueller Clients.

Deshalb steht die Entscheidung hier, einmal. Der Runner startet, was diese
Planung als aktiv meldet; die Oberflaeche zeigt genau dieselbe Liste an. Ein
Auseinanderlaufen ist damit nicht mehr moeglich, sondern nur noch eine
Aenderung an einer Stelle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from fbtest.config import AppConfig

#: ``offen`` heisst: haengt am TR-064-Zugriff, der erst beim Start feststeht.
ModuleState = Literal["aktiv", "inaktiv", "offen"]


@dataclass(frozen=True, slots=True)
class ModulePlan:
    """Vorhersage fuer genau ein Modul.

    Attributes:
        name: Interner Modulname, wie er auch im Scheduler steht.
        title: Bezeichnung fuer die Oberflaeche.
        state: Ob das Modul laufen wird.
        detail: Was es tun wird, in einem Satzteil.
        reason: Warum es nicht laeuft; leer, wenn es laeuft.
        section: Abschnitt der Einstellungen, der es steuert.
    """

    name: str
    title: str
    state: ModuleState
    detail: str
    reason: str
    section: str

    @property
    def active(self) -> bool:
        """True, wenn das Modul gestartet wird."""
        return self.state == "aktiv"

    def as_dict(self) -> dict[str, str | bool]:
        """Form fuer die JSON-Schnittstelle des Dashboards."""
        return {
            "name": self.name,
            "title": self.title,
            "state": self.state,
            "detail": self.detail,
            "reason": self.reason,
            "section": self.section,
        }


def _takt(seconds: float) -> str:
    """Beschreibt einen Abstand in Worten statt in Sekunden."""
    if seconds < 2:
        return "im Sekundentakt"
    if seconds < 60:
        return f"alle {seconds:g} s"
    if seconds < 3600:
        return f"alle {seconds / 60:g} min"
    return f"alle {seconds / 3600:g} h"


def _plan_ping(config: AppConfig) -> ModulePlan:
    """Ping-Ueberwachung samt DNS-Messung."""
    ping = config.ping
    detail = f"{len(ping.targets)} Ziele {_takt(ping.interval_s)}"
    if ping.dns.enabled:
        detail += f", DNS {_takt(ping.dns.interval_s)}"
    return ModulePlan(
        name="ping",
        title="Erreichbarkeit",
        state="aktiv" if ping.enabled else "inaktiv",
        detail=detail,
        reason="" if ping.enabled else "Abgeschaltet - ohne Ping gibt es keine Ausfallerkennung.",
        section="ping",
    )


def _plan_uptime(config: AppConfig, tr064: bool | None) -> ModulePlan:
    """Laufzeit und Neustart-Erkennung ueber TR-064."""
    state: ModuleState = "offen" if tr064 is None else ("aktiv" if tr064 else "inaktiv")
    reasons = {
        "inaktiv": "Kein TR-064-Zugriff - Neustarts der FRITZ!Box werden nicht erkannt.",
        "offen": "Haengt vom TR-064-Zugriff ab; die Vorabpruefung klaert das.",
        "aktiv": "",
    }
    return ModulePlan(
        name="uptime",
        title="Router-Laufzeit",
        state=state,
        detail=f"Neustart-Erkennung, Abfrage {_takt(config.router.poll_interval_s)}",
        reason=reasons[state],
        section="router",
    )


def _plan_traffic(config: AppConfig) -> ModulePlan:
    """Kuenstlich erzeugte Last."""
    traffic = config.traffic
    active_profiles = [profile for profile in traffic.profiles if profile.enabled]
    clients = sum(profile.clients for profile in active_profiles)
    rate = sum(profile.target_rate_mbps * profile.clients for profile in active_profiles)

    detail = f"{len(active_profiles)} Profile, {clients} virtuelle Clients"
    if rate:
        # Eine Dauerlast von 1 Mbit/s sind 10,8 GB am Tag - die Zahl gehoert
        # dorthin, wo man die Rate einstellt, nicht in eine Fussnote.
        detail += f", zusammen {rate:g} Mbit/s (rund {rate * 10.8:.0f} GB/Tag)"
    elif active_profiles:
        detail += ", ungedrosselt"

    if not traffic.enabled:
        reason = "Abgeschaltet."
    elif not traffic.profiles:
        reason = "Keine Profile angelegt."
    elif not active_profiles:
        reason = "Alle Profile abgeschaltet."
    else:
        reason = ""
    return ModulePlan(
        name="traffic",
        title="Datenverkehr",
        state="aktiv" if not reason else "inaktiv",
        detail=detail,
        reason=reason,
        section="traffic",
    )


def _plan_speedtest(config: AppConfig) -> ModulePlan:
    """Periodische Bandbreitenmessung."""
    speedtest = config.speedtest
    detail = f"{_takt(speedtest.interval_s)}, {speedtest.connections} Verbindungen"
    detail += ", Download" if not speedtest.upload_url else ", Download und Upload"
    return ModulePlan(
        name="speedtest",
        title="Bandbreite",
        state="aktiv" if speedtest.enabled else "inaktiv",
        detail=detail,
        reason="" if speedtest.enabled else "Abgeschaltet.",
        section="speedtest",
    )


def _plan_wlan(config: AppConfig, tr064: bool | None) -> ModulePlan:
    """WLAN-Ueberwachung aus Router- und Clientsicht."""
    wlan = config.wlan
    views = [
        name
        for name, on in (("Routersicht", wlan.router_view), ("Clientsicht", wlan.client_view))
        if on
    ]
    detail = f"{' und '.join(views) or 'keine Sicht'} {_takt(wlan.interval_s)}"
    if wlan.band_switch_enabled:
        detail += f", Bandwechsel {_takt(wlan.band_switch_interval_s)}"

    # Die Routersicht braucht TR-064, die Clientsicht nicht. Ohne beides gaebe
    # es nichts zu messen - das Modul wird dann gar nicht erst gestartet.
    if not wlan.enabled:
        state: ModuleState = "inaktiv"
        reason = "Abgeschaltet."
    elif wlan.client_view:
        state, reason = "aktiv", ""
    elif tr064 is None:
        state, reason = "offen", "Nur Routersicht aktiv - haengt am TR-064-Zugriff."
    elif tr064:
        state, reason = "aktiv", ""
    else:
        state, reason = "inaktiv", "Clientsicht aus und kein TR-064-Zugriff."
    return ModulePlan(
        name="wlan",
        title="WLAN",
        state=state,
        detail=detail,
        reason=reason,
        section="wlan",
    )


def plan_modules(config: AppConfig, tr064_available: bool | None = None) -> list[ModulePlan]:
    """Sagt voraus, welche Module ein Testlauf mit dieser Konfiguration startet.

    Args:
        config: Die zu beurteilende Konfiguration.
        tr064_available: Ob der Router ueber TR-064 erreichbar ist. ``None``,
            solange das nicht feststeht - betroffene Module melden dann
            ``offen`` statt eine Erreichbarkeit zu behaupten, die niemand
            geprueft hat.

    Returns:
        Die Planung je Modul, in der Reihenfolge, in der sie gestartet werden.
    """
    return [
        _plan_ping(config),
        _plan_uptime(config, tr064_available),
        _plan_traffic(config),
        _plan_speedtest(config),
        _plan_wlan(config, tr064_available),
    ]
