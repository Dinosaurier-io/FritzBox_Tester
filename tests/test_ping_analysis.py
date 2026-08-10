"""Tests der Ping-Auswertung: Ausgabe-Parser, Aggregation, Ausfallerkennung."""

from __future__ import annotations

import pytest

from fbtest.core.models import OutageScope
from fbtest.core.state import RuntimeState
from fbtest.modules.ping_monitor import (
    OutageTracker,
    aggregate_window,
    compute_jitter,
    parse_ping_output,
    percentile,
)

# --------------------------------------------------------------------------
# Beispielausgaben echter Systeme (Fixtures)
# --------------------------------------------------------------------------

PING_WINDOWS_DE = """
Ping wird ausgeführt für 1.1.1.1 mit 32 Bytes Daten:
Antwort von 1.1.1.1: Bytes=32 Zeit=13ms TTL=57

Ping-Statistik für 1.1.1.1:
    Pakete: Gesendet = 1, Empfangen = 1, Verloren = 0 (0% Verlust),
"""

PING_WINDOWS_DE_SUBMS = """
Antwort von 192.168.178.1: Bytes=32 Zeit<1ms TTL=64
"""

PING_WINDOWS_DE_UNREACHABLE = """
Ping wird ausgeführt für 192.168.178.99 mit 32 Bytes Daten:
Antwort von 192.168.178.20: Zielhost nicht erreichbar.

Ping-Statistik für 192.168.178.99:
    Pakete: Gesendet = 1, Empfangen = 1, Verloren = 0 (0% Verlust),
"""

PING_WINDOWS_DE_TIMEOUT = """
Ping wird ausgeführt für 10.0.0.99 mit 32 Bytes Daten:
Zeitüberschreitung der Anforderung.
"""

PING_LINUX_EN = """
PING 1.1.1.1 (1.1.1.1) 56(84) bytes of data.
64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=12.4 ms

--- 1.1.1.1 ping statistics ---
1 packets transmitted, 1 received, 0% packet loss, time 0ms
"""

PING_LINUX_EN_LOSS = """
PING 10.0.0.99 (10.0.0.99) 56(84) bytes of data.

--- 10.0.0.99 ping statistics ---
1 packets transmitted, 0 received, 100% packet loss, time 0ms
"""


class TestParsePingOutput:
    """Der Parser muss deutsche wie englische Ausgaben verstehen."""

    def test_windows_german(self) -> None:
        assert parse_ping_output(PING_WINDOWS_DE) == 13.0

    def test_windows_sub_millisecond(self) -> None:
        """'Zeit<1ms' ist eine Antwort, kein Verlust."""
        assert parse_ping_output(PING_WINDOWS_DE_SUBMS) == 1.0

    def test_linux_english_with_decimals(self) -> None:
        assert parse_ping_output(PING_LINUX_EN) == 12.4

    def test_unreachable_counts_as_loss(self) -> None:
        """Windows meldet 'Zielhost nicht erreichbar' mit Exitcode 0 - trotzdem Verlust."""
        assert parse_ping_output(PING_WINDOWS_DE_UNREACHABLE) is None

    def test_timeout_counts_as_loss(self) -> None:
        assert parse_ping_output(PING_WINDOWS_DE_TIMEOUT) is None

    def test_full_loss_counts_as_loss(self) -> None:
        assert parse_ping_output(PING_LINUX_EN_LOSS) is None

    def test_empty_output(self) -> None:
        assert parse_ping_output("") is None


class TestPercentile:
    """Perzentil per naechstem Rang (ohne Interpolation)."""

    def test_median(self) -> None:
        assert percentile([1.0, 2.0, 3.0], 50) == 2.0

    def test_p95_returns_measured_value(self) -> None:
        values = [float(i) for i in range(1, 101)]
        assert percentile(values, 95) in values

    def test_extremes(self) -> None:
        values = [5.0, 1.0, 3.0]
        assert percentile(values, 0) == 1.0
        assert percentile(values, 100) == 5.0

    def test_single_value(self) -> None:
        assert percentile([42.0], 95) == 42.0

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="leeren Liste"):
            percentile([], 50)


class TestJitter:
    """Jitter als mittlere absolute Differenz aufeinanderfolgender Messungen."""

    def test_constant_latency_has_no_jitter(self) -> None:
        assert compute_jitter([10.0, 10.0, 10.0]) == 0.0

    def test_alternating(self) -> None:
        assert compute_jitter([10.0, 20.0, 10.0]) == 10.0

    def test_needs_two_values(self) -> None:
        assert compute_jitter([10.0]) is None
        assert compute_jitter([]) is None


class TestAggregateWindow:
    """Verdichtung eines Messfensters."""

    def test_all_successful(self) -> None:
        stats = aggregate_window([10.0, 20.0, 30.0, 40.0])
        assert stats is not None
        assert stats.sent == 4
        assert stats.lost == 0
        assert stats.loss_pct == 0.0
        assert stats.rtt_min == 10.0
        assert stats.rtt_max == 40.0
        assert stats.rtt_avg == 25.0

    def test_partial_loss(self) -> None:
        stats = aggregate_window([10.0, None, 30.0, None])
        assert stats is not None
        assert stats.sent == 4
        assert stats.lost == 2
        assert stats.loss_pct == 50.0
        assert stats.rtt_avg == 20.0

    def test_total_loss_keeps_counters_but_no_rtt(self) -> None:
        stats = aggregate_window([None, None, None])
        assert stats is not None
        assert stats.loss_pct == 100.0
        assert stats.rtt_avg is None
        assert stats.jitter_ms is None

    def test_empty_window(self) -> None:
        assert aggregate_window([]) is None


class TestOutageTracker:
    """Ausfallerkennung und -klassifikation.

    Der Aufbau entspricht der Realkonfiguration: ein Gateway-Ziel (die
    FRITZ!Box) und zwei Internet-Ziele.
    """

    @staticmethod
    def _tracker(threshold: int = 3, state: RuntimeState | None = None) -> OutageTracker:
        tracker = OutageTracker(threshold, state)
        tracker.register("fritzbox", "gateway")
        tracker.register("cloudflare", "internet")
        tracker.register("google", "internet")
        return tracker

    @staticmethod
    def _round(tracker: OutageTracker, gateway: bool, internet: bool) -> object:
        """Simuliert eine komplette Messrunde ueber alle Ziele."""
        tracker.record("fritzbox", gateway)
        tracker.record("cloudflare", internet)
        tracker.record("google", internet)
        return tracker.evaluate()

    def test_no_outage_when_everything_works(self) -> None:
        tracker = self._tracker()
        for _ in range(10):
            assert self._round(tracker, True, True) is None
        assert tracker.current is None

    def test_single_failures_below_threshold_are_ignored(self) -> None:
        """Ein einzelnes verlorenes Paket ist kein Ausfall."""
        tracker = self._tracker(threshold=3)
        self._round(tracker, True, False)
        self._round(tracker, True, False)
        assert tracker.current is None
        self._round(tracker, True, True)
        assert tracker.current is None

    def test_wan_outage_when_gateway_still_answers(self) -> None:
        """FRITZ!Box erreichbar, Internet weg -> Problem hinter dem Router."""
        tracker = self._tracker(threshold=3)
        for _ in range(2):
            assert self._round(tracker, True, False) is None
        transition = self._round(tracker, True, False)
        assert transition is not None
        kind, outage = transition
        assert kind == "start"
        assert outage.scope is OutageScope.WAN

    def test_gateway_outage_without_known_wlan_state(self) -> None:
        """Nichts mehr erreichbar und keine lokale Ursache bekannt -> full."""
        tracker = self._tracker(threshold=2)
        self._round(tracker, False, False)
        transition = self._round(tracker, False, False)
        assert transition is not None
        assert transition[1].scope is OutageScope.FULL

    def test_gateway_outage_attributed_to_wlan(self) -> None:
        """Ist die WLAN-Verbindung nachweislich getrennt, ist das die Ursache."""
        state = RuntimeState()
        state.link.update(is_wireless=True, connected=False, ssid=None)
        tracker = self._tracker(threshold=2, state=state)
        self._round(tracker, False, False)
        transition = self._round(tracker, False, False)
        assert transition is not None
        assert transition[1].scope is OutageScope.WLAN

    def test_unknown_wlan_state_is_not_blamed_on_wlan(self) -> None:
        """Bei unbekanntem WLAN-Zustand wird bewusst nichts behauptet."""
        state = RuntimeState()  # wlan_connected bleibt None
        tracker = self._tracker(threshold=2, state=state)
        self._round(tracker, False, False)
        transition = self._round(tracker, False, False)
        assert transition is not None
        assert transition[1].scope is OutageScope.FULL

    def test_gateway_down_but_internet_up(self) -> None:
        """Manche Boxen antworten nicht auf ICMP, routen aber weiter."""
        tracker = self._tracker(threshold=2)
        self._round(tracker, False, True)
        transition = self._round(tracker, False, True)
        assert transition is not None
        assert transition[1].scope is OutageScope.GATEWAY

    def test_outage_end_reports_duration(self) -> None:
        tracker = self._tracker(threshold=2)
        self._round(tracker, True, False)
        self._round(tracker, True, False)
        assert tracker.current is not None

        transition = self._round(tracker, True, True)
        assert transition is not None
        kind, outage = transition
        assert kind == "end"
        assert outage.ended_at is not None
        assert outage.duration_s is not None
        assert outage.duration_s >= 0.0
        assert tracker.current is None

    def test_outage_starts_at_first_failure_not_at_threshold(self) -> None:
        """Die Ausfalldauer soll den ersten Fehlschlag enthalten, nicht erst die Schwelle."""
        tracker = self._tracker(threshold=3)
        tracker.record("cloudflare", False, timestamp=1000.0)
        tracker.record("google", False, timestamp=1000.0)
        tracker.record("fritzbox", True, timestamp=1000.0)
        tracker.evaluate()
        for ts in (1001.0, 1002.0):
            tracker.record("cloudflare", False, timestamp=ts)
            tracker.record("google", False, timestamp=ts)
            tracker.record("fritzbox", True, timestamp=ts)
        transition = tracker.evaluate()
        assert transition is not None
        assert transition[1].started_at == 1000.0

    def test_scope_is_sharpened_during_ongoing_outage(self) -> None:
        """Faellt waehrend eines WAN-Ausfalls auch das Gateway aus, wird das nachgezogen."""
        tracker = self._tracker(threshold=2)
        self._round(tracker, True, False)
        transition = self._round(tracker, True, False)
        assert transition is not None
        assert transition[1].scope is OutageScope.WAN

        self._round(tracker, False, False)
        self._round(tracker, False, False)
        assert tracker.current is not None
        assert tracker.current.scope is OutageScope.FULL

    def test_recovery_resets_failure_counter(self) -> None:
        tracker = self._tracker(threshold=3)
        self._round(tracker, True, False)
        self._round(tracker, True, False)
        self._round(tracker, True, True)
        self._round(tracker, True, False)
        self._round(tracker, True, False)
        assert tracker.current is None, "Der Zaehler haette zurueckgesetzt werden muessen"

    def test_stats_count_sent_and_lost(self) -> None:
        tracker = self._tracker()
        self._round(tracker, True, False)
        self._round(tracker, True, True)
        stats = tracker.stats()
        assert stats["fritzbox"] == (2, 0)
        assert stats["cloudflare"] == (2, 1)
