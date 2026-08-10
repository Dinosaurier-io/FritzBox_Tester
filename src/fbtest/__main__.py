"""Kommandozeile des Testsystems (``fbtest``).

Aufbau bewusst als Subcommands: Ein Testlauf besteht aus mehreren Schritten
(pruefen, laufen lassen, exportieren, auswerten), die unabhaengig voneinander
wiederholbar sein muessen.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import sys
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from fbtest import __version__
from fbtest.checks import Checker, Status
from fbtest.config import ConfigError, format_duration, load_config, parse_duration
from fbtest.context import AppContext
from fbtest.logging_setup import setup_logging
from fbtest.paths import example_config_path, resolve_paths
from fbtest.report.generator import render_comparison, render_report
from fbtest.runner import TestRunner
from fbtest.storage.database import Database
from fbtest.storage.exporter import export_csv, export_json
from fbtest.storage.workbook import export_workbook

console = Console()
log = logging.getLogger(__name__)

app = typer.Typer(
    name="fbtest",
    help="Automatisiertes Langzeit-Testsystem fuer FRITZ!Box-Router.",
    no_args_is_help=True,
    add_completion=False,
)

ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        "-c",
        help="Pfad zur Konfigurationsdatei. Ohne Angabe wird sie automatisch gesucht.",
    ),
]


# --------------------------------------------------------------------------
# Gemeinsame Hilfsfunktionen
# --------------------------------------------------------------------------


def _context(config_option: Path | None, *, allow_missing: bool = False) -> AppContext:
    """Laedt Konfiguration und Pfade, bricht bei Fehlern verstaendlich ab.

    Args:
        config_option: Wert von ``--config``.
        allow_missing: Fehlende Datei zulassen. Das Dashboard setzt ``True``,
            weil dort der Einrichtungsassistent laeuft; die uebrigen Befehle
            sollen dagegen deutlich sagen, dass etwas fehlt.
    """
    paths = resolve_paths(config_option)
    try:
        if allow_missing:
            return AppContext.load_or_template(config_option)
        return AppContext(paths=paths, config=load_config(paths.config_file))
    except ConfigError as exc:
        console.print(f"[bold red]Konfigurationsfehler[/bold red]\n{exc}")
        # Ohne diese Zeile bleibt bei mehreren moeglichen Speicherorten unklar,
        # welche Datei ueberhaupt gesucht wurde.
        console.print(f"[dim]Gesucht in: {paths.config_file} ({paths.describe()})[/dim]")
        raise typer.Exit(code=2) from exc


def _open_db(config_option: Path | None) -> tuple[AppContext, Database]:
    """Laedt die Konfiguration und oeffnet die Datenbank."""
    context = _context(config_option)
    context.ensure_directories()
    if not context.db_path.exists():
        console.print(f"[yellow]Noch keine Datenbank vorhanden ({context.db_path}).[/yellow]")
    return context, Database(context.db_path)


# --------------------------------------------------------------------------
# fbtest init
# --------------------------------------------------------------------------


@app.command()
def init(
    config: ConfigOption = None,
    force: Annotated[
        bool, typer.Option("--force", help="Vorhandene config.yaml ueberschreiben.")
    ] = False,
) -> None:
    """Legt Ordnerstruktur und eine kommentierte Beispielkonfiguration an."""
    paths = resolve_paths(config)
    base = paths.base_dir
    console.print(f"Arbeitsordner: [cyan]{base}[/cyan] ({paths.describe()})\n")

    for folder in ("data", "exports", "reports", "logs"):
        (base / folder).mkdir(parents=True, exist_ok=True)
        console.print(f"  Ordner bereit: [cyan]{folder}/[/cyan]")

    example = example_config_path()
    if not example.exists():
        console.print(
            f"[red]Vorlage nicht gefunden:[/red] {example}\n"
            "Die Datei gehoert zum Paket - vermutlich ist die Installation unvollstaendig."
        )
        raise typer.Exit(code=1)

    target = paths.config_file
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not force:
        console.print(
            f"[yellow]{target.name} existiert bereits[/yellow] - unveraendert gelassen. "
            "Mit [bold]--force[/bold] ueberschreiben."
        )
    else:
        shutil.copyfile(example, target)
        console.print(f"  Konfiguration angelegt: [cyan]{target}[/cyan]")

    console.print(
        Panel(
            f"1. [bold]{target}[/bold] anpassen (IP der FRITZ!Box, Benutzername, Ping-Ziele)\n"
            "2. Passwort als Umgebungsvariable setzen, damit es nicht in der Datei steht:\n"
            "   [cyan]$env:FRITZ_PASSWORD = 'dein-passwort'[/cyan]   (PowerShell)\n"
            "   [cyan]export FRITZ_PASSWORD='dein-passwort'[/cyan]   (Linux)\n"
            "3. [bold]fbtest check[/bold] ausfuehren\n"
            "4. [bold]fbtest run --duration 24h --name \"FRITZ!OS 8.02\"[/bold] starten",
            title="Naechste Schritte",
            border_style="cyan",
        )
    )


# --------------------------------------------------------------------------
# fbtest check
# --------------------------------------------------------------------------


@app.command()
def check(config: ConfigOption = None) -> None:
    """Prueft vor einem Testlauf, ob alle Voraussetzungen erfuellt sind."""
    context = _context(config)
    setup_logging(
        context.log_dir,
        level="WARNING",
        console=False,
        filename="check.log",
    )

    checker = Checker(context.config, context.base_dir, context.db_path)
    console.print("[bold]Vorabpruefung laeuft ...[/bold]\n")
    results = asyncio.run(checker.run_all())

    symbols = {Status.OK: "[green]OK  [/green]", Status.WARN: "[yellow]WARN[/yellow]",
               Status.FAIL: "[red]FEHL[/red]"}
    for result in results:
        console.print(f"{symbols[result.status]} [bold]{result.name}[/bold]: {result.message}")
        if result.hint:
            for line in result.hint.splitlines():
                console.print(f"       [dim]{line}[/dim]")

    console.print()
    if checker.worst is Status.OK:
        console.print("[bold green]Alles bereit - der Testlauf kann gestartet werden.[/bold green]")
    elif checker.worst is Status.WARN:
        console.print(
            "[bold yellow]Der Testlauf ist moeglich, aber eingeschraenkt.[/bold yellow] "
            "Die Hinweise oben beachten."
        )
        raise typer.Exit(code=0)
    else:
        console.print(
            "[bold red]So ist kein sinnvoller Testlauf moeglich.[/bold red] "
            "Zuerst die mit FEHL markierten Punkte beheben."
        )
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------
# fbtest run
# --------------------------------------------------------------------------


@app.command()
def run(
    config: ConfigOption = None,
    duration: Annotated[
        str | None,
        typer.Option("--duration", "-d", help="Laufzeit, z.B. 30m, 24h, 3d."),
    ] = None,
    name: Annotated[
        str | None, typer.Option("--name", "-n", help="Name des Testlaufs (z.B. Firmware-Version).")
    ] = None,
    resume: Annotated[
        bool | None,
        typer.Option("--resume/--no-resume", help="Offenen Testlauf fortsetzen."),
    ] = None,
    live: Annotated[
        bool, typer.Option("--live/--no-live", help="Live-Statusanzeige im Terminal.")
    ] = True,
) -> None:
    """Startet einen Testlauf."""
    context = _context(config)
    context.ensure_directories()
    app_config = context.config

    log_path = setup_logging(
        context.log_dir,
        level=app_config.logging.level,
        max_bytes=app_config.logging.max_bytes,
        backup_count=app_config.logging.backup_count,
        console=not live,
    )

    try:
        duration_s = parse_duration(duration) if duration else None
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    runner = TestRunner(
        config=app_config,
        base_dir=context.base_dir,
        db_path=context.db_path,
        duration_s=duration_s,
        name=name,
        resume=resume,
    )

    console.print(
        Panel(
            f"Laufzeit:   [bold]{format_duration(runner.duration_s)}[/bold]\n"
            f"Router:     {app_config.router.host}\n"
            f"Datenbank:  {runner.db_path}\n"
            f"Logdatei:   {log_path}\n\n"
            "Beenden mit [bold]Strg+C[/bold] - laufende Messungen werden sauber "
            "abgeschlossen und gespeichert.",
            title="FRITZ!Box-Langzeittest startet",
            border_style="cyan",
        )
    )

    try:
        summary = asyncio.run(_run_with_ui(runner, live))
    except KeyboardInterrupt:
        console.print("\n[yellow]Abgebrochen.[/yellow]")
        raise typer.Exit(code=130) from None

    console.print(
        Panel(
            f"Testlauf-ID:      [bold]#{summary.run.id}[/bold]\n"
            f"Geplante Dauer:   {format_duration(summary.planned_duration_s)}\n"
            f"Tatsaechlich:     {format_duration(summary.actual_duration_s)}"
            + ("  [yellow](vorzeitig beendet)[/yellow]" if summary.interrupted else "")
            + f"\nFirmware:         {summary.run.firmware_version or 'unbekannt'}\n"
            f"Geschriebene Zeilen: {summary.rows_written:,}".replace(",", "'")
            + f"\n\nBericht erzeugen mit:  [cyan]fbtest report {summary.run.id}[/cyan]",
            title="Testlauf beendet",
            border_style="green",
        )
    )


async def _run_with_ui(runner: TestRunner, live: bool):  # type: ignore[no-untyped-def]
    """Fuehrt den Testlauf aus, optional mit Live-Statusanzeige."""
    if not live:
        return await runner.execute()

    async def render() -> None:
        """Aktualisiert die Statusanzeige im Sekundentakt."""
        started = time.monotonic()
        with Live(console=console, refresh_per_second=2, transient=False) as display:
            while True:
                display.update(_status_table(runner, time.monotonic() - started))
                await asyncio.sleep(1.0)

    ui_task = asyncio.create_task(render(), name="ui")
    try:
        return await runner.execute()
    finally:
        ui_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ui_task


def _status_table(runner: TestRunner, elapsed_s: float) -> Table:
    """Baut die Tabelle der Live-Statusanzeige."""
    remaining = max(0.0, runner.duration_s - elapsed_s)
    table = Table(
        title=(
            f"Testlauf #{runner.run.id if runner.run else '?'} | "
            f"laeuft {format_duration(elapsed_s)} | "
            f"Restzeit {format_duration(remaining)} | "
            f"Puffer {runner.bus.pending} | "
            f"geschrieben {runner.writer.written_rows if runner.writer else 0}"
        ),
        expand=True,
    )
    table.add_column("Modul", style="cyan")
    table.add_column("Status")
    table.add_column("Laufzeit", justify="right")
    table.add_column("Neustarts", justify="right")
    table.add_column("Letzter Fehler", overflow="fold")

    for state in runner.scheduler.states:
        table.add_row(
            state.name,
            "[green]laeuft[/green]" if state.running else "[yellow]haelt an[/yellow]",
            format_duration(time.monotonic() - state.started_at),
            str(state.restarts),
            state.last_error or "-",
        )
    if not runner.scheduler.states:
        table.add_row("(keine)", "-", "-", "-", "-")
    return table


# --------------------------------------------------------------------------
# fbtest list / export / report
# --------------------------------------------------------------------------


@app.command("list")
def list_runs(config: ConfigOption = None) -> None:
    """Listet alle gespeicherten Testlaeufe mit ihren Kennzahlen."""
    _, db = _open_db(config)
    with db:
        runs = db.list_test_runs()
        if not runs:
            console.print("[yellow]Noch keine Testlaeufe vorhanden.[/yellow]")
            return

        table = Table(title="Gespeicherte Testlaeufe", expand=True)
        table.add_column("ID", justify="right", style="cyan")
        table.add_column("Name")
        table.add_column("Firmware")
        table.add_column("Beginn")
        table.add_column("Dauer", justify="right")
        table.add_column("Messwerte", justify="right")
        table.add_column("Ausfaelle", justify="right")
        table.add_column("Status")

        for item in runs:
            table.add_row(
                str(item.id),
                item.name or "-",
                item.firmware_version or "-",
                time.strftime("%d.%m.%Y %H:%M", time.localtime(item.started_at)),
                format_duration(item.duration_s),
                f"{db.count_rows('measurements', item.id):,}".replace(",", "'"),
                str(db.count_rows("outages", item.id)),
                "[green]beendet[/green]" if item.ended_at else "[yellow]offen[/yellow]",
            )
        console.print(table)


@app.command()
def export(
    run_id: Annotated[int, typer.Argument(help="ID des Testlaufs (siehe 'fbtest list').")],
    export_format: Annotated[
        str, typer.Option("--format", "-f", help="xlsx, csv, json, both oder all.")
    ] = "csv",
    config: ConfigOption = None,
) -> None:
    """Exportiert einen Testlauf als Arbeitsmappe, CSV und/oder JSON."""
    context, db = _open_db(config)
    target = context.export_dir

    allowed = {"xlsx", "csv", "json", "both", "all"}
    if export_format not in allowed:
        console.print(f"[red]--format muss eines von {', '.join(sorted(allowed))} sein.[/red]")
        raise typer.Exit(code=2)

    with db:
        try:
            if export_format in {"xlsx", "all"}:
                console.print(f"  [green]geschrieben[/green] {export_workbook(db, run_id, target)}")
            if export_format in {"csv", "both", "all"}:
                for path in export_csv(db, run_id, target):
                    console.print(f"  [green]geschrieben[/green] {path}")
            if export_format in {"json", "both", "all"}:
                console.print(f"  [green]geschrieben[/green] {export_json(db, run_id, target)}")
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc


@app.command()
def report(
    run_id: Annotated[
        int | None, typer.Argument(help="ID des auszuwertenden Testlaufs.")
    ] = None,
    compare: Annotated[
        tuple[int, int] | None,
        typer.Option("--compare", help="Zwei Testlauf-IDs gegenueberstellen."),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Erzeugt einen HTML-Bericht - einzeln oder als Vergleich zweier Laeufe."""
    context, db = _open_db(config)
    target = context.report_dir

    with db:
        try:
            if compare:
                path = render_comparison(db, compare[0], compare[1], target)
            elif run_id is not None:
                path = render_report(db, run_id, target)
            else:
                console.print(
                    "[red]Entweder eine Testlauf-ID angeben oder --compare <a> <b> verwenden.[/red]"
                )
                raise typer.Exit(code=2)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc

    console.print(f"[green]Bericht erzeugt:[/green] {path}")
    console.print(f"[dim]Im Browser oeffnen: start {path}[/dim]")


# --------------------------------------------------------------------------
# Noch nicht in der Alpha enthalten
# --------------------------------------------------------------------------


@app.command()
def agent(
    master: Annotated[str, typer.Option("--master", help="URL des Master-Systems.")] = "",
    name: Annotated[str, typer.Option("--name", help="Name dieses Agenten.")] = "",
) -> None:
    """Agent-Modus (Messungen von einem Zweitgeraet) - in der Alpha nicht enthalten."""
    console.print(
        Panel(
            "Der Agent-Modus ist in der Alpha [bold]noch nicht implementiert[/bold].\n\n"
            "Vorgesehen ist: Der Agent laeuft auf einem Raspberry Pi oder Zweitlaptop, "
            "fuehrt Ping- und Traffic-Module lokal aus, puffert bei Verbindungsverlust "
            "und sendet die Messwerte spaeter im Batch an den Master.\n\n"
            "Bis dahin lassen sich mehrere Geraete testen, indem auf jedem Geraet ein "
            "eigener Testlauf gestartet und die Datenbanken anschliessend verglichen werden.",
            title="Nicht verfuegbar",
            border_style="yellow",
        )
    )
    raise typer.Exit(code=1)


@app.command()
def dashboard(
    config: ConfigOption = None,
    host: Annotated[
        str | None, typer.Option("--host", help="Adresse, auf der das Dashboard lauscht.")
    ] = None,
    port: Annotated[int | None, typer.Option("--port", "-p", help="Port.")] = None,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Browser automatisch oeffnen.")
    ] = True,
) -> None:
    """Startet das Web-Dashboard zur Live-Ueberwachung.

    Testlaeufe lassen sich hier starten, beobachten und beenden; Berichte und
    Exporte werden direkt aus der Oberflaeche erzeugt.
    """
    import threading
    import webbrowser

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - nur ohne Installation erreichbar
        console.print(
            "[red]Das Dashboard benoetigt 'fastapi' und 'uvicorn'.[/red]\n"
            "Installation: [cyan]pip install -r requirements.txt[/cyan]"
        )
        raise typer.Exit(code=1) from exc

    from fbtest.dashboard.app import create_app

    context = _context(config, allow_missing=True)
    context.ensure_directories()
    app_config = context.config

    bind_host = host or app_config.dashboard.host
    bind_port = port or app_config.dashboard.port
    url = f"http://{bind_host}:{bind_port}"

    log_path = setup_logging(
        context.log_dir,
        level=app_config.logging.level,
        max_bytes=app_config.logging.max_bytes,
        backup_count=app_config.logging.backup_count,
        console=False,
        filename="dashboard.log",
    )

    console.print(
        Panel(
            f"Adresse:   [bold cyan]{url}[/bold cyan]\n"
            f"Router:    {app_config.router.host}\n"
            f"Datenbank: {context.db_path}\n"
            f"Logdatei:  {log_path}\n\n"
            + (
                "Im Dashboard laesst sich der Testlauf starten und beenden.\n"
                if context.configured
                else "[yellow]Noch nicht eingerichtet[/yellow] - im Browser fuehrt "
                "ein Assistent durch die Einrichtung.\n"
            )
            + "Beenden des Servers mit [bold]Strg+C[/bold] - ein laufender Testlauf\n"
            "wird dabei sauber abgeschlossen und gespeichert.",
            title="Dashboard startet",
            border_style="cyan" if context.configured else "yellow",
        )
    )

    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    web_app = create_app(context)
    try:
        uvicorn.run(web_app, host=bind_host, port=bind_port, log_level="warning")
    except KeyboardInterrupt:  # pragma: no cover - interaktiv
        console.print("\n[yellow]Dashboard beendet.[/yellow]")


@app.command("app")
def desktop_app(
    config: ConfigOption = None,
    port: Annotated[
        int | None, typer.Option("--port", "-p", help="Fester Port statt eines freien.")
    ] = None,
) -> None:
    """Startet die Desktop-Anwendung (Fenster und Infobereich-Symbol)."""
    try:
        from fbtest.desktop.application import DesktopApplication
    except ImportError as exc:  # pragma: no cover - nur ohne Installation erreichbar
        console.print(
            "[red]Die Desktop-Anwendung benoetigt 'pywebview' und 'pystray'.[/red]\n"
            "Installation: [cyan]pip install -r requirements.txt[/cyan]"
        )
        raise typer.Exit(code=1) from exc

    context = _context(config, allow_missing=True)
    context.ensure_directories()

    setup_logging(
        context.log_dir,
        level=context.config.logging.level,
        max_bytes=context.config.logging.max_bytes,
        backup_count=context.config.logging.backup_count,
        console=False,
        filename="desktop.log",
    )

    application = DesktopApplication(context, port=port)
    console.print(
        Panel(
            f"Adresse:   [bold cyan]{application.url}[/bold cyan]\n"
            f"Router:    {context.config.router.host}\n"
            f"Datenbank: {context.db_path}\n\n"
            + (
                ""
                if context.configured
                else "[yellow]Noch nicht eingerichtet[/yellow] - ein Assistent fuehrt "
                "durch die Einrichtung.\n"
            )
            + "Das Fenster darf geschlossen werden - ein laufender Testlauf\n"
            "laeuft im Infobereich weiter.",
            title="FRITZ!Box-Langzeittest",
            border_style="cyan",
        )
    )
    raise typer.Exit(code=application.run())


def gui() -> None:
    """Einstiegspunkt der gebuendelten Anwendung (ohne Konsolenfenster).

    Bewusst getrennt von der Kommandozeile: Ein Programm ohne Konsole hat kein
    ``sys.stdout``; jede Ausgabe von typer oder rich wuerde dort scheitern.
    """
    import sys as _sys

    from fbtest.desktop.application import DesktopApplication

    context = AppContext.load_or_template(None)
    context.ensure_directories()
    setup_logging(
        context.log_dir,
        level=context.config.logging.level,
        console=False,
        filename="desktop.log",
    )
    _sys.exit(DesktopApplication(context).run())


@app.command()
def version() -> None:
    """Zeigt die Programmversion."""
    console.print(f"fbtest {__version__} (Python {sys.version.split()[0]})")


if __name__ == "__main__":
    app()
