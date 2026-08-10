"""FastAPI-Anwendung des Dashboards.

Aufteilung der Datenquellen - bewusst zweigleisig:

* **Live-Zustand** (Ping-Latenz, Modulzustand, Restlaufzeit) kommt direkt aus den
  Modulinstanzen im Arbeitsspeicher. Er ist damit sekundenaktuell, unabhaengig
  davon, wann der Writer das naechste Mal in die Datenbank schreibt.
* **Verlaufsdaten** (Diagramme, Ereignisse, frueher Testlaeufe) kommen aus der
  Datenbank. Weil SQLite im WAL-Modus laeuft, stoert das Lesen den laufenden
  Schreibvorgang nicht.

Das Frontend ist bewusst reines HTML/CSS/JS ohne Build-Werkzeuge und ohne CDN:
Es soll auch ohne Internetverbindung funktionieren - was bei einem Werkzeug, das
Internetausfaelle misst, keine akademische Anforderung ist.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from hmac import compare_digest
from pathlib import Path
from secrets import token_urlsafe
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from fbtest import __version__
from fbtest.checks import Checker
from fbtest.config import ConfigError, format_duration, parse_duration
from fbtest.config_service import (
    ConfigValidationError,
    SaveResult,
    json_schema,
    read_raw,
    save,
    save_raw,
    to_form_values,
    validate,
    validate_raw,
)
from fbtest.context import AppContext
from fbtest.core.models import utc_now
from fbtest.dashboard.controller import ControllerState, RunController
from fbtest.modules.ping_monitor import select_backend
from fbtest.network import detect_default_gateway
from fbtest.paths import is_frozen, resource_path
from fbtest.power import PowerKeeper
from fbtest.report.generator import render_comparison, render_report
from fbtest.secrets_store import SecretStoreError, account_name, default_store
from fbtest.storage.database import Database
from fbtest.storage.exporter import export_csv, export_json
from fbtest.storage.workbook import export_workbook

log = logging.getLogger(__name__)

#: Kopfzeile, in der die Oberflaeche das Sitzungs-Token mitsendet.
TOKEN_HEADER = "X-Fbtest-Token"

#: Platzhalter in ``index.html``, der beim Ausliefern ersetzt wird.
TOKEN_PLACEHOLDER = "__FBTEST_TOKEN__"

#: Ordner der Web-Oberflaeche. Ueber :func:`resource_path`, damit die Dateien
#: auch in der gebuendelten Anwendung gefunden werden - dort liegen sie nicht
#: neben diesem Modul, sondern im Entpackverzeichnis von PyInstaller.
STATIC_DIR = resource_path("dashboard", "static")


# --------------------------------------------------------------------------
# Anfrage-Modelle
# --------------------------------------------------------------------------


class StartRequest(BaseModel):
    """Anfrage zum Starten eines Testlaufs."""

    name: str = Field(default="", description="Name des Laufs, z.B. die Firmware-Version")
    duration: str = Field(default="24h", description="Laufzeit, z.B. '30m', '24h', '3d'")
    resume: bool | None = Field(default=None, description="Offenen Testlauf fortsetzen")


class ExportRequest(BaseModel):
    """Anfrage zum Exportieren eines Testlaufs."""

    format: str = Field(default="xlsx", pattern="^(xlsx|csv|json|both|all)$")


class ConfigRequest(BaseModel):
    """Anfrage zum Speichern der Konfiguration aus dem Formular."""

    values: dict[str, Any] = Field(description="Vollstaendige Konfiguration als Objekt")


class RawConfigRequest(BaseModel):
    """Anfrage zum Speichern der Rohansicht."""

    text: str = Field(description="Vollstaendiger YAML-Inhalt")


class OpenRequest(BaseModel):
    """Anfrage zum Oeffnen einer erzeugten Datei."""

    path: str = Field(description="Vollstaendiger Pfad innerhalb der Arbeitsordner")
    reveal: bool = Field(default=False, description="Den Ordner statt der Datei oeffnen")


class PasswordRequest(BaseModel):
    """Anfrage zum Hinterlegen des FRITZ!Box-Passworts."""

    password: str = Field(description="Das Passwort im Klartext - wird nur weitergereicht")


# --------------------------------------------------------------------------
# Anwendung
# --------------------------------------------------------------------------


def create_app(context: AppContext) -> FastAPI:
    """Erzeugt die Dashboard-Anwendung.

    Args:
        context: Laufzeitkontext mit Konfiguration und Pfaden. Alle Endpunkte
            lesen ihn bei jedem Aufruf neu, damit geaenderte Einstellungen
            sofort wirken.

    Returns:
        Die fertig verdrahtete FastAPI-Anwendung.
    """
    controller = RunController(context)
    # Eigene Verbindung fuers Lesen. Der laufende Testlauf hat seine eigene -
    # dank WAL-Modus stoeren sich beide nicht.
    db_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Oeffnet die Lesedatenbank und raeumt beim Beenden auf."""
        _open_database()
        log.info("Dashboard bereit - Datenbank: %s", context.db_path)
        try:
            yield
        finally:
            await controller.shutdown()
            app.state.db.close()

    def _open_database() -> Database:
        """Oeffnet die Lesedatenbank am aktuell konfigurierten Ort."""
        path = context.db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        database = Database(path)
        app.state.db = database
        app.state.db_path = path
        return database

    def read_db() -> Database:
        """Liefert die Lesedatenbank und folgt dabei geaenderten Speicherorten.

        Wird ``storage.data_dir`` in den Einstellungen umgestellt, zeigt die
        offene Verbindung noch auf die alte Datei. Ohne diese Pruefung wuerde
        die Oberflaeche stillschweigend die falschen Daten anzeigen - der
        unangenehmste Fehler ueberhaupt, weil er wie ein Datenverlust aussieht.
        """
        if app.state.db_path != context.db_path:
            log.info("Datenbankpfad geaendert: %s -> %s", app.state.db_path, context.db_path)
            app.state.db.close()
            return _open_database()
        db: Database = app.state.db
        return db

    app = FastAPI(
        title="FRITZ!Box-Langzeittest",
        version=__version__,
        lifespan=lifespan,
        # Die eingebaute API-Dokumentation laedt ihre Oberflaeche von einem CDN
        # und widerspraeche damit dem Grundsatz, dass dieses Werkzeug ohne
        # Internet bedienbar bleibt. Die Schnittstelle ist im Quelltext
        # dokumentiert.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # Ein Zufallswert je Programmstart. Er schuetzt nicht vor dem Benutzer -
    # sondern davor, dass eine beliebige Webseite im Browser des Benutzers per
    # JavaScript auf 127.0.0.1 zugreift: Sie koennte sonst die Konfiguration
    # auslesen oder einen mehrtaegigen Testlauf abbrechen. Die Oberflaeche
    # bekommt den Wert beim Laden der Seite; fremde Seiten koennen ihn wegen
    # der Same-Origin-Policy nicht lesen.
    session_token = token_urlsafe(32)
    app.state.token = session_token

    @app.middleware("http")
    async def require_token(request: Request, call_next: Any) -> Response:
        """Laesst nur Anfragen mit gueltigem Sitzungs-Token an die API."""
        if not request.url.path.startswith("/api/"):
            response: Response = await call_next(request)
            return response

        supplied = request.headers.get(TOKEN_HEADER) or request.query_params.get("token") or ""
        if not compare_digest(supplied, session_token):
            return JSONResponse(
                status_code=401,
                content={
                    "detail": (
                        "Ungueltiges oder fehlendes Sitzungs-Token. "
                        "Die Oberflaeche unter http://127.0.0.1 neu laden."
                    )
                },
            )
        allowed: Response = await call_next(request)
        return allowed

    # Der Controller haengt am App-Objekt, damit die spaetere Desktop-Schale
    # (Tray-Symbol, Benachrichtigungen) den Laufzustand abfragen kann, ohne
    # den Umweg ueber die HTTP-Schnittstelle zu nehmen.
    app.state.controller = controller

    async def read(function: Any, *args: Any) -> Any:
        """Fuehrt eine Datenbankabfrage im Worker-Thread aus.

        SQLite-Aufrufe blockieren; ohne diesen Umweg wuerde eine langsame
        Abfrage die gesamte Ereignisschleife und damit auch den laufenden
        Testlauf ausbremsen.
        """
        async with db_lock:
            return await asyncio.to_thread(function, *args)

    # -- Oberflaeche -------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        """Liefert die Oberflaeche aus - mit eingesetztem Sitzungs-Token.

        Das Token steht bewusst in der Seite und nicht in einem Cookie: Ein
        Cookie wuerde der Browser auch bei Anfragen fremder Seiten mitsenden,
        womit der Schutz wirkungslos waere.
        """
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace(TOKEN_PLACEHOLDER, session_token))

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # -- Status ------------------------------------------------------------

    @app.get("/api/status")
    async def api_status() -> dict[str, Any]:
        """Liefert den kompletten Live-Zustand fuer die Oberflaeche."""
        status = controller.status()
        payload: dict[str, Any] = {
            "server_time": utc_now(),
            "state": str(controller.state),
            "version": __version__,
            "router_host": context.config.router.host,
            "running": status is not None,
            "last_result": asdict(controller.last_result) if controller.last_result else None,
        }
        if status is None:
            return payload

        payload["run"] = {
            **asdict(status),
            "elapsed_text": format_duration(status.elapsed_s),
            "remaining_text": format_duration(status.remaining_s),
            "progress_pct": (
                min(100.0, 100.0 * status.elapsed_s / status.planned_duration_s)
                if status.planned_duration_s
                else 0.0
            ),
        }

        # Ergaenzende Angaben aus der Datenbank: laufender Ausfall, letzter
        # Speedtest, juengste Router-Telemetrie.
        if status.run_id is not None:
            database = read_db()
            outage = await read(database.fetch_open_outage, status.run_id)
            speedtest = await read(database.fetch_latest_speedtest, status.run_id)
            router = await read(database.fetch_latest_router_status, status.run_id)
            payload["outage"] = _row(outage)
            payload["speedtest"] = _row(speedtest)
            payload["router"] = _row(router)
        return payload

    @app.get("/api/events")
    async def api_events(run_id: int, limit: int = 40) -> list[dict[str, Any]]:
        """Liefert die juengsten Ereignisse eines Testlaufs (Ticker)."""
        database = read_db()
        rows = await read(database.fetch_latest_events, run_id, min(limit, 200))
        return [_row(row) or {} for row in rows]

    @app.get("/api/series")
    async def api_series(
        run_id: int, module: str = "ping", metric: str = "rtt_avg", minutes: int = 60
    ) -> dict[str, Any]:
        """Liefert eine Messreihe, gruppiert nach Ziel bzw. Profil."""
        database = read_db()
        since = utc_now() - minutes * 60 if minutes > 0 else 0.0
        rows = await read(database.fetch_series, run_id, module, metric, since)

        grouped: dict[str, list[list[float]]] = {}
        for row in rows:
            label = "gesamt"
            if row["meta_json"]:
                try:
                    meta = json.loads(row["meta_json"])
                    label = str(meta.get("target") or meta.get("profile") or meta.get("band")
                                or "gesamt")
                except (ValueError, TypeError):
                    pass
            grouped.setdefault(label, []).append([float(row["ts"]), float(row["value"])])
        return {"metric": metric, "series": grouped}

    # -- Testlaeufe --------------------------------------------------------

    @app.get("/api/runs")
    async def api_runs() -> list[dict[str, Any]]:
        """Listet alle gespeicherten Testlaeufe."""
        database = read_db()
        runs = await read(database.list_test_runs)
        result = []
        for item in runs:
            measurements = await read(database.count_rows, "measurements", item.id)
            outages = await read(database.count_rows, "outages", item.id)
            result.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "firmware_version": item.firmware_version,
                    "router_model": item.router_model,
                    "started_at": item.started_at,
                    "ended_at": item.ended_at,
                    "duration_text": format_duration(item.duration_s),
                    "measurements": measurements,
                    "outages": outages,
                    "open": item.ended_at is None,
                }
            )
        return result

    @app.post("/api/run/start")
    async def api_start(request: StartRequest) -> dict[str, Any]:
        """Startet einen Testlauf."""
        try:
            duration_s = parse_duration(request.duration)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            run_id = await controller.start(request.name, duration_s, request.resume)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        return {"run_id": run_id, "duration_s": duration_s}

    @app.get("/api/run/open")
    async def api_open_run() -> dict[str, Any]:
        """Meldet einen abgebrochenen Testlauf zur Entscheidung.

        Ein Lauf ohne ``ended_at`` bedeutet: Das Programm wurde nicht geordnet
        beendet - Absturz, Stromausfall oder hartes Abwuergen. Die Oberflaeche
        legt die drei Moeglichkeiten vor (fortsetzen, als beendet markieren,
        verwerfen), statt sie stillschweigend zu waehlen.
        """
        if controller.state is not ControllerState.IDLE:
            return {"open": False, "reason": "Es laeuft bereits ein Testlauf."}

        database = read_db()
        run = await read(database.get_open_test_run)
        if run is None:
            return {"open": False}

        last = await read(database.last_activity, run.id)
        return {
            "open": True,
            "run": {
                "id": run.id,
                "name": run.name,
                "started_at": run.started_at,
                "firmware_version": run.firmware_version,
                "measurements": await read(database.count_rows, "measurements", run.id),
                "outages": await read(database.count_rows, "outages", run.id),
                "last_activity": last,
                "gap_s": (utc_now() - last) if last else None,
            },
        }

    @app.post("/api/run/{run_id}/close")
    async def api_close_run(run_id: int) -> dict[str, Any]:
        """Markiert einen offenen Testlauf als beendet.

        Die Messdaten bleiben erhalten und sind auswertbar; nur weitergefuehrt
        wird der Lauf nicht mehr.
        """
        database = read_db()
        run = await read(database.get_test_run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Testlauf #{run_id} existiert nicht.")
        if run.ended_at is not None:
            raise HTTPException(status_code=409, detail="Der Testlauf ist bereits beendet.")

        await read(database.close_dangling_outages, run_id)
        await read(database.finish_test_run, run_id)
        return {"closed": True, "run_id": run_id}

    @app.delete("/api/run/{run_id}")
    async def api_delete_run(run_id: int) -> dict[str, Any]:
        """Loescht einen Testlauf mitsamt allen Messdaten."""
        if controller.state is not ControllerState.IDLE:
            status = controller.status()
            if status is not None and status.run_id == run_id:
                raise HTTPException(
                    status_code=409,
                    detail="Dieser Testlauf laeuft gerade. Zuerst beenden, dann loeschen.",
                )

        database = read_db()
        run = await read(database.get_test_run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Testlauf #{run_id} existiert nicht.")

        removed = await read(database.delete_test_run, run_id)
        log.info("Testlauf #%s geloescht (%s Datenzeilen).", run_id, removed)
        return {"deleted": True, "run_id": run_id, "rows": removed}

    @app.post("/api/run/stop")
    async def api_stop() -> dict[str, Any]:
        """Beendet den laufenden Testlauf geordnet."""
        if controller.state is ControllerState.IDLE:
            raise HTTPException(status_code=409, detail="Es laeuft kein Testlauf.")
        await controller.stop()
        return {"stopped": True}

    # -- Auswertung --------------------------------------------------------

    @app.post("/api/report/{run_id}")
    async def api_report(run_id: int) -> dict[str, Any]:
        """Erzeugt den HTML-Bericht eines Testlaufs."""
        database = read_db()
        target = context.report_dir
        try:
            path = await read(render_report, database, run_id, target)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"path": str(path), "url": f"/api/report/{run_id}/view"}

    @app.get("/api/report/{run_id}/view", response_class=HTMLResponse)
    async def api_report_view(run_id: int) -> FileResponse:
        """Zeigt einen bereits erzeugten Bericht an."""
        target = context.report_dir
        path = target / f"bericht_run{run_id:04d}.html"
        if not path.exists():
            raise HTTPException(status_code=404, detail="Bericht wurde noch nicht erzeugt.")
        return FileResponse(path)

    @app.post("/api/compare/{run_a}/{run_b}")
    async def api_compare(run_a: int, run_b: int) -> dict[str, Any]:
        """Erzeugt den Vergleichsbericht zweier Testlaeufe."""
        database = read_db()
        target = context.report_dir
        try:
            path = await read(render_comparison, database, run_a, run_b, target)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"path": str(path), "url": f"/api/compare/{run_a}/{run_b}/view"}

    @app.get("/api/compare/{run_a}/{run_b}/view", response_class=HTMLResponse)
    async def api_compare_view(run_a: int, run_b: int) -> FileResponse:
        """Zeigt einen bereits erzeugten Vergleichsbericht an."""
        target = context.report_dir
        path = target / f"vergleich_run{run_a:04d}_run{run_b:04d}.html"
        if not path.exists():
            raise HTTPException(status_code=404, detail="Vergleich wurde noch nicht erzeugt.")
        return FileResponse(path)

    @app.post("/api/export/{run_id}")
    async def api_export(run_id: int, request: ExportRequest) -> dict[str, Any]:
        """Exportiert einen Testlauf als Arbeitsmappe, CSV und/oder JSON."""
        database = read_db()
        target = context.export_dir
        written: list[str] = []
        try:
            if request.format in {"xlsx", "all"}:
                written.append(str(await read(export_workbook, database, run_id, target)))
            if request.format in {"csv", "both", "all"}:
                written.extend(str(p) for p in await read(export_csv, database, run_id, target))
            if request.format in {"json", "both", "all"}:
                written.append(str(await read(export_json, database, run_id, target)))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"files": written, "directory": str(target)}

    # -- Einstellungen -----------------------------------------------------

    def _apply(result: SaveResult) -> dict[str, Any]:
        """Uebernimmt eine gespeicherte Konfiguration und baut die Antwort."""
        context.apply(result.config)
        context.ensure_directories()
        return {
            "saved": True,
            "path": str(result.path),
            "backup": str(result.backup) if result.backup else None,
            "warnings": result.warnings,
        }

    def _reject_storage_change_while_running(values: dict[str, Any]) -> None:
        """Verhindert das Umlegen der Speicherorte waehrend eines Testlaufs.

        Der laufende Testlauf schreibt weiter in die alte Datenbank, die
        Oberflaeche laese aus der neuen - das Ergebnis waere ein Lauf, der
        scheinbar keine Daten produziert. Alle uebrigen Einstellungen duerfen
        geaendert werden; sie gelten dann ab dem naechsten Lauf.

        Raises:
            HTTPException: 409, wenn waehrend eines Laufs Speicherorte
                geaendert werden sollen.
        """
        if controller.state is ControllerState.IDLE:
            return
        current = context.config.model_dump(mode="json")["storage"]
        wanted = values.get("storage")
        if isinstance(wanted, dict) and wanted != current:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Speicherorte lassen sich waehrend eines laufenden Testlaufs nicht "
                    "aendern. Alle uebrigen Einstellungen sind aenderbar und gelten ab "
                    "dem naechsten Lauf."
                ),
            )

    def _validation_error(exc: ConfigValidationError) -> HTTPException:
        """Baut eine Antwort, die das Formular feldgenau auswerten kann."""
        return HTTPException(
            status_code=422,
            detail={
                "message": "Die Konfiguration enthaelt Fehler.",
                "errors": [
                    {"path": item.path, "message": item.message, "kind": item.kind}
                    for item in exc.errors
                ],
            },
        )

    @app.get("/api/config")
    async def api_config() -> dict[str, Any]:
        """Liefert die aktuelle Konfiguration fuer das Formular."""
        return {
            "values": to_form_values(context.config),
            "path": str(context.paths.config_file),
            "origin": str(context.paths.origin),
            "origin_text": context.paths.describe(),
            "exists": context.paths.exists,
            "running": controller.state is not ControllerState.IDLE,
        }

    @app.get("/api/config/schema")
    async def api_config_schema() -> dict[str, Any]:
        """Liefert das JSON-Schema, aus dem die Oberflaeche ihr Formular baut."""
        return json_schema()

    @app.post("/api/config/validate")
    async def api_config_validate(request: ConfigRequest) -> dict[str, Any]:
        """Prueft Formularwerte, ohne zu speichern."""
        try:
            validate(request.values)
        except ConfigValidationError as exc:
            raise _validation_error(exc) from exc
        return {"valid": True}

    @app.put("/api/config")
    async def api_config_save(request: ConfigRequest) -> dict[str, Any]:
        """Speichert die Konfiguration unter Erhalt der Kommentare."""
        _reject_storage_change_while_running(request.values)
        try:
            result = await asyncio.to_thread(
                save,
                context.paths,
                request.values,
                keep_password=context.config.router.password,
            )
        except ConfigValidationError as exc:
            raise _validation_error(exc) from exc
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _apply(result)

    @app.get("/api/config/raw")
    async def api_config_raw() -> dict[str, Any]:
        """Liefert die Konfigurationsdatei im Original (Rohansicht)."""
        try:
            return {"text": read_raw(context.paths), "path": str(context.paths.config_file)}
        except ConfigError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.put("/api/config/raw")
    async def api_config_save_raw(request: RawConfigRequest) -> dict[str, Any]:
        """Speichert die Rohansicht unveraendert - nach erfolgreicher Pruefung."""
        try:
            # Erst pruefen, dann die Speicherort-Sperre anwenden: Ohne geparste
            # Werte laesst sich nicht feststellen, ob storage.* geaendert wurde.
            parsed = validate_raw(request.text)
            _reject_storage_change_while_running(parsed.model_dump(mode="json"))
            result = await asyncio.to_thread(save_raw, context.paths, request.text)
        except ConfigValidationError as exc:
            raise _validation_error(exc) from exc
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _apply(result)

    # -- Passwort ----------------------------------------------------------

    def _account() -> str:
        """Schluessel des Passworts zur aktuell konfigurierten Box."""
        return account_name(context.config.router.host, context.config.router.username)

    @app.get("/api/secret/password")
    async def api_password_state() -> dict[str, Any]:
        """Meldet, ob und woher ein Passwort vorliegt - niemals welches."""
        resolved = context.config.router.resolve_password_source()
        store = default_store()
        return {
            "source": str(resolved.source),
            "source_text": resolved.source.describe(),
            "present": bool(resolved),
            "account": _account(),
            "keyring_available": store.available,
            "keyring_name": store.name,
            "keyring_error": store.error,
            "env_variable": context.config.router.password_env,
        }

    @app.put("/api/secret/password")
    async def api_password_set(request: PasswordRequest) -> dict[str, Any]:
        """Legt das Passwort im Schluesselspeicher des Systems ab."""
        if not request.password:
            raise HTTPException(status_code=400, detail="Das Passwort darf nicht leer sein.")
        try:
            await asyncio.to_thread(default_store().set, _account(), request.password)
        except SecretStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return await api_password_state()

    @app.delete("/api/secret/password")
    async def api_password_delete() -> dict[str, Any]:
        """Entfernt das Passwort aus dem Schluesselspeicher."""
        removed = await asyncio.to_thread(default_store().delete, _account())
        return {**await api_password_state(), "removed": removed}

    # -- Dateien oeffnen ---------------------------------------------------

    def _allowed_roots() -> list[Path]:
        """Ordner, deren Inhalt geoeffnet werden darf."""
        return [context.report_dir, context.export_dir, context.log_dir, context.data_dir]

    @app.post("/api/open")
    async def api_open(request: OpenRequest) -> dict[str, Any]:
        """Oeffnet eine erzeugte Datei oder ihren Ordner im Betriebssystem.

        Berichte gehoeren in den Systembrowser, nicht in das Programmfenster:
        Ein Fenster ohne Adressleiste hat auch keinen Zurueck-Knopf. Wer dort
        einen Bericht oeffnet, kommt nicht mehr zur Oberflaeche zurueck.

        Geoeffnet wird ausschliesslich innerhalb der eigenen Arbeitsordner -
        die Schnittstelle ist sonst ein bequemer Weg, beliebige Programme auf
        diesem Rechner zu starten.
        """
        target = Path(request.path).resolve()
        if not any(target.is_relative_to(root.resolve()) for root in _allowed_roots()):
            raise HTTPException(
                status_code=403,
                detail="Nur Dateien aus den Arbeitsordnern koennen geoeffnet werden.",
            )
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"Nicht gefunden: {target}")

        opened = target.parent if request.reveal else target
        await asyncio.to_thread(_open_with_system, opened)
        return {"opened": str(opened)}

    # -- Fenster -----------------------------------------------------------

    #: Wird von der Desktop-Schale gesetzt, um ihr Fenster nach vorn zu holen.
    app.state.on_show = None

    @app.post("/api/window/show")
    async def api_window_show() -> dict[str, Any]:
        """Holt das Programmfenster nach vorn.

        Der zweite Start des Programms ruft das auf, statt ein weiteres Fenster
        zu oeffnen: Zwei parallele Testlaeufe auf einem Geraet wuerden sich die
        Bandbreite teilen und beide Messreihen unbrauchbar machen.
        """
        callback = app.state.on_show
        if callback is None:
            raise HTTPException(status_code=404, detail="Kein Programmfenster vorhanden.")
        callback()
        return {"shown": True}

    # -- Ersteinrichtung ---------------------------------------------------

    @app.get("/api/setup/state")
    async def api_setup_state() -> dict[str, Any]:
        """Meldet, ob die Anwendung bereits eingerichtet ist."""
        return {
            "configured": context.configured,
            "config_path": str(context.paths.config_file),
            "origin_text": context.paths.describe(),
            "base_dir": str(context.base_dir),
            "has_runs": context.db_path.exists(),
        }

    @app.get("/api/setup/gateway")
    async def api_setup_gateway() -> dict[str, Any]:
        """Schlaegt die IP-Adresse der FRITZ!Box vor.

        Das Standard-Gateway ist der Router, ueber den dieser Rechner ins
        Internet geht - im Heimnetz also die Box selbst. Das ist verlaesslicher
        als die Werksadresse zu raten, die in einem geaenderten Adressbereich
        ins Leere fuehrt.
        """
        gateway = await asyncio.to_thread(detect_default_gateway)
        return {
            "gateway": gateway,
            "fallback": "192.168.178.1",
            "detected": gateway is not None,
        }

    # -- Diagnose ----------------------------------------------------------

    @app.get("/api/system")
    async def api_system() -> dict[str, Any]:
        """Liefert Angaben zur Umgebung fuer den Diagnosebereich.

        Beantwortet die Fragen, die bei einer Fehlersuche zuerst kommen: Wo
        liegen die Daten? Womit wird gemessen? Warum ist etwas eingeschraenkt?
        """
        backend = await select_backend(context.config.ping.prefer_icmplib)
        store = default_store()
        power = PowerKeeper(enabled=context.config.run.prevent_standby)
        return {
            "version": __version__,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "frozen": is_frozen(),
            "paths": {
                "config": str(context.paths.config_file),
                "origin": context.paths.describe(),
                "base": str(context.base_dir),
                "database": str(context.db_path),
                "reports": str(context.report_dir),
                "exports": str(context.export_dir),
                "logs": str(context.log_dir),
            },
            "ping_backend": {
                "name": backend.name,
                "precise": backend.name == "icmplib",
                # Ohne Rohsocket-Rechte weicht das System auf den Systembefehl
                # 'ping' aus. Das funktioniert, misst aber groeber - und war
                # bisher nur im Protokoll sichtbar.
                "hint": (
                    ""
                    if backend.name == "icmplib"
                    else (
                        "Fuer praezisere Latenzmessung fehlen die Rechte fuer Rohsockets. "
                        "Windows: Programm als Administrator starten. "
                        "Linux: sudo setcap cap_net_raw+ep $(which python3). "
                        "Der Testlauf funktioniert auch ohne."
                    )
                ),
            },
            "keyring": {
                "available": store.available,
                "name": store.name,
                "error": store.error,
            },
            "standby": {
                "enabled": context.config.run.prevent_standby,
                "backend": power.backend.description,
            },
        }

    # -- Vorabpruefung -----------------------------------------------------

    @app.post("/api/check")
    async def api_check() -> dict[str, Any]:
        """Fuehrt die Vorabpruefung aus (dieselbe wie ``fbtest check``)."""
        checker = Checker(context.config, context.base_dir, context.db_path)
        results = await checker.run_all()
        return {
            "worst": str(checker.worst),
            "results": [
                {
                    "name": item.name,
                    "status": str(item.status),
                    "message": item.message,
                    "hint": item.hint,
                }
                for item in results
            ],
        }

    @app.exception_handler(Exception)
    async def unhandled(_: Any, exc: Exception) -> JSONResponse:
        """Faengt unerwartete Fehler ab, damit das Dashboard nicht stumm bleibt."""
        log.exception("Unerwarteter Fehler im Dashboard.")
        return JSONResponse(
            status_code=500,
            content={"detail": f"Unerwarteter Fehler: {type(exc).__name__}: {exc}"},
        )

    return app


def _open_with_system(target: Path) -> None:
    """Uebergibt einen Pfad an das Betriebssystem.

    Blockierend - der Aufrufer fuehrt das in einem Worker-Thread aus.
    """
    if sys.platform == "win32":
        os.startfile(target)
    elif sys.platform == "darwin":
        subprocess.run(["open", str(target)], check=False)
    else:
        subprocess.run(["xdg-open", str(target)], check=False)


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Wandelt eine Datenbankzeile in ein JSON-taugliches Dictionary."""
    return dict(row) if row is not None else None
