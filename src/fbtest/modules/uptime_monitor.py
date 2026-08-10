"""Router-Telemetrie: Laufzeit, WAN-Status, Neustart- und Reconnect-Erkennung.

Kernidee: Die FRITZ!Box meldet ueber TR-064 ihre eigene Laufzeit (``NewUpTime``).
Dieser Wert waechst monoton - **ausser** die Box wurde neu gestartet. Ein
Rueckgang der Laufzeit ist damit ein direkter, eindeutiger Beweis fuer einen
Neustart, und aus dem neuen Wert laesst sich der Neustartzeitpunkt berechnen.

Dasselbe Prinzip auf die WAN-Laufzeit angewendet unterscheidet einen echten
Router-Neustart von einem blossen Verbindungsabbruch (Zwangstrennung, DSL-Resync):

* Router-Laufzeit sinkt  -> ``ROUTER_REBOOT``  (das Geraet war weg)
* nur WAN-Laufzeit sinkt -> ``WAN_RECONNECT``  (nur die Internetverbindung war weg)

Genau diese Trennung macht die Firmware-Vergleiche belastbar: Ein Router, der
taeglich neu startet, ist etwas anderes als einer, der nur den PPPoE-Tunnel neu
aufbaut.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from fbtest.core.events import EventBus
from fbtest.core.models import EventType, RouterStatus, Severity
from fbtest.core.state import RuntimeState
from fbtest.modules.base import IntervalModule
from fbtest.router.fritzbox import FritzBoxClient

log = logging.getLogger(__name__)

#: Verbindungsstatus, die als "online" gelten.
_ONLINE_STATES = {"Connected", "Up"}


@dataclass(slots=True)
class UptimeChange:
    """Ein erkannter Wechsel im Router-Zustand."""

    type: EventType
    message: str
    severity: Severity = Severity.INFO
    meta: dict[str, float | str | bool] = field(default_factory=dict)


@dataclass(slots=True)
class AvailabilityStats:
    """Fortgeschriebene Verfuegbarkeitskennzahlen eines Testlaufs."""

    polls_total: int = 0
    polls_reachable: int = 0
    reboots_unplanned: int = 0
    reboots_planned: int = 0
    wan_reconnects: int = 0
    wan_down_events: int = 0
    downtime_s: float = 0.0
    longest_downtime_s: float = 0.0
    failure_timestamps: list[float] = field(default_factory=list)

    @property
    def availability_pct(self) -> float:
        """Anteil erfolgreicher Abfragen in Prozent."""
        if not self.polls_total:
            return 100.0
        return 100.0 * self.polls_reachable / self.polls_total

    @property
    def mtbf_s(self) -> float | None:
        """Mittlere Zeit zwischen zwei Ausfaellen in Sekunden.

        Returns:
            ``None``, solange weniger als zwei Ausfaelle vorliegen - vorher ist
            die Kennzahl nicht definiert.
        """
        if len(self.failure_timestamps) < 2:
            return None
        first, last = self.failure_timestamps[0], self.failure_timestamps[-1]
        return (last - first) / (len(self.failure_timestamps) - 1)


class UptimeTracker:
    """Wertet aufeinanderfolgende Router-Zustaende aus (ohne Netzwerkzugriff).

    Die Klasse ist bewusst rein: Sie bekommt Zustaende hineingereicht und liefert
    die daraus folgenden Ereignisse zurueck. Damit lassen sich Neustart- und
    Reconnect-Erkennung mit synthetischen Uptime-Verlaeufen testen.
    """

    def __init__(self) -> None:
        self.previous: RouterStatus | None = None
        self.stats = AvailabilityStats()
        self._planned_reboot_pending = False
        self._unreachable_since_mono: float | None = None

    def mark_planned_reboot(self) -> None:
        """Kuendigt einen selbst ausgeloesten Neustart an.

        Der naechste erkannte Neustart wird dann als *geplant* gezaehlt. In der
        Alpha loest das Testsystem selbst keine Neustarts aus (es ist ein Mess-,
        kein Steuerwerkzeug); die Methode existiert fuer manuelle Wartungsfenster.
        """
        self._planned_reboot_pending = True

    def update(self, status: RouterStatus) -> list[UptimeChange]:
        """Verarbeitet einen neuen Router-Zustand.

        Args:
            status: Die frisch abgefragte Telemetrie.

        Returns:
            Alle aus dem Vergleich mit dem Vorzustand folgenden Ereignisse.
        """
        changes: list[UptimeChange] = []
        previous = self.previous
        self.stats.polls_total += 1

        if not status.reachable:
            self.stats.failure_timestamps.append(status.timestamp)
            if self._unreachable_since_mono is None:
                self._unreachable_since_mono = time.monotonic()
                changes.append(
                    UptimeChange(
                        EventType.ROUTER_UNREACHABLE,
                        "FRITZ!Box antwortet nicht mehr auf TR-064-Abfragen.",
                        Severity.ERROR,
                        {"last_error": status.last_error or ""},
                    )
                )
            self.previous = status
            return changes

        self.stats.polls_reachable += 1

        if self._unreachable_since_mono is not None:
            gap = time.monotonic() - self._unreachable_since_mono
            self.stats.downtime_s += gap
            self.stats.longest_downtime_s = max(self.stats.longest_downtime_s, gap)
            self._unreachable_since_mono = None
            changes.append(
                UptimeChange(
                    EventType.ROUTER_REACHABLE,
                    f"FRITZ!Box antwortet wieder (Ausfall der TR-064-Abfrage: {gap:.1f} s).",
                    Severity.WARNING,
                    {"gap_s": round(gap, 1)},
                )
            )

        changes.extend(self._check_router_reboot(previous, status))
        changes.extend(self._check_wan(previous, status))

        self.previous = status
        return changes

    def _check_router_reboot(
        self, previous: RouterStatus | None, status: RouterStatus
    ) -> list[UptimeChange]:
        """Erkennt einen Router-Neustart an einer gesunkenen Laufzeit."""
        if previous is None or previous.router_uptime_s is None or status.router_uptime_s is None:
            return []
        if status.router_uptime_s >= previous.router_uptime_s:
            return []

        planned = self._planned_reboot_pending
        self._planned_reboot_pending = False
        if planned:
            self.stats.reboots_planned += 1
        else:
            self.stats.reboots_unplanned += 1

        reboot_at = status.timestamp - status.router_uptime_s
        offline_estimate = max(0.0, reboot_at - (previous.timestamp - previous.router_uptime_s))

        return [
            UptimeChange(
                EventType.ROUTER_REBOOT,
                (
                    f"{'Geplanter' if planned else 'UNGEPLANTER'} Router-Neustart erkannt: "
                    f"Laufzeit fiel von {previous.router_uptime_s} s auf "
                    f"{status.router_uptime_s} s. Geschaetzter Neustart um "
                    f"{time.strftime('%d.%m.%Y %H:%M:%S', time.localtime(reboot_at))}."
                ),
                Severity.INFO if planned else Severity.CRITICAL,
                {
                    "planned": planned,
                    "uptime_before_s": previous.router_uptime_s,
                    "uptime_after_s": status.router_uptime_s,
                    "reboot_at": reboot_at,
                    "estimated_downtime_s": round(offline_estimate, 1),
                },
            )
        ]

    def _check_wan(
        self, previous: RouterStatus | None, status: RouterStatus
    ) -> list[UptimeChange]:
        """Erkennt WAN-Reconnects und Statuswechsel der Internetverbindung."""
        changes: list[UptimeChange] = []

        if (
            previous is not None
            and previous.wan_uptime_s is not None
            and status.wan_uptime_s is not None
            and status.wan_uptime_s < previous.wan_uptime_s
        ):
            self.stats.wan_reconnects += 1
            reconnect_at = status.timestamp - status.wan_uptime_s
            changes.append(
                UptimeChange(
                    EventType.WAN_RECONNECT,
                    (
                        "Internetverbindung wurde neu aufgebaut (WAN-Laufzeit fiel von "
                        f"{previous.wan_uptime_s} s auf {status.wan_uptime_s} s) - "
                        "der Router selbst lief dabei weiter. Zeitpunkt: "
                        f"{time.strftime('%d.%m.%Y %H:%M:%S', time.localtime(reconnect_at))}."
                    ),
                    Severity.WARNING,
                    {
                        "wan_uptime_before_s": previous.wan_uptime_s,
                        "wan_uptime_after_s": status.wan_uptime_s,
                        "reconnect_at": reconnect_at,
                    },
                )
            )

        previous_online = _is_online(previous.connection_status) if previous else None
        current_online = _is_online(status.connection_status)

        if previous_online is not None and previous_online != current_online:
            if current_online:
                changes.append(
                    UptimeChange(
                        EventType.WAN_UP,
                        f"WAN-Status wechselt auf '{status.connection_status}'.",
                        Severity.WARNING,
                        {"status": status.connection_status or ""},
                    )
                )
            else:
                self.stats.wan_down_events += 1
                changes.append(
                    UptimeChange(
                        EventType.WAN_DOWN,
                        (
                            f"WAN-Status wechselt auf '{status.connection_status}'"
                            + (
                                f" (letzter Fehler: {status.last_error})"
                                if status.last_error and status.last_error != "ERROR_NONE"
                                else ""
                            )
                            + "."
                        ),
                        Severity.ERROR,
                        {
                            "status": status.connection_status or "",
                            "last_error": status.last_error or "",
                        },
                    )
                )

        return changes


def _is_online(connection_status: str | None) -> bool | None:
    """Bewertet einen TR-064-Verbindungsstatus.

    Returns:
        ``True``/``False`` oder ``None``, wenn kein Status vorliegt.
    """
    if connection_status is None:
        return None
    return connection_status in _ONLINE_STATES


class UptimeMonitor(IntervalModule):
    """Fragt die FRITZ!Box zyklisch ab und meldet Zustandswechsel."""

    name = "uptime"

    def __init__(
        self,
        bus: EventBus,
        client: FritzBoxClient,
        state: RuntimeState,
        interval_s: float,
        source: str = "master",
    ) -> None:
        super().__init__(bus, interval_s, source)
        self.client = client
        self.state = state
        self.tracker = UptimeTracker()
        self._last_bytes: tuple[int, int, float] | None = None

    async def tick(self) -> None:
        """Fragt einen Router-Zustand ab, wertet ihn aus und speichert ihn."""
        status = await self.client.poll_status()
        self.bus.publish(status)

        for change in self.tracker.update(status):
            self.emit(change.type, change.message, change.severity, **change.meta)

        self.state.tr064_available = status.reachable

        if not status.reachable:
            return

        self._publish_measurements(status)
        self._publish_throughput(status)

    def _publish_measurements(self, status: RouterStatus) -> None:
        """Legt die Kennzahlen des Zustands als Messwerte auf den Bus."""
        if status.router_uptime_s is not None:
            self.measure("router_uptime", status.router_uptime_s, "s")
        if status.wan_uptime_s is not None:
            self.measure("wan_uptime", status.wan_uptime_s, "s")
        if status.sync_down_kbps is not None:
            self.measure("sync_down", status.sync_down_kbps, "kbit/s")
        if status.sync_up_kbps is not None:
            self.measure("sync_up", status.sync_up_kbps, "kbit/s")
        if status.host_count is not None:
            self.measure("hosts", status.host_count, "")

        stats = self.tracker.stats
        self.measure("availability", stats.availability_pct, "%")
        self.measure("reboots_unplanned", stats.reboots_unplanned, "")
        self.measure("wan_reconnects", stats.wan_reconnects, "")

    def _publish_throughput(self, status: RouterStatus) -> None:
        """Berechnet den realen Durchsatz aus der Differenz der Byte-Zaehler."""
        if status.bytes_sent is None or status.bytes_received is None:
            return

        now = time.monotonic()
        previous = self._last_bytes
        self._last_bytes = (status.bytes_sent, status.bytes_received, now)
        if previous is None:
            return

        prev_sent, prev_received, prev_mono = previous
        elapsed = now - prev_mono
        if elapsed <= 0:
            return

        delta_sent = status.bytes_sent - prev_sent
        delta_received = status.bytes_received - prev_received
        # Negative Differenzen bedeuten einen Zaehleruberlauf oder einen
        # Router-Neustart - in beiden Faellen ist der Wert nicht auswertbar.
        if delta_sent < 0 or delta_received < 0:
            self._log.debug(
                "Byte-Zaehler zurueckgesetzt (Neustart oder Ueberlauf) - uebersprungen."
            )
            return

        self.measure("throughput_up", delta_sent * 8 / elapsed / 1e6, "Mbit/s")
        self.measure("throughput_down", delta_received * 8 / elapsed / 1e6, "Mbit/s")
