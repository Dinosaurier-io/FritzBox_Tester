"""Tests der Modulplanung (:mod:`fbtest.plan`).

Die Planung beantwortet fuer den Runner *und* fuer die Vorschau im Dialog
«Testlauf starten» dieselbe Frage. Vorher rechnete die Oberflaeche selbst, mit
Regeln, die vom Runner abwichen - die Tests hier halten die frueheren
Abweichungen einzeln fest, damit sie nicht zurueckkehren.
"""

from __future__ import annotations

import copy
import inspect
from typing import Any

import pytest

from fbtest import runner as runner_modul
from fbtest.config import AppConfig
from fbtest.plan import ModulePlan, plan_modules
from tests.conftest import MINIMAL_CONFIG


def baue(**abschnitte: Any) -> AppConfig:
    """Baut eine Konfiguration aus dem Minimalstand plus Abweichungen."""
    roh = copy.deepcopy(MINIMAL_CONFIG)
    for name, werte in abschnitte.items():
        vorhanden = roh.get(name)
        roh[name] = {**vorhanden, **werte} if isinstance(vorhanden, dict) else werte
    return AppConfig.model_validate(roh)


def modul(config: AppConfig, name: str, tr064: bool | None = None) -> ModulePlan:
    """Holt die Planung eines einzelnen Moduls."""
    treffer = [plan for plan in plan_modules(config, tr064) if plan.name == name]
    assert treffer, f"Modul '{name}' kommt in der Planung nicht vor"
    return treffer[0]


class TestVollstaendigkeit:
    """Alles, was der Runner startet, muss auch in der Planung stehen."""

    def test_alle_module_erscheinen(self, config: AppConfig) -> None:
        """`uptime` fehlte in der Vorschau komplett - es wurde nie erwaehnt."""
        namen = [plan.name for plan in plan_modules(config, tr064_available=True)]
        assert namen == ["ping", "uptime", "traffic", "speedtest", "wlan"]

    def test_runner_entscheidet_nicht_selbst(self) -> None:
        """Der Runner darf die Bedingungen nicht noch einmal formulieren.

        Sonst entsteht genau die Abweichung wieder, die es zu beseitigen galt:
        zwei Stellen mit denselben Regeln, von denen nur eine gepflegt wird.
        """
        quelltext = inspect.getsource(runner_modul.TestRunner._build_modules)
        assert "plan_modules" in quelltext
        for verboten in (".enabled", ".client_view", ".profiles"):
            assert verboten not in quelltext, (
                f"_build_modules prueft wieder selbst ({verboten}) statt plan_modules zu nutzen"
            )


class TestPing:
    """Ohne Ping gibt es keine Ausfallerkennung."""

    def test_zaehlt_ziele_und_nennt_dns(self, config: AppConfig) -> None:
        plan = modul(config, "ping")
        assert plan.active
        assert "2 Ziele" in plan.detail
        assert "DNS" in plan.detail

    def test_abgeschaltet_mit_begruendung(self) -> None:
        plan = modul(baue(ping={"enabled": False}), "ping")
        assert not plan.active
        assert "Ausfallerkennung" in plan.reason


class TestUptime:
    """Haengt allein am TR-064-Zugriff."""

    @pytest.mark.parametrize(
        "tr064,zustand", [(True, "aktiv"), (False, "inaktiv"), (None, "offen")]
    )
    def test_zustand_folgt_dem_zugriff(
        self, config: AppConfig, tr064: bool | None, zustand: str
    ) -> None:
        """Ohne gepruefte Erreichbarkeit wird keine behauptet."""
        assert modul(config, "uptime", tr064).state == zustand

    def test_ohne_zugriff_wird_der_verlust_benannt(self, config: AppConfig) -> None:
        assert "Neustarts" in modul(config, "uptime", False).reason


class TestTraffic:
    """Die Vorschau zaehlte Profile, der Lauf erzeugt virtuelle Clients."""

    def test_zaehlt_virtuelle_clients(self) -> None:
        """Zwei Profile mit je zwei Clients sind vier gleichzeitige Verbindungen."""
        config = baue(
            traffic={
                "profiles": [
                    {"name": "a", "type": "download", "url": "http://x/1", "clients": 2},
                    {"name": "b", "type": "download", "url": "http://x/2", "clients": 2},
                ]
            }
        )
        plan = modul(config, "traffic")
        assert plan.active
        assert "2 Profile" in plan.detail
        assert "4 virtuelle Clients" in plan.detail

    def test_nennt_das_datenvolumen(self) -> None:
        """10 Mbit/s Dauerlast sind 108 GB am Tag - das gehoert vor den Start."""
        config = baue(
            traffic={
                "profiles": [
                    {
                        "name": "a",
                        "type": "download",
                        "url": "http://x/1",
                        "target_rate_mbps": 10.0,
                    }
                ]
            }
        )
        assert "108 GB/Tag" in modul(config, "traffic").detail

    def test_alle_profile_abgeschaltet_ist_inaktiv(self) -> None:
        """Frueher lief das Modul und meldete «0 Profile» - es tat nichts."""
        config = baue(
            traffic={
                "profiles": [
                    {
                        "name": "a",
                        "type": "download",
                        "url": "http://x/1",
                        "enabled": False,
                    }
                ]
            }
        )
        plan = modul(config, "traffic")
        assert not plan.active
        assert "abgeschaltet" in plan.reason.lower()

    def test_ohne_profile_inaktiv(self) -> None:
        assert not modul(baue(traffic={"profiles": []}), "traffic").active


class TestWlan:
    """Die Routersicht braucht TR-064, die Clientsicht nicht."""

    def test_clientsicht_reicht_ohne_router(self) -> None:
        config = baue(wlan={"client_view": True, "router_view": False})
        assert modul(config, "wlan", tr064=False).active

    def test_ohne_clientsicht_und_ohne_zugriff_inaktiv(self) -> None:
        """Die Vorschau versprach hier frueher WLAN, das nie startete."""
        config = baue(wlan={"client_view": False, "router_view": True})
        plan = modul(config, "wlan", tr064=False)
        assert not plan.active
        assert "TR-064" in plan.reason

    def test_ungeklaerter_zugriff_bleibt_offen(self) -> None:
        config = baue(wlan={"client_view": False, "router_view": True})
        assert modul(config, "wlan", tr064=None).state == "offen"


class TestSpeedtest:
    """Nur die Upload-Messung ist optional."""

    def test_ohne_upload_url_nur_download(self, config: AppConfig) -> None:
        plan = modul(config, "speedtest")
        assert plan.detail.endswith("Download")

    def test_mit_upload_url_beides(self) -> None:
        config = baue(speedtest={"upload_url": "http://x/post"})
        assert modul(config, "speedtest").detail.endswith("Download und Upload")
