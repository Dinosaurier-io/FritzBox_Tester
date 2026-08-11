"""Tests der Dashboard-API.

Laeuft komplett ohne echten Testlauf und ohne Netzwerk: Geprueft werden die
Schnittstelle, die Fehlerbehandlung und die Datenaufbereitung fuer die
Oberflaeche. Das Starten eines echten Laufs bleibt dem manuellen Test
vorbehalten - dafuer braucht es eine FRITZ!Box.
"""

from __future__ import annotations

import contextlib
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fbtest.config import AppConfig
from fbtest.config_service import PASSWORD_MASK
from fbtest.context import AppContext
from fbtest.core.models import Event, EventType, Measurement, Outage, OutageScope, Severity
from fbtest.dashboard.app import STATIC_DIR, TOKEN_HEADER, TOKEN_PLACEHOLDER, create_app
from fbtest.dashboard.controller import ControllerState, LastResult, RunController
from fbtest.paths import AppPaths, PathOrigin
from fbtest.runner import RunnerStatus
from fbtest.secrets_store import SecretStore, set_default_store
from fbtest.storage.database import Database

from .test_secrets_store import FakeBackend

#: Eine gueltige, kommentierte Konfigurationsdatei fuer die Tests der
#: Einstellungs-API. Bewusst mit Kommentar, damit auffaellt, wenn das
#: Speichern ihn verschluckt.
CONFIG_FILE = """\
# Testkonfiguration
router:
  host: 192.168.178.1

ping:
  targets:
    - name: fritzbox
      host: 192.168.178.1
      scope: gateway
    - name: cloudflare
      host: 1.1.1.1
      scope: internet
"""


class _FakeRunner:
    """Ein Testlauf-Platzhalter, der nur seine ID kennt.

    Reicht fuer die Faelle, in denen der Controller lediglich wissen muss,
    *welcher* Lauf gerade aktiv ist - ohne echten Testlauf und ohne Netzwerk.
    """

    def __init__(self, run_id: int) -> None:
        self._run_id = run_id
        self.run = None
        self.name = "Attrappe"

    async def execute(self) -> None:
        """Scheitert absichtlich - der Fehlerpfad ist hier der interessante."""
        raise RuntimeError("kein echter Testlauf")

    def status(self) -> RunnerStatus:
        return RunnerStatus(
            run_id=self._run_id, run_name="", firmware_version=None, router_model=None,
            elapsed_s=1.0, planned_duration_s=60.0, remaining_s=59.0,
            buffered=0, dropped=0, rows_written=0, tr064_available=False,
            traffic_paused=False, traffic_pause_reason="",
            standby_prevented=True, standby_text="Energiesparmodus unterdrueckt (Attrappe)",
            modules=[], ping={}, traffic={}, wlan={},
        )


def make_context(config: AppConfig, base_dir: Path) -> AppContext:
    """Baut einen Kontext, dessen Arbeitsordner im Testverzeichnis liegt."""
    paths = AppPaths(base_dir / "config.yaml", base_dir, PathOrigin.EXPLICIT)
    return AppContext(paths=paths, config=config)


@pytest.fixture
def context(config: AppConfig, tmp_path: Path) -> AppContext:
    """Ein Kontext mit temporaeren Arbeitsverzeichnissen und echter Datei."""
    (tmp_path / "config.yaml").write_text(CONFIG_FILE, encoding="utf-8")
    return make_context(config, tmp_path)


@pytest.fixture(autouse=True)
def fake_keyring():  # type: ignore[no-untyped-def]
    """Haelt die Tests vom echten Schluesselspeicher des Benutzers fern."""
    set_default_store(SecretStore(FakeBackend()))
    yield
    set_default_store(None)


@pytest.fixture
def client(context: AppContext, tmp_path: Path):  # type: ignore[no-untyped-def]
    """Ein Testclient mit eigener, vorbefuellter Datenbank."""
    db_path = context.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    database = Database(db_path)
    run = database.create_test_run("Lauf A", "{}", "8.02", "FRITZ!Box 7590")
    base = time.time() - 600
    database.insert_measurements(
        run.id,
        [
            Measurement("ping", "rtt_avg", 10.0 + index, "ms",
                        timestamp=base + index * 60, meta={"target": "cloudflare"})
            for index in range(5)
        ]
        + [
            Measurement("ping", "rtt_avg", 3.0, "ms",
                        timestamp=base + index * 60, meta={"target": "fritzbox"})
            for index in range(5)
        ],
    )
    database.insert_events(
        run.id,
        [
            Event(EventType.RUN_START, "Testlauf gestartet", Severity.INFO, timestamp=base),
            Event(EventType.OUTAGE_START, "Ausfall erkannt", Severity.ERROR, timestamp=base + 120),
        ],
    )
    outage_id = database.insert_outage(run.id, Outage(started_at=base + 120,
                                                      scope=OutageScope.WAN))
    database.close_outage(outage_id, base + 180, 60.0, "wan")
    database.finish_test_run(run.id)
    database.close()

    app = create_app(context)
    with TestClient(app) as test_client:
        # Ohne Sitzungs-Token weist der Server jede API-Anfrage ab.
        test_client.headers[TOKEN_HEADER] = app.state.token
        test_client.run_id = run.id  # type: ignore[attr-defined]
        yield test_client


class TestStaticFiles:
    """Die Oberflaeche muss vollstaendig lokal ausgeliefert werden."""

    def test_index_is_served(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "FRITZ!Box-Langzeittest" in response.text

    @pytest.mark.parametrize("name", ["style.css", "app.js", "settings.js", "setup.js"])
    def test_assets_are_served(self, client: TestClient, name: str) -> None:
        assert client.get(f"/static/{name}").status_code == 200

    def test_no_asset_is_shipped_unused(self, client: TestClient) -> None:
        """Jede Datei im Ordner ``static`` muss auch eingebunden sein.

        Eine nicht mehr eingebundene Datei wird trotzdem mitgeliefert und in
        den Bauskripten weiter auf Aktualitaet geprueft - sie sieht also wie
        gepflegter Code aus, waehrend niemand mehr merkt, wenn sie kaputt
        geht. Genau so blieb ``chart.js`` nach dem Entfernen des
        Live-Diagramms liegen.
        """
        html = client.get("/").text
        unused = [
            path.name
            for path in STATIC_DIR.iterdir()
            if path.suffix in {".js", ".css"} and f"/static/{path.name}" not in html
        ]
        assert not unused, f"Wird mitgeliefert, aber nicht eingebunden: {unused}"

    def test_script_order(self, client: TestClient) -> None:
        """settings.js nutzt Helfer aus app.js - die Reihenfolge ist bindend."""
        html = client.get("/").text
        assert html.index("app.js") < html.index("settings.js")

    def test_every_referenced_element_exists(self, client: TestClient) -> None:
        """Jede ``$("id")``-Abfrage im JavaScript muss ein Element treffen.

        Ein Tippfehler liefert dort stillschweigend ``null`` und faellt erst
        auf, wenn ein Benutzer den betroffenen Knopf drueckt - moeglicherweise
        mitten in einem mehrtaegigen Testlauf. Dynamisch zusammengesetzte
        Bezeichner (Template-Literale) bleiben aussen vor.
        """
        html = client.get("/").text
        available = set(re.findall(r'id="([^"]+)"', html))

        for name in ("app.js", "settings.js", "setup.js"):
            script = client.get(f"/static/{name}").text
            for element_id in re.findall(r"""\$\(["']([A-Za-z0-9_-]+)["']\)""", script):
                assert element_id in available, f"{name}: id '{element_id}' fehlt im HTML"

    def test_all_tabs_have_a_panel(self, client: TestClient) -> None:
        """Ein Reiter ohne Inhalt waere ein toter Knopf."""
        html = client.get("/").text
        for tab in ("live", "runs", "check", "settings"):
            assert f'data-tab="{tab}"' in html
            assert f'id="tab-{tab}"' in html

    def test_no_external_resources(self, client: TestClient) -> None:
        """Kein CDN, keine externen Schriften - das Dashboard muss offline laufen.

        Das ist keine Stilfrage: Ein Werkzeug, das Internetausfaelle misst, darf
        zur Anzeige der Messwerte kein Internet benoetigen.
        """
        html = client.get("/").text
        for asset in ("style.css", "app.js", "settings.js", "setup.js"):
            assert f"/static/{asset}" in html

        # Geprueft wird auf tatsaechlich nachgeladene Ressourcen. Die
        # SVG-Namensraum-URI im Favicon zaehlt nicht dazu - sie ist ein reiner
        # Bezeichner und wird nie abgerufen.
        for pattern in ('src="http', "src='http", 'href="http', "href='http", "url(http"):
            assert pattern not in html, f"Externe Ressource gefunden: {pattern}"

        css = client.get("/static/style.css").text
        assert "@import" not in css and "url(http" not in css

    def test_live_view_shows_no_curves(self, client: TestClient) -> None:
        """Der Live-Reiter zeigt Zustaende, keine Verlaeufe.

        Kurven brauchen eine Zeitachse, die zu Beginn eines Laufs noch leer ist
        und danach im Takt der Aggregationsfenster einen Punkt bekommt - das
        laesst sich waehrend der Messung kaum lesen und draengt den aktuellen
        Zustand aus dem Bild. Verlaeufe stehen deshalb ausschliesslich im
        Bericht, wo der ganze Lauf vorliegt.
        """
        html = client.get("/").text
        assert "<canvas" not in html, "Live-Ansicht enthaelt wieder ein Diagramm."

        script = client.get("/static/app.js").text
        assert "/api/series" not in script

    def test_settings_are_navigable(self, client: TestClient) -> None:
        """Die Einstellungen brauchen Menue, Suche und Standardwerte.

        Zehn Abschnitte mit ueber hundert Feldern untereinander sind eine
        Rolle, in der niemand etwas wiederfindet - gesucht wird dann in der
        Rohansicht, wo keine Beschreibung und keine Pruefung mehr hilft.
        """
        html = client.get("/").text
        for element in ("settings-nav", "settings-search", "settings-only-changed"):
            assert f'id="{element}"' in html, f"Bedienelement '{element}' fehlt"

        script = client.get("/static/settings.js").text
        assert "meta.default" in script, "Standardwerte werden nicht mehr ausgewertet"
        assert "DURATION_UNITS" in script, "Dauern erscheinen wieder als nackte Sekunden"


class TestSessionToken:
    """Schutz vor Zugriffen fremder Webseiten auf 127.0.0.1.

    Der Server ist ohne Anmeldung erreichbar. Ohne diesen Schutz koennte jede
    beliebige Seite im selben Browser per JavaScript die Konfiguration auslesen
    oder einen mehrtaegigen Testlauf abbrechen.
    """

    def test_api_without_token_is_refused(self, client: TestClient) -> None:
        response = client.get("/api/status", headers={TOKEN_HEADER: ""})
        assert response.status_code == 401
        assert "Sitzungs-Token" in response.json()["detail"]

    def test_api_with_wrong_token_is_refused(self, client: TestClient) -> None:
        response = client.get("/api/status", headers={TOKEN_HEADER: "geraten"})
        assert response.status_code == 401

    def test_token_as_query_parameter(self, client: TestClient) -> None:
        """Fuer Links, die der Browser selbst oeffnet (Bericht, Vergleich)."""
        token = client.app.state.token
        response = client.get(f"/api/status?token={token}", headers={TOKEN_HEADER: ""})
        assert response.status_code == 200

    def test_page_and_assets_stay_open(self, client: TestClient) -> None:
        """Die Seite selbst braucht kein Token - sie transportiert es ja."""
        assert client.get("/", headers={TOKEN_HEADER: ""}).status_code == 200
        assert client.get("/static/app.js", headers={TOKEN_HEADER: ""}).status_code == 200

    def test_token_is_injected_into_the_page(self, client: TestClient) -> None:
        html = client.get("/").text
        assert TOKEN_PLACEHOLDER not in html, "Der Platzhalter wurde nicht ersetzt"
        assert client.app.state.token in html

    def test_token_changes_per_start(self, context: AppContext) -> None:
        """Ein neuer Programmstart entwertet alte Token."""
        assert create_app(context).state.token != create_app(context).state.token

    def test_api_docs_are_disabled(self, client: TestClient) -> None:
        """Die eingebaute Doku laedt ihre Oberflaeche von einem CDN.

        Das widerspraeche dem Grundsatz, dass dieses Werkzeug ohne Internet
        bedienbar bleibt.
        """
        assert client.get("/api/docs").status_code in {401, 404}


class TestStatus:
    """Statusendpunkt im Leerlauf."""

    def test_idle_status(self, client: TestClient) -> None:
        data = client.get("/api/status").json()
        assert data["running"] is False
        assert data["state"] == "idle"
        assert data["last_result"] is None
        assert "run" not in data

    def test_stop_without_run_is_rejected(self, client: TestClient) -> None:
        response = client.post("/api/run/stop")
        assert response.status_code == 409
        assert "kein Testlauf" in response.json()["detail"]

    def test_invalid_duration_is_rejected(self, client: TestClient) -> None:
        response = client.post("/api/run/start", json={"name": "x", "duration": "quatsch"})
        assert response.status_code == 400
        assert "Ungueltige Dauerangabe" in response.json()["detail"]


class TestData:
    """Datenendpunkte fuer Tabellen und Diagramme."""

    def test_runs_list(self, client: TestClient) -> None:
        runs = client.get("/api/runs").json()
        assert len(runs) == 1
        assert runs[0]["firmware_version"] == "8.02"
        assert runs[0]["outages"] == 1
        assert runs[0]["open"] is False

    def test_events(self, client: TestClient) -> None:
        run_id = client.run_id  # type: ignore[attr-defined]
        events = client.get(f"/api/events?run_id={run_id}").json()
        assert len(events) == 2
        # Neueste zuerst - der Ticker soll oben das Aktuellste zeigen.
        assert events[0]["type"] == "OUTAGE_START"

    def test_series_is_grouped_by_target(self, client: TestClient) -> None:
        run_id = client.run_id  # type: ignore[attr-defined]
        data = client.get(f"/api/series?run_id={run_id}&metric=rtt_avg&minutes=0").json()
        assert set(data["series"]) == {"cloudflare", "fritzbox"}
        assert len(data["series"]["cloudflare"]) == 5
        # Format: [[zeitstempel, wert], ...] - direkt zeichenbar.
        assert len(data["series"]["cloudflare"][0]) == 2

    def test_series_respects_time_window(self, client: TestClient) -> None:
        run_id = client.run_id  # type: ignore[attr-defined]
        data = client.get(f"/api/series?run_id={run_id}&metric=rtt_avg&minutes=1").json()
        assert not data["series"], "Alte Messwerte duerfen nicht im 1-Minuten-Fenster auftauchen"

    def test_empty_series_for_unknown_metric(self, client: TestClient) -> None:
        run_id = client.run_id  # type: ignore[attr-defined]
        data = client.get(f"/api/series?run_id={run_id}&metric=gibtsnicht").json()
        assert data["series"] == {}


class TestLiveConfiguration:
    """Die Endpunkte muessen der Konfiguration folgen, nicht sie kopieren.

    Das ist der Zweck des Laufzeitkontexts: Wer die Einstellungen in der
    Oberflaeche aendert, soll das Ergebnis sofort sehen - ohne Neustart.
    """

    def test_status_reflects_changed_router_host(
        self, client: TestClient, context: AppContext
    ) -> None:
        assert client.get("/api/status").json()["router_host"] == "192.168.178.1"
        context.config.router.host = "10.0.0.1"
        assert client.get("/api/status").json()["router_host"] == "10.0.0.1"

    def test_read_database_follows_changed_data_dir(
        self, client: TestClient, context: AppContext
    ) -> None:
        """Ein anderer Datenordner heisst: andere Datenbank, andere Laeufe.

        Ohne diese Nachfuehrung zeigte die Oberflaeche weiter die Laeufe aus
        der alten Datei an - das sieht fuer den Nutzer wie Datenverlust aus.
        """
        assert len(client.get("/api/runs").json()) == 1

        context.config.storage.data_dir = Path("andere_messwerte")
        assert client.get("/api/runs").json() == []

    def test_report_lands_in_configured_directory(
        self, client: TestClient, context: AppContext
    ) -> None:
        context.config.storage.report_dir = Path("eigene_berichte")
        run_id = client.run_id  # type: ignore[attr-defined]
        result = client.post(f"/api/report/{run_id}").json()
        assert Path(result["path"]).parent == context.base_dir / "eigene_berichte"


class TestOpenRun:
    """Abgebrochener Testlauf: erkennen und entscheiden lassen.

    Ein Lauf ohne ``ended_at`` bedeutet Absturz, Stromausfall oder hartes
    Abwuergen. Frueher blieb er einfach liegen und wurde beim naechsten Start
    stillschweigend fortgesetzt - was falsch ist, wenn zwischen beiden Laeufen
    Tage liegen.
    """

    @pytest.fixture
    def open_run(self, context: AppContext) -> int:
        """Legt einen offenen Testlauf mit ein paar Messwerten an."""
        database = Database(context.db_path)
        run = database.create_test_run("Abgestuerzt", "{}", "8.02", "FRITZ!Box 7590")
        database.insert_measurements(
            run.id,
            [Measurement("ping", "rtt_avg", 12.0, "ms", timestamp=time.time() - 300)],
        )
        database.close()
        return run.id

    def test_no_open_run(self, client: TestClient) -> None:
        assert client.get("/api/run/open").json()["open"] is False

    def test_open_run_is_reported_with_context(self, client: TestClient, open_run: int) -> None:
        """Die Entscheidung braucht Zahlen: wie viel Messzeit steht auf dem Spiel?"""
        data = client.get("/api/run/open").json()
        assert data["open"] is True
        assert data["run"]["id"] == open_run
        assert data["run"]["measurements"] == 1
        assert data["run"]["gap_s"] > 0

    def test_close_keeps_the_data(self, client: TestClient, open_run: int) -> None:
        assert client.post(f"/api/run/{open_run}/close").json()["closed"] is True
        assert client.get("/api/run/open").json()["open"] is False

        runs = {run["id"]: run for run in client.get("/api/runs").json()}
        assert runs[open_run]["open"] is False
        assert runs[open_run]["measurements"] == 1, "Die Messwerte muessen erhalten bleiben"

    def test_close_of_finished_run_is_refused(self, client: TestClient) -> None:
        run_id = client.run_id  # type: ignore[attr-defined]
        assert client.post(f"/api/run/{run_id}/close").status_code == 409

    def test_close_of_unknown_run(self, client: TestClient) -> None:
        assert client.post("/api/run/999/close").status_code == 404

    def test_discard_removes_everything(self, client: TestClient, open_run: int) -> None:
        result = client.delete(f"/api/run/{open_run}").json()
        assert result["deleted"] is True
        assert result["rows"] >= 1
        assert open_run not in [run["id"] for run in client.get("/api/runs").json()]

    def test_discard_of_unknown_run(self, client: TestClient) -> None:
        assert client.delete("/api/run/999").status_code == 404

    def test_discard_is_refused_while_running(self, client: TestClient, open_run: int) -> None:
        """Sonst zoege man dem laufenden Schreibvorgang die Daten unter den Fuessen weg."""
        controller = client.app.state.controller
        controller._state = ControllerState.RUNNING
        controller._runner = _FakeRunner(open_run)

        response = client.delete(f"/api/run/{open_run}")
        assert response.status_code == 409
        assert "laeuft gerade" in response.json()["detail"]


class TestConfigApi:
    """Einstellungen lesen, pruefen und speichern."""

    def test_get_returns_values_and_location(
        self, client: TestClient, context: AppContext
    ) -> None:
        data = client.get("/api/config").json()
        assert data["values"]["router"]["host"] == "192.168.178.1"
        assert data["path"] == str(context.paths.config_file)
        assert data["origin_text"]
        assert data["running"] is False

    def test_password_never_leaves_the_process(
        self, client: TestClient, context: AppContext
    ) -> None:
        context.config.router.password = "geheim"
        body = client.get("/api/config").text
        assert "geheim" not in body
        assert PASSWORD_MASK in body

    def test_schema_is_available_for_the_form(self, client: TestClient) -> None:
        schema = client.get("/api/config/schema").json()
        assert schema["title"] == "AppConfig"

    def test_validate_reports_field_errors(self, client: TestClient) -> None:
        values = client.get("/api/config").json()["values"]
        values["ping"]["interval_s"] = -1

        response = client.post("/api/config/validate", json={"values": values})
        assert response.status_code == 422
        errors = response.json()["detail"]["errors"]
        assert errors[0]["path"] == "ping.interval_s"

    def test_validate_accepts_valid_values(self, client: TestClient) -> None:
        values = client.get("/api/config").json()["values"]
        assert client.post("/api/config/validate", json={"values": values}).json()["valid"]

    def test_save_writes_and_applies(self, client: TestClient, context: AppContext) -> None:
        values = client.get("/api/config").json()["values"]
        values["router"]["host"] = "10.0.0.5"

        result = client.put("/api/config", json={"values": values}).json()
        assert result["saved"] is True
        # Sofort wirksam, ohne Neustart - das ist der Zweck des Laufzeitkontexts.
        assert context.config.router.host == "10.0.0.5"
        assert "10.0.0.5" in context.paths.config_file.read_text(encoding="utf-8")

    def test_save_rejects_invalid_values(self, client: TestClient, context: AppContext) -> None:
        values = client.get("/api/config").json()["values"]
        values["ping"]["targets"] = []

        response = client.put("/api/config", json={"values": values})
        assert response.status_code == 422
        assert context.config.ping.targets, "Die alte Konfiguration muss gelten bleiben"

    def test_raw_returns_the_file_verbatim(self, client: TestClient) -> None:
        assert client.get("/api/config/raw").json()["text"] == CONFIG_FILE

    def test_raw_round_trip(self, client: TestClient, context: AppContext) -> None:
        changed = CONFIG_FILE.replace("host: 192.168.178.1\n\nping", "host: 10.0.0.9\n\nping")

        assert client.put("/api/config/raw", json={"text": changed}).json()["saved"] is True
        assert client.get("/api/config/raw").json()["text"] == changed
        assert context.config.router.host == "10.0.0.9"

    def test_raw_rejects_broken_yaml(self, client: TestClient) -> None:
        response = client.put("/api/config/raw", json={"text": "ping: [offen\n"})
        assert response.status_code == 400
        assert "YAML-Syntaxfehler" in response.json()["detail"]


class TestConfigWhileRunning:
    """Waehrend eines Testlaufs gelten engere Regeln."""

    @pytest.fixture(autouse=True)
    def running(self, client: TestClient) -> None:
        """Versetzt den Controller in den Zustand 'laeuft'."""
        client.app.state.controller._state = ControllerState.RUNNING

    def test_storage_change_is_refused(self, client: TestClient, context: AppContext) -> None:
        """Ein Wechsel des Datenordners mitten im Lauf zerlegt die Messreihe.

        Der Lauf schriebe weiter in die alte Datenbank, die Oberflaeche laese
        aus der neuen - der Lauf saehe aus, als produziere er keine Daten.
        """
        values = client.get("/api/config").json()["values"]
        values["storage"]["data_dir"] = "woanders"

        response = client.put("/api/config", json={"values": values})
        assert response.status_code == 409
        assert "laufenden Testlaufs" in response.json()["detail"]
        assert context.config.storage.data_dir == Path("data")

    def test_other_settings_stay_editable(self, client: TestClient, context: AppContext) -> None:
        """Alles ausser den Speicherorten darf geaendert werden."""
        values = client.get("/api/config").json()["values"]
        values["router"]["host"] = "10.0.0.7"

        assert client.put("/api/config", json={"values": values}).status_code == 200
        assert context.config.router.host == "10.0.0.7"

    def test_status_flag_is_reported(self, client: TestClient) -> None:
        """Die Oberflaeche soll die Sperre erklaeren koennen, bevor sie zuschlaegt."""
        assert client.get("/api/config").json()["running"] is True


class TestSetup:
    """Ersteinrichtung beim allerersten Start."""

    def test_configured_installation(self, client: TestClient) -> None:
        assert client.get("/api/setup/state").json()["configured"] is True

    def test_gateway_suggestion(self, client: TestClient) -> None:
        """Es kommt immer ein Vorschlag - notfalls die Werksadresse."""
        data = client.get("/api/setup/gateway").json()
        assert data["fallback"] == "192.168.178.1"
        assert data["gateway"] or not data["detected"]

    def test_starts_without_configuration(self, config: AppConfig, tmp_path: Path) -> None:
        """Die Oberflaeche muss ohne config.yaml starten koennen.

        Sonst laesst sich das Programm nur einrichten, wenn es bereits
        eingerichtet ist - der Assistent koennte nie laufen.
        """
        context = AppContext.load_or_template(tmp_path / "gibtsnicht.yaml")
        assert context.configured is False
        assert context.config.ping.targets, "Die Vorlage muss benutzbar sein"

        with TestClient(create_app(context)) as client:
            client.headers[TOKEN_HEADER] = client.app.state.token
            state = client.get("/api/setup/state").json()
            assert state["configured"] is False

    def test_saving_completes_the_setup(self, tmp_path: Path) -> None:
        """Nach dem Speichern gilt die Anwendung als eingerichtet."""
        context = AppContext.load_or_template(tmp_path / "config.yaml")
        with TestClient(create_app(context)) as client:
            client.headers[TOKEN_HEADER] = client.app.state.token
            values = client.get("/api/config").json()["values"]
            values["router"]["host"] = "10.0.0.1"

            assert client.put("/api/config", json={"values": values}).status_code == 200
            assert client.get("/api/setup/state").json()["configured"] is True
            assert (tmp_path / "config.yaml").exists()


class TestOpenFiles:
    """Erzeugte Dateien im Betriebssystem oeffnen.

    Berichte gehoeren in den Systembrowser: Das Programmfenster hat keine
    Adressleiste und damit auch keinen Zurueck-Knopf - ein dort geoeffneter
    Bericht ersetzt die Oberflaeche dauerhaft.
    """

    @pytest.fixture
    def opened(self, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
        """Faengt den Aufruf ans Betriebssystem ab."""
        calls: list[Path] = []
        monkeypatch.setattr("fbtest.dashboard.app._open_with_system", calls.append)
        return calls

    def test_opens_a_report(self, client: TestClient, context: AppContext,
                            opened: list[Path]) -> None:
        report = context.report_dir / "bericht.html"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("<html></html>", encoding="utf-8")

        assert client.post("/api/open", json={"path": str(report)}).status_code == 200
        assert opened == [report]

    def test_reveals_the_folder(self, client: TestClient, context: AppContext,
                                opened: list[Path]) -> None:
        report = context.report_dir / "bericht.html"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("x", encoding="utf-8")

        client.post("/api/open", json={"path": str(report), "reveal": True})
        assert opened == [context.report_dir]

    def test_refuses_paths_outside_the_working_directories(
        self, client: TestClient, tmp_path: Path, opened: list[Path]
    ) -> None:
        """Sonst waere die Schnittstelle ein bequemer Programmstarter.

        Der Server hoert auf localhost; ohne diese Pruefung koennte jeder
        lokale Prozess mit dem Token beliebige Dateien ausfuehren lassen.
        """
        outside = tmp_path.parent / "fremd.exe"
        outside.write_text("x", encoding="utf-8")

        response = client.post("/api/open", json={"path": str(outside)})
        assert response.status_code == 403
        assert opened == []

    def test_missing_file(self, client: TestClient, context: AppContext,
                          opened: list[Path]) -> None:
        response = client.post("/api/open", json={"path": str(context.report_dir / "weg.html")})
        assert response.status_code == 404
        assert opened == []


class TestSystemInfo:
    """Diagnosebereich der Einstellungen."""

    def test_reports_paths_and_environment(self, client: TestClient, context: AppContext) -> None:
        data = client.get("/api/system").json()
        assert data["paths"]["database"] == str(context.db_path)
        assert data["paths"]["config"] == str(context.paths.config_file)
        assert data["paths"]["origin"]
        assert data["python"].startswith("3.")

    def test_ping_backend_explains_itself(self, client: TestClient) -> None:
        """Fehlende Rohsocket-Rechte waren bisher nur im Protokoll sichtbar."""
        backend = client.get("/api/system").json()["ping_backend"]
        assert backend["name"] in {"icmplib", "ping"}
        assert backend["precise"] or backend["hint"], "Ein Rueckfall muss erklaert werden"

    def test_reports_keyring_and_standby(self, client: TestClient) -> None:
        data = client.get("/api/system").json()
        assert "available" in data["keyring"]
        assert data["standby"]["backend"]


class TestPasswordApi:
    """Passwort setzen und entfernen - ohne es je zurueckzugeben."""

    def test_state_without_password(self, client: TestClient) -> None:
        data = client.get("/api/secret/password").json()
        assert data["present"] is False
        assert data["source"] == "none"
        assert data["account"] == "(standard)@192.168.178.1"

    def test_set_and_report(self, client: TestClient) -> None:
        result = client.put("/api/secret/password", json={"password": "geheim"}).json()
        assert result["present"] is True
        assert result["source"] == "keyring"
        assert "geheim" not in client.get("/api/secret/password").text

    def test_password_reaches_the_router_configuration(
        self, client: TestClient, context: AppContext
    ) -> None:
        """Der eigentliche Zweck: Der Testlauf muss es anschliessend finden."""
        client.put("/api/secret/password", json={"password": "geheim"})
        assert context.config.router.resolve_password() == "geheim"

    def test_delete(self, client: TestClient, context: AppContext) -> None:
        client.put("/api/secret/password", json={"password": "geheim"})
        assert client.delete("/api/secret/password").json()["removed"] is True
        assert context.config.router.resolve_password() == ""

    def test_empty_password_is_rejected(self, client: TestClient) -> None:
        """Leer bedeutet loeschen - dafuer gibt es DELETE."""
        assert client.put("/api/secret/password", json={"password": ""}).status_code == 400

    def test_account_follows_the_configured_box(
        self, client: TestClient, context: AppContext
    ) -> None:
        """Zwei FRITZ!Boxen duerfen sich nicht das Passwort ueberschreiben."""
        client.put("/api/secret/password", json={"password": "erste"})
        context.config.router.host = "10.0.0.2"
        assert client.get("/api/secret/password").json()["present"] is False


class TestReports:
    """Bericht und Export ueber die Oberflaeche."""

    def test_report_generation_and_view(self, client: TestClient) -> None:
        run_id = client.run_id  # type: ignore[attr-defined]
        result = client.post(f"/api/report/{run_id}").json()
        assert Path(result["path"]).exists()

        view = client.get(result["url"])
        assert view.status_code == 200
        assert "Management-Summary" in view.text

    def test_view_before_generation(self, client: TestClient) -> None:
        assert client.get("/api/report/1/view").status_code in {404, 200}

    def test_report_of_unknown_run(self, client: TestClient) -> None:
        response = client.post("/api/report/999")
        assert response.status_code == 404
        assert "existiert nicht" in response.json()["detail"]

    def test_export(self, client: TestClient) -> None:
        run_id = client.run_id  # type: ignore[attr-defined]
        result = client.post(f"/api/export/{run_id}", json={"format": "both"}).json()
        assert len(result["files"]) == 8
        assert all(Path(path).exists() for path in result["files"])

    def test_export_rejects_unknown_format(self, client: TestClient) -> None:
        run_id = client.run_id  # type: ignore[attr-defined]
        response = client.post(f"/api/export/{run_id}", json={"format": "xml"})
        assert response.status_code == 422


class TestController:
    """Zustandsverwaltung des Controllers (ohne echten Testlauf)."""

    async def test_starts_idle(self, context: AppContext) -> None:
        controller = RunController(context)
        assert controller.state is ControllerState.IDLE
        assert controller.status() is None
        assert controller.last_result is None

    async def test_stop_without_run_is_harmless(self, context: AppContext) -> None:
        controller = RunController(context)
        await controller.stop()
        await controller.shutdown()
        assert controller.state is ControllerState.IDLE

    async def test_observers_are_called(self, context: AppContext) -> None:
        """Tray-Symbol und Systemmeldungen haengen an diesen Rueckrufen."""
        controller = RunController(context)
        started: list[object] = []
        finished: list[LastResult] = []
        controller.on_run_started(started.append)
        controller.on_run_finished(finished.append)

        controller._runner = _FakeRunner(1)  # type: ignore[assignment]
        with contextlib.suppress(Exception):
            await controller._execute()

        assert started == [controller._runner]
        assert finished and finished[0].error

    async def test_broken_observer_does_not_break_the_run(self, context: AppContext) -> None:
        """Ein defektes Tray-Symbol darf keinen Testlauf zum Absturz bringen."""
        controller = RunController(context)

        def explode(_: object) -> None:
            raise RuntimeError("Tray kaputt")

        controller.on_run_started(explode)
        controller._runner = _FakeRunner(1)  # type: ignore[assignment]
        with contextlib.suppress(Exception):
            await controller._execute()

        assert controller.state is ControllerState.IDLE
