"""Tests der Massnahmen gegen systematische Messfehler.

Beide hier geprueften Verhalten stammen aus echten Messreihen: In fuenf von
fuenf Testlaeufen war der groesste DNS-Wert die allererste Abfrage, und der
Bufferbloat-Wert kippte zweimal ins Negative, weil ein einzelner verschlafener
Ping den Mittelwert hob.
"""

from __future__ import annotations

import asyncio
import statistics
from typing import Any

import pytest

from fbtest.config import DnsCheckConfig, PingConfig, PingTarget, SpeedtestConfig
from fbtest.core.events import EventBus
from fbtest.core.models import Measurement
from fbtest.core.state import RuntimeState
from fbtest.modules.ping_monitor import PingMonitor
from fbtest.modules.speedtest import SpeedtestModule


def _measurements(bus: EventBus, metric: str) -> list[Measurement]:
    """Sammelt alle veroeffentlichten Messwerte einer Metrik."""
    return [
        item
        for item in bus.get_nowait_batch(10_000)
        if isinstance(item, Measurement) and item.metric == metric
    ]


class TestDnsWarmup:
    """Die erste Aufloesung je Name wird nicht gewertet."""

    @staticmethod
    def _monitor(bus: EventBus, hostnames: list[str]) -> PingMonitor:
        # Ein Ping-Ziel ist Pflicht, wird hier aber nicht benutzt -
        # geprueft wird ausschliesslich die DNS-Schleife.
        config = PingConfig(
            targets=[PingTarget(name="gw", host="127.0.0.1", scope="gateway")],
            dns=DnsCheckConfig(enabled=True, hostnames=hostnames, interval_s=0.01),
        )
        return PingMonitor(bus, config, RuntimeState())

    async def test_first_resolution_is_discarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Der Anlaufwert darf die Messreihe nicht bestimmen.

        Nachgestellt wird der beobachtete Fall: eine sehr langsame erste
        Abfrage, danach schnelle. Ohne Warmlauf laege der Mittelwert um ein
        Vielfaches ueber allen tatsaechlichen Werten.
        """
        # Erste Abfrage deutlich langsam, danach schnell - so sah es real aus.
        delays = iter([0.08])

        async def fake_resolve(*_: Any, **__: Any) -> list[Any]:
            await asyncio.sleep(next(delays, 0.0))
            return []

        monkeypatch.setattr(
            "fbtest.modules.ping_monitor.asyncio.to_thread",
            lambda *args, **kwargs: fake_resolve(),
        )

        bus = EventBus()
        monitor = self._monitor(bus, ["example.test"])
        task = asyncio.create_task(monitor._dns_loop())
        await asyncio.sleep(0.20)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        values = [item.value for item in _measurements(bus, "dns_resolve")]
        assert values, "Nach dem Warmlauf muessen Werte ankommen."
        assert max(values) < 40, f"Der Anlaufwert wurde mitgezaehlt: {values}"

    async def test_warmup_is_per_hostname(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Jeder Name hat seinen eigenen Anlaufeffekt."""

        async def fake_resolve(*_: Any, **__: Any) -> list[Any]:
            return []

        monkeypatch.setattr(
            "fbtest.modules.ping_monitor.asyncio.to_thread",
            lambda *args, **kwargs: fake_resolve(),
        )

        bus = EventBus()
        monitor = self._monitor(bus, ["a.test", "b.test"])
        task = asyncio.create_task(monitor._dns_loop())
        await asyncio.sleep(0.06)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        hosts = [item.meta.get("hostname") for item in _measurements(bus, "dns_resolve")]
        # Beide Namen liefern Werte - aber erst ab der zweiten Runde.
        assert set(hosts) == {"a.test", "b.test"}


class _StubBackend:
    """Liefert vorgegebene Antwortzeiten der Reihe nach."""

    name = "stub"

    def __init__(self, values: list[float]) -> None:
        self.values = list(values)

    async def ping(self, host: str, timeout_s: float) -> float | None:
        return self.values.pop(0) if self.values else None


class TestLatencyMedian:
    """Ein einzelner Ausreisser darf den Latenzwert nicht bestimmen."""

    @staticmethod
    def _module(values: list[float]) -> SpeedtestModule:
        from fbtest.core.events import TrafficGate

        module = SpeedtestModule(EventBus(), SpeedtestConfig(), TrafficGate())
        module._backend = _StubBackend(values)  # type: ignore[assignment]
        return module

    async def test_single_outlier_does_not_move_the_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Im WLAN-Leerlauf antwortet ein Ping verspaetet - der Rest nicht.

        Mit dem arithmetischen Mittel haette der Wert bei 5.2 ms gelegen und
        damit ueber der spaeteren Messung unter Last; der Bufferbloat-Wert
        waere negativ geworden.
        """
        monkeypatch.setattr("fbtest.modules.speedtest.asyncio.sleep", _no_sleep)
        samples = [4.0, 4.1, 3.9, 4.0, 4.2, 4.0, 3.8, 4.1, 4.0, 16.0]
        module = self._module(samples)

        result = await module._measure_latency()

        assert result == statistics.median(samples)
        assert result is not None and result < 4.5
        assert statistics.fmean(samples) > 5.0, "Der Mittelwert waere verschoben gewesen."

    async def test_no_answer_at_all_yields_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("fbtest.modules.speedtest.asyncio.sleep", _no_sleep)
        assert await self._module([])._measure_latency() is None


async def _no_sleep(_: float) -> None:
    """Ersetzt die Wartezeit zwischen den Stichproben."""
    return None
