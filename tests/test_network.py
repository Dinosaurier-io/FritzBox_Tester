"""Tests der Gateway-Ermittlung.

Die Vorlagen stammen aus echten Ausgaben - unter anderem von einem Rechner mit
mehreren virtuellen Netzwerkkarten, weil genau dort die Auswahl schwierig wird:
Es gibt dann mehrere Standardrouten, und nur eine davon fuehrt zur FRITZ!Box.
"""

from __future__ import annotations

from fbtest.network import (
    DefaultRoute,
    parse_linux_route,
    parse_linux_routes,
    parse_windows_route,
    parse_windows_routes,
)

# Echte Ausgabe von 'route print -4' (deutsches Windows 11, mit Hyper-V- und
# VirtualBox-Adaptern).
WINDOWS_ROUTE = """\
===========================================================================
Schnittstellenliste
 12...xx xx xx xx xx xx ......Intel(R) Wi-Fi 6 AX201
===========================================================================

IPv4-Routentabelle
===========================================================================
Aktive Routen:
     Netzwerkziel    Netzwerkmaske          Gateway    Schnittstelle Metrik
          0.0.0.0          0.0.0.0    192.168.178.1   192.168.178.24     30
        127.0.0.0        255.0.0.0   Auf Verbindung         127.0.0.1    331
      192.168.6.0    255.255.255.0   Auf Verbindung       192.168.6.1    291
    192.168.178.0    255.255.255.0   Auf Verbindung    192.168.178.24    286
        224.0.0.0        240.0.0.0   Auf Verbindung         127.0.0.1    331
===========================================================================
"""

# Zwei Standardrouten: Kabel (Metrik 25) und WLAN (Metrik 45).
WINDOWS_TWO_ROUTES = """\
Aktive Routen:
     Netzwerkziel    Netzwerkmaske          Gateway    Schnittstelle Metrik
          0.0.0.0          0.0.0.0     192.168.1.254      192.168.1.5     45
          0.0.0.0          0.0.0.0      10.10.10.254      10.10.10.20     25
"""

LINUX_ROUTE = """\
default via 192.168.178.1 dev wlp3s0 proto dhcp src 192.168.178.24 metric 600
"""

LINUX_MULTIPLE = """\
default via 10.0.0.1 dev eth0 proto dhcp metric 100
default via 192.168.178.1 dev wlan0 proto dhcp metric 600
"""


class TestWindows:
    """Auswertung der Windows-Routingtabelle."""

    def test_finds_gateway(self) -> None:
        assert parse_windows_route(WINDOWS_ROUTE) == "192.168.178.1"

    def test_lowest_metric_wins(self) -> None:
        """Windows benutzt die Route mit der niedrigsten Metrik - wir auch."""
        assert parse_windows_route(WINDOWS_TWO_ROUTES) == "10.10.10.254"

    def test_ignores_on_link_entries(self) -> None:
        """'Auf Verbindung' bedeutet: direkt angebunden, kein Router dahinter."""
        assert parse_windows_route(
            "          0.0.0.0          0.0.0.0   Auf Verbindung      10.0.0.5     10\n"
        ) is None

    def test_ignores_unspecified_gateway(self) -> None:
        assert parse_windows_route(
            "          0.0.0.0          0.0.0.0          0.0.0.0      10.0.0.5     10\n"
        ) is None

    def test_empty_output(self) -> None:
        assert parse_windows_route("") is None

    def test_unrelated_output(self) -> None:
        """Ein fehlgeschlagenes Kommando darf keine Adresse erfinden."""
        assert parse_windows_route("Der Befehl wurde nicht gefunden.") is None


class TestLinux:
    """Auswertung von ``ip route``."""

    def test_finds_gateway(self) -> None:
        assert parse_linux_route(LINUX_ROUTE) == "192.168.178.1"

    def test_first_entry_wins(self) -> None:
        """``ip route`` sortiert bereits nach Metrik."""
        assert parse_linux_route(LINUX_MULTIPLE) == "10.0.0.1"

    def test_ignores_routes_without_gateway(self) -> None:
        assert parse_linux_route("default dev tun0 scope link\n") is None

    def test_empty_output(self) -> None:
        assert parse_linux_route("") is None


class TestAllRoutes:
    """Vollstaendige Liste statt nur der benutzten Route.

    Die uebrigen Routen sind kein Beiwerk: Sind Kabel und WLAN gleichzeitig
    verbunden, misst man ueber die eine und glaubt, die andere zu pruefen.
    """

    def test_single_route_is_reported_with_interface_and_metric(self) -> None:
        routes = parse_windows_routes(WINDOWS_ROUTE)
        assert len(routes) == 1
        assert routes[0] == DefaultRoute(
            gateway="192.168.178.1", interface="192.168.178.24", metric=30
        )

    def test_routes_are_sorted_by_metric(self) -> None:
        """Die erste Route ist die, die Windows tatsaechlich benutzt."""
        routes = parse_windows_routes(WINDOWS_TWO_ROUTES)
        assert [route.metric for route in routes] == [25, 45]
        assert routes[0].gateway == "10.10.10.254"

    def test_linux_routes_include_device_and_metric(self) -> None:
        routes = parse_linux_routes(LINUX_MULTIPLE)
        assert [(route.interface, route.metric) for route in routes] == [
            ("eth0", 100),
            ("wlan0", 600),
        ]

    def test_linux_route_without_metric_defaults_to_zero(self) -> None:
        routes = parse_linux_routes("default via 192.168.0.1 dev eth0\n")
        assert routes == [DefaultRoute(gateway="192.168.0.1", interface="eth0", metric=0)]

    def test_unreadable_table_yields_no_routes(self) -> None:
        assert parse_windows_routes("") == []
        assert parse_linux_routes("Der Befehl wurde nicht gefunden.") == []
