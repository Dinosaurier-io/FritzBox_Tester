"""Tests der WLAN-Parser fuer ``netsh`` (Windows) und ``iw`` (Linux).

Die Fixtures sind gekuerzte, aber im Aufbau originalgetreue Ausgaben echter
Systeme - inklusive deutscher Umlaute und Dezimalkommata, an denen naive Parser
regelmaessig scheitern.
"""

from __future__ import annotations

import pytest

from fbtest.modules.wlan_monitor import (
    band_from_channel,
    band_from_frequency,
    parse_iw_link,
    parse_netsh_interfaces,
    signal_pct_to_dbm,
)

NETSH_DE_CONNECTED = """
Es ist 1 Schnittstelle auf dem System vorhanden:

    Name                   : WLAN
    Beschreibung           : Intel(R) Wi-Fi 6 AX201 160MHz
    GUID                   : 7f3c1c9a-1111-2222-3333-444455556666
    Physische Adresse      : a4:b1:c1:d1:e1:f1
    Status                 : Verbunden
    SSID                   : Heimnetz-5G
    BSSID                  : 3c:a6:2f:11:22:33
    Netzwerktyp            : Infrastruktur
    Funktyp                : 802.11ax
    Authentifizierung      : WPA2-Personal
    Verschlüsselung        : CCMP
    Verbindungsmodus       : Automatische Verbindung
    Kanal                  : 36
    Empfangsrate (MBit/s)  : 866,7
    Übertragungsrate (MBit/s) : 780,5
    Signal                 : 84%
    Profil                 : Heimnetz-5G
"""

NETSH_EN_CONNECTED = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : Intel(R) Wi-Fi 6 AX201 160MHz
    State                  : connected
    SSID                   : Home-24
    Radio type             : 802.11n
    Channel                : 6
    Receive rate (Mbps)    : 144.4
    Transmit rate (Mbps)   : 144.4
    Signal                 : 62%
"""

NETSH_DE_DISCONNECTED = """
Es ist 1 Schnittstelle auf dem System vorhanden:

    Name                   : WLAN
    Beschreibung           : Intel(R) Wi-Fi 6 AX201 160MHz
    Status                 : Nicht verbunden
    Funkstatus             : Hardware ein/Software ein
"""

NETSH_NO_ADAPTER = """
Das WLAN-AutoConfig-Dienst (wlansvc) wird nicht ausgeführt.
"""

IW_CONNECTED = """
Connected to 3c:a6:2f:11:22:33 (on wlp2s0)
\tSSID: Heimnetz-5G
\tfreq: 5180
\tRX: 812345678 bytes (654321 packets)
\tTX: 12345678 bytes (54321 packets)
\tsignal: -47 dBm
\trx bitrate: 866.7 MBit/s VHT-MCS 9 80MHz short GI VHT-NSS 2
\ttx bitrate: 780.0 MBit/s VHT-MCS 8 80MHz short GI VHT-NSS 2
"""

IW_CONNECTED_24 = """
Connected to 3c:a6:2f:11:22:44 (on wlan0)
\tSSID: Heimnetz-24
\tfreq: 2437
\tsignal: -62 dBm
\trx bitrate: 144.4 MBit/s
"""

IW_NOT_CONNECTED = "Not connected.\n"


class TestNetshParser:
    """Windows-Ausgaben, deutsch und englisch."""

    def test_german_connected(self) -> None:
        info = parse_netsh_interfaces(NETSH_DE_CONNECTED)
        assert info.connected is True
        assert info.interface == "WLAN"
        assert info.ssid == "Heimnetz-5G"
        assert info.channel == 36
        assert info.band == "5GHz"
        assert info.signal_pct == 84
        assert info.rssi_dbm == -58
        assert info.rx_mbps == pytest.approx(866.7)
        assert info.tx_mbps == pytest.approx(780.5)
        assert info.radio_type == "802.11ax"

    def test_english_connected(self) -> None:
        info = parse_netsh_interfaces(NETSH_EN_CONNECTED)
        assert info.connected is True
        assert info.ssid == "Home-24"
        assert info.channel == 6
        assert info.band == "2.4GHz"
        assert info.rx_mbps == pytest.approx(144.4)

    def test_german_disconnected(self) -> None:
        info = parse_netsh_interfaces(NETSH_DE_DISCONNECTED)
        assert info.connected is False
        assert info.interface == "WLAN"
        assert info.ssid is None

    def test_no_adapter(self) -> None:
        assert parse_netsh_interfaces(NETSH_NO_ADAPTER).connected is False

    def test_empty_output(self) -> None:
        assert parse_netsh_interfaces("").connected is False

    def test_ssid_with_spaces_is_kept_intact(self) -> None:
        output = NETSH_DE_CONNECTED.replace("Heimnetz-5G", "Mein WLAN 5 GHz")
        assert parse_netsh_interfaces(output).ssid == "Mein WLAN 5 GHz"


class TestIwParser:
    """Linux-Ausgaben."""

    def test_connected_5ghz(self) -> None:
        info = parse_iw_link(IW_CONNECTED)
        assert info.connected is True
        assert info.interface == "wlp2s0"
        assert info.ssid == "Heimnetz-5G"
        assert info.band == "5GHz"
        assert info.rssi_dbm == -47
        assert info.rx_mbps == pytest.approx(866.7)
        assert info.tx_mbps == pytest.approx(780.0)

    def test_connected_24ghz(self) -> None:
        info = parse_iw_link(IW_CONNECTED_24)
        assert info.band == "2.4GHz"
        assert info.rssi_dbm == -62
        assert info.tx_mbps is None

    def test_not_connected(self) -> None:
        info = parse_iw_link(IW_NOT_CONNECTED, "wlan0")
        assert info.connected is False
        assert info.interface == "wlan0"

    def test_empty_output(self) -> None:
        assert parse_iw_link("").connected is False


class TestBandMapping:
    """Zuordnung Kanal/Frequenz -> Band."""

    @pytest.mark.parametrize(
        ("channel", "band"),
        [(1, "2.4GHz"), (13, "2.4GHz"), (36, "5GHz"), (100, "5GHz"), (196, "5GHz")],
    )
    def test_channels(self, channel: int, band: str) -> None:
        assert band_from_channel(channel) == band

    def test_channel_none(self) -> None:
        assert band_from_channel(None) is None

    @pytest.mark.parametrize(
        ("mhz", "band"),
        [(2412, "2.4GHz"), (2484, "2.4GHz"), (5180, "5GHz"), (5825, "5GHz"), (6175, "6GHz")],
    )
    def test_frequencies(self, mhz: int, band: str) -> None:
        assert band_from_frequency(mhz) == band

    def test_unknown_frequency(self) -> None:
        assert band_from_frequency(900) is None
        assert band_from_frequency(None) is None


class TestSignalConversion:
    """Naeherungsweise Umrechnung der Windows-Signalqualitaet in dBm."""

    @pytest.mark.parametrize(
        ("percent", "dbm"), [(100, -50), (84, -58), (50, -75), (0, -100)]
    )
    def test_conversion(self, percent: int, dbm: int) -> None:
        assert signal_pct_to_dbm(percent) == dbm

    def test_none(self) -> None:
        assert signal_pct_to_dbm(None) is None
