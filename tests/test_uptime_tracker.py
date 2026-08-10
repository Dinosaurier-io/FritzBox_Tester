"""Tests der Neustart- und Reconnect-Erkennung.

Das ist die inhaltlich wichtigste Logik des Systems: Aus dem Verlauf der
Router-Laufzeit wird abgeleitet, ob die Box neu gestartet ist oder "nur" die
Internetverbindung neu aufgebaut wurde.
"""

from __future__ import annotations

from fbtest.core.models import EventType, RouterStatus
from fbtest.modules.uptime_monitor import UptimeTracker, _is_online


def status(
    router_uptime: int | None = None,
    wan_uptime: int | None = None,
    connection: str | None = "Connected",
    timestamp: float = 1_700_000_000.0,
    reachable: bool = True,
    last_error: str | None = None,
) -> RouterStatus:
    """Baut einen Router-Zustand fuer die Tests."""
    return RouterStatus(
        router_uptime_s=router_uptime,
        wan_uptime_s=wan_uptime,
        connection_status=connection,
        last_error=last_error,
        timestamp=timestamp,
        reachable=reachable,
    )


def types(changes: list) -> list[EventType]:  # type: ignore[type-arg]
    """Extrahiert die Ereignistypen einer Aenderungsliste."""
    return [change.type for change in changes]


class TestRebootDetection:
    """Ein Rueckgang der Router-Laufzeit beweist einen Neustart."""

    def test_rising_uptime_is_normal(self) -> None:
        tracker = UptimeTracker()
        tracker.update(status(router_uptime=1000, timestamp=1000.0))
        changes = tracker.update(status(router_uptime=1010, timestamp=1010.0))
        assert EventType.ROUTER_REBOOT not in types(changes)

    def test_falling_uptime_is_a_reboot(self) -> None:
        tracker = UptimeTracker()
        tracker.update(status(router_uptime=86400, timestamp=1000.0))
        changes = tracker.update(status(router_uptime=45, timestamp=1200.0))

        assert EventType.ROUTER_REBOOT in types(changes)
        reboot = next(c for c in changes if c.type is EventType.ROUTER_REBOOT)
        assert reboot.meta["planned"] is False
        assert reboot.meta["uptime_before_s"] == 86400
        assert reboot.meta["uptime_after_s"] == 45
        # Neustartzeitpunkt = Abfragezeit minus neue Laufzeit.
        assert reboot.meta["reboot_at"] == 1200.0 - 45
        assert tracker.stats.reboots_unplanned == 1

    def test_first_poll_never_reports_a_reboot(self) -> None:
        """Ohne Vorzustand ist kein Vergleich moeglich - und keine Behauptung erlaubt."""
        tracker = UptimeTracker()
        changes = tracker.update(status(router_uptime=10))
        assert EventType.ROUTER_REBOOT not in types(changes)

    def test_planned_reboot_is_counted_separately(self) -> None:
        tracker = UptimeTracker()
        tracker.update(status(router_uptime=5000, timestamp=1000.0))
        tracker.mark_planned_reboot()
        changes = tracker.update(status(router_uptime=30, timestamp=1100.0))

        reboot = next(c for c in changes if c.type is EventType.ROUTER_REBOOT)
        assert reboot.meta["planned"] is True
        assert tracker.stats.reboots_planned == 1
        assert tracker.stats.reboots_unplanned == 0

    def test_planned_flag_is_consumed_once(self) -> None:
        tracker = UptimeTracker()
        tracker.update(status(router_uptime=5000, timestamp=1000.0))
        tracker.mark_planned_reboot()
        tracker.update(status(router_uptime=30, timestamp=1100.0))
        tracker.update(status(router_uptime=20, timestamp=1200.0))
        assert tracker.stats.reboots_planned == 1
        assert tracker.stats.reboots_unplanned == 1

    def test_missing_uptime_values_are_ignored(self) -> None:
        tracker = UptimeTracker()
        tracker.update(status(router_uptime=None, timestamp=1000.0))
        changes = tracker.update(status(router_uptime=5, timestamp=1010.0))
        assert EventType.ROUTER_REBOOT not in types(changes)


class TestWanDetection:
    """Trennung von Router-Neustart und reinem Verbindungsabbruch."""

    def test_wan_reconnect_without_router_reboot(self) -> None:
        tracker = UptimeTracker()
        tracker.update(status(router_uptime=86400, wan_uptime=7200, timestamp=1000.0))
        changes = tracker.update(status(router_uptime=86500, wan_uptime=12, timestamp=1100.0))

        assert EventType.WAN_RECONNECT in types(changes)
        assert EventType.ROUTER_REBOOT not in types(changes)
        assert tracker.stats.wan_reconnects == 1

    def test_reboot_also_resets_wan_uptime(self) -> None:
        """Beim Neustart faellt beides - gemeldet werden muss trotzdem beides."""
        tracker = UptimeTracker()
        tracker.update(status(router_uptime=86400, wan_uptime=7200, timestamp=1000.0))
        changes = tracker.update(status(router_uptime=60, wan_uptime=20, timestamp=1300.0))

        assert EventType.ROUTER_REBOOT in types(changes)
        assert EventType.WAN_RECONNECT in types(changes)

    def test_connection_status_change_to_down(self) -> None:
        tracker = UptimeTracker()
        tracker.update(status(connection="Connected", timestamp=1000.0))
        changes = tracker.update(
            status(connection="Disconnected", last_error="ERROR_ISP_TIME_OUT", timestamp=1010.0)
        )
        assert EventType.WAN_DOWN in types(changes)
        assert tracker.stats.wan_down_events == 1
        message = next(c for c in changes if c.type is EventType.WAN_DOWN).message
        assert "ERROR_ISP_TIME_OUT" in message

    def test_connection_status_change_to_up(self) -> None:
        tracker = UptimeTracker()
        tracker.update(status(connection="Disconnected", timestamp=1000.0))
        changes = tracker.update(status(connection="Connected", timestamp=1010.0))
        assert EventType.WAN_UP in types(changes)

    def test_stable_status_produces_no_event(self) -> None:
        tracker = UptimeTracker()
        tracker.update(status(connection="Connected", timestamp=1000.0))
        changes = tracker.update(status(connection="Connected", timestamp=1010.0))
        assert changes == []


class TestReachability:
    """Verhalten, wenn die Box gar nicht antwortet - der Normalfall beim Neustart."""

    def test_unreachable_reports_once_not_repeatedly(self) -> None:
        tracker = UptimeTracker()
        first = tracker.update(status(reachable=False, timestamp=1000.0))
        second = tracker.update(status(reachable=False, timestamp=1010.0))
        assert EventType.ROUTER_UNREACHABLE in types(first)
        assert types(second) == []

    def test_recovery_is_reported(self) -> None:
        tracker = UptimeTracker()
        tracker.update(status(reachable=False, timestamp=1000.0))
        changes = tracker.update(status(router_uptime=30, timestamp=1060.0))
        assert EventType.ROUTER_REACHABLE in types(changes)

    def test_availability_percentage(self) -> None:
        tracker = UptimeTracker()
        for _ in range(9):
            tracker.update(status(router_uptime=100))
        tracker.update(status(reachable=False))
        assert tracker.stats.availability_pct == 90.0

    def test_mtbf_needs_two_failures(self) -> None:
        tracker = UptimeTracker()
        assert tracker.stats.mtbf_s is None
        tracker.update(status(reachable=False, timestamp=1000.0))
        assert tracker.stats.mtbf_s is None
        tracker.update(status(router_uptime=10, timestamp=1100.0))
        tracker.update(status(reachable=False, timestamp=1200.0))
        assert tracker.stats.mtbf_s == 200.0


def test_is_online_mapping() -> None:
    assert _is_online("Connected") is True
    assert _is_online("Up") is True
    assert _is_online("Disconnected") is False
    assert _is_online("Connecting") is False
    assert _is_online(None) is None
