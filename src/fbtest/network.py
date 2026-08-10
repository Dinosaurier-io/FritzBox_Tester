"""Ermittlung des Standard-Gateways.

Beim ersten Start soll der Assistent die IP-Adresse der FRITZ!Box vorschlagen,
statt ``192.168.178.1`` zu raten. Die Werksadresse stimmt zwar meistens - aber
eben nur meistens: In einem Netz hinter einem anderen Router, in einer
Ferienwohnung oder bei geaendertem Adressbereich fuehrt sie ins Leere, und der
Benutzer sucht den Fehler an der falschen Stelle.

Das Standard-Gateway ist der Router, ueber den dieser Rechner ins Internet
geht - in einem Heimnetz mit FRITZ!Box also die Box selbst. Ermittelt wird es
aus der Routing-Tabelle des Betriebssystems.

Die Auswertung ist bewusst von der Ausfuehrung getrennt: Die Parser bekommen
Text und liefern eine Adresse, ohne selbst Kommandos zu starten. Nur so lassen
sie sich mit echten Ausgaben testen, ohne dass ein bestimmtes Netz vorhanden
sein muss.
"""

from __future__ import annotations

import ipaddress
import logging
import platform
import re
import subprocess
from dataclasses import dataclass

from fbtest.proc import hidden_process_kwargs

log = logging.getLogger(__name__)

#: Windows-Routingtabelle: Zielnetz, Maske, Gateway, Schnittstelle, Metrik.
_WINDOWS_ROUTE = re.compile(
    r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\S+)\s+(\S+)\s+(\d+)\s*$", re.MULTILINE
)

#: Linux: ``default via 192.168.178.1 dev wlan0 proto dhcp metric 600``
_LINUX_ROUTE = re.compile(
    r"^default\s+via\s+(\S+)(?:\s+dev\s+(\S+))?(?:.*?\bmetric\s+(\d+))?", re.MULTILINE
)


@dataclass(frozen=True, slots=True)
class DefaultRoute:
    """Eine Standardroute aus der Routing-Tabelle des Betriebssystems."""

    gateway: str
    #: Unter Windows die IP der Schnittstelle, unter Linux ihr Geraetename.
    interface: str
    #: Kleinere Metrik gewinnt. Ohne Angabe in der Tabelle: 0.
    metric: int


def _is_usable_address(value: str) -> bool:
    """Prueft, ob eine Zeichenkette eine brauchbare Gateway-Adresse ist."""
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        return False
    # 'On-link' und 0.0.0.0 stehen in der Tabelle fuer direkt angebundene
    # Netze - dahinter steckt kein Router.
    return not (address.is_unspecified or address.is_loopback)


def parse_windows_routes(output: str) -> list[DefaultRoute]:
    """Liest alle Standardrouten aus der Ausgabe von ``route print``.

    Args:
        output: Vollstaendige Ausgabe von ``route print -4``.

    Returns:
        Die Routen, aufsteigend nach Metrik - die erste ist die benutzte.
    """
    routes = [
        DefaultRoute(gateway=gateway, interface=interface, metric=int(metric))
        for gateway, interface, metric in _WINDOWS_ROUTE.findall(output)
        if _is_usable_address(gateway)
    ]
    return sorted(routes, key=lambda route: route.metric)


def parse_linux_routes(output: str) -> list[DefaultRoute]:
    """Liest alle Standardrouten aus der Ausgabe von ``ip route``.

    Args:
        output: Ausgabe von ``ip route show default``.

    Returns:
        Die Routen, aufsteigend nach Metrik.
    """
    routes = [
        DefaultRoute(gateway=gateway, interface=device, metric=int(metric) if metric else 0)
        for gateway, device, metric in _LINUX_ROUTE.findall(output)
        if _is_usable_address(gateway)
    ]
    return sorted(routes, key=lambda route: route.metric)


def parse_windows_route(output: str) -> str | None:
    """Liest das benutzte Standard-Gateway aus ``route print``.

    Gibt es mehrere Standardrouten (WLAN und Kabel gleichzeitig), gewinnt die
    mit der niedrigsten Metrik - das ist die, die Windows tatsaechlich benutzt.

    Args:
        output: Vollstaendige Ausgabe von ``route print -4``.

    Returns:
        Die Gateway-Adresse oder ``None``.
    """
    routes = parse_windows_routes(output)
    return routes[0].gateway if routes else None


def parse_linux_route(output: str) -> str | None:
    """Liest das benutzte Standard-Gateway aus ``ip route``.

    Args:
        output: Ausgabe von ``ip route show default``.

    Returns:
        Die Gateway-Adresse oder ``None``.
    """
    routes = parse_linux_routes(output)
    return routes[0].gateway if routes else None


def _run(command: list[str]) -> str:
    """Fuehrt ein Kommando aus und liefert seine Ausgabe (leer bei Fehler)."""
    try:
        result = subprocess.run(
            command, capture_output=True, timeout=8, check=False,
            # Die deutsche Windows-Ausgabe kommt in cp850; die gesuchten Werte
            # sind zwar reine Zahlen, ein Dekodierfehler wuerde aber alles
            # verwerfen.
            encoding="cp850" if platform.system() == "Windows" else "utf-8",
            errors="replace",
            **hidden_process_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("Kommando %s nicht ausfuehrbar: %s", command[0], exc)
        return ""
    return result.stdout or ""


def detect_default_routes() -> list[DefaultRoute]:
    """Ermittelt alle Standardrouten dieses Rechners.

    Gibt es mehr als eine, sind mehrere Netzwerkverbindungen gleichzeitig
    aktiv - typischerweise Kabel und WLAN. Das Betriebssystem benutzt dann
    stillschweigend die mit der niedrigsten Metrik. Fuer eine Messung ist das
    eine Fehlerquelle mit erheblicher Wirkung: Wer glaubt, ueber WLAN zu
    messen, misst in Wahrheit ueber das Kabel, und beide Messungen sehen
    einander verdaechtig aehnlich.

    Returns:
        Die Routen, aufsteigend nach Metrik. Die erste wird tatsaechlich
        benutzt. Leer, wenn sich die Tabelle nicht lesen laesst.
    """
    if platform.system() == "Windows":
        return parse_windows_routes(_run(["route", "print", "-4"]))
    return parse_linux_routes(_run(["ip", "-4", "route", "show", "default"]))


def detect_default_gateway() -> str | None:
    """Ermittelt das Standard-Gateway dieses Rechners.

    Returns:
        Die IP-Adresse oder ``None``, wenn sie sich nicht bestimmen laesst.
        Ein Fehlschlag ist kein Ausnahmefall - der Assistent schlaegt dann
        die Werksadresse vor.
    """
    if platform.system() == "Windows":
        return parse_windows_route(_run(["route", "print", "-4"]))

    output = _run(["ip", "-4", "route", "show", "default"])
    if not output:
        # Aeltere Systeme ohne iproute2.
        output = _run(["route", "-n"])
        for line in output.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "0.0.0.0" and _is_usable_address(parts[1]):
                return parts[1]
        return None
    return parse_linux_route(output)
