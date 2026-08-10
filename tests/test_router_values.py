"""Tests der Wertaufbereitung aus TR-064-Antworten."""

from __future__ import annotations

import pytest

from fbtest.router.fritzbox import _kbps


class TestSyncRate:
    """Umrechnung der Leitungsgeschwindigkeit."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (250_000_000, 250_000),
            ("116000000", 116_000),
            (0, 0),
        ],
    )
    def test_converts_bits_to_kilobits(self, value: object, expected: int) -> None:
        assert _kbps(value) == expected

    def test_unknown_rate_becomes_none(self) -> None:
        """An Glasfaseranschluessen meldet die Box den Datentyp-Maximalwert.

        Ungeprueft uebernommen ergaebe das eine Leitungsgeschwindigkeit von
        4.3 Tbit/s im Bericht - eine Zahl, die glaubwuerdig formatiert ist und
        trotzdem nichts bedeutet.
        """
        assert _kbps(2**32 - 1) is None

    @pytest.mark.parametrize("value", [None, "", "keine Zahl"])
    def test_missing_values_stay_none(self, value: object) -> None:
        assert _kbps(value) is None
