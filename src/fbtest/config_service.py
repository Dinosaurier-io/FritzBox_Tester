"""Lesen, Pruefen und Schreiben der Konfiguration fuer die Oberflaeche.

Die Kommandozeile kommt mit :func:`fbtest.config.load_config` aus - sie liest
die Datei einmal und faellt bei einem Fehler mit einer Meldung aus. Eine
Oberflaeche braucht mehr:

* **Feldgenaue Fehler.** "Konfiguration ungueltig" hilft in einem Formular
  niemandem; die Meldung muss an dem Eingabefeld stehen, das sie verursacht hat.
* **Schreiben ohne Kommentarverlust.** Die ausgelieferte ``config.yaml`` ist zu
  weiten Teilen Dokumentation. Wuerde das Speichern aus dem Formular sie in eine
  nackte Werteliste verwandeln, waere die Rohansicht fuer Fortgeschrittene
  wertlos - und der Nutzer haette etwas verloren, ohne es zu merken.
* **Geheimnisse getrennt halten.** Das Passwort darf die Oberflaeche nie
  erreichen und beim Speichern nicht versehentlich geleert werden.

Deshalb wird zum *Lesen* weiterhin PyYAML verwendet (schnell, ausreichend) und
zum *Schreiben* ``ruamel.yaml`` im Round-Trip-Modus, das Kommentare, Reihenfolge
und Formatierung erhaelt.
"""

from __future__ import annotations

import io
import shutil
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from pydantic.json_schema import PydanticJsonSchemaWarning
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from fbtest.config import AppConfig, ConfigError
from fbtest.paths import AppPaths, example_config_path

#: Platzhalter, der in der Oberflaeche anstelle des Passworts erscheint.
PASSWORD_MASK = "***"

#: Endung der Sicherungskopie, die vor jedem Schreiben angelegt wird.
BACKUP_SUFFIX = ".bak"

#: Kennzeichnet "Schluessel war bisher nicht vorhanden" - abgegrenzt von ``None``,
#: das ein gueltiger Konfigurationswert ist.
_MISSING = object()


# --------------------------------------------------------------------------
# Fehler
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FieldError:
    """Ein einzelner Validierungsfehler, einem Eingabefeld zugeordnet."""

    path: str
    """Punktpfad des Feldes, z.B. ``ping.targets.0.host``."""

    message: str
    """Verstaendliche Meldung."""

    kind: str = ""
    """Fehlertyp von pydantic, z.B. ``missing`` oder ``greater_than``."""


class ConfigValidationError(ConfigError):
    """Die Konfiguration ist ungueltig - mit Zuordnung zu einzelnen Feldern."""

    def __init__(self, errors: list[FieldError]) -> None:
        self.errors = errors
        details = "; ".join(f"{item.path}: {item.message}" for item in errors)
        super().__init__(f"Konfiguration ungueltig ({len(errors)} Fehler): {details}")


def _to_field_errors(error: ValidationError) -> list[FieldError]:
    """Wandelt einen pydantic-Fehler in feldbezogene Meldungen um."""
    result: list[FieldError] = []
    for item in error.errors():
        # Bei Union-Typen (Traffic-Profile) enthaelt loc den Namen der Variante.
        # Der interessiert im Formular nicht - dort zaehlt der Feldpfad.
        parts = [str(part) for part in item["loc"] if not str(part).startswith("function-")]
        result.append(
            FieldError(path=".".join(parts), message=item["msg"], kind=str(item.get("type", "")))
        )
    return result


# --------------------------------------------------------------------------
# Lesen
# --------------------------------------------------------------------------


def to_form_values(config: AppConfig) -> dict[str, Any]:
    """Konfiguration als JSON-taugliche Werte fuer das Formular.

    Das Passwort wird durch :data:`PASSWORD_MASK` ersetzt. Es verlaesst den
    Prozess damit nie - auch nicht ueber die localhost-Schnittstelle.

    Args:
        config: Die aktuelle Konfiguration.

    Returns:
        Verschachteltes Dictionary mit allen Feldern.
    """
    values: dict[str, Any] = config.model_dump(mode="json")
    values["router"]["password"] = PASSWORD_MASK if config.router.password else ""
    return values


def json_schema() -> dict[str, Any]:
    """JSON-Schema aller Konfigurationsfelder.

    Die Oberflaeche baut daraus ihr Formular: Beschriftungen stammen aus den
    ``description``-Angaben der pydantic-Modelle, Wertebereiche aus deren
    Einschraenkungen. So bleibt das Formular automatisch aktuell, wenn ein Feld
    dazukommt.
    """
    with warnings.catch_warnings():
        # Die Vorgabewerte der Speicherorte sind Path-Objekte und damit nicht
        # JSON-faehig; pydantic laesst sie im Schema weg und warnt darueber.
        # Das ist hier folgenlos: Die Werte fuers Formular kommen aus
        # to_form_values(), das Schema liefert nur Beschriftungen und Grenzen.
        warnings.simplefilter("ignore", PydanticJsonSchemaWarning)
        return AppConfig.model_json_schema()


def read_raw(paths: AppPaths) -> str:
    """Liefert den Dateiinhalt fuer die Rohansicht.

    Raises:
        ConfigError: Wenn die Datei nicht gelesen werden kann.
    """
    try:
        return paths.config_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Konfiguration nicht lesbar ({paths.config_file}): {exc}") from exc


# --------------------------------------------------------------------------
# Pruefen
# --------------------------------------------------------------------------


def validate(values: dict[str, Any]) -> AppConfig:
    """Prueft Formularwerte und erzeugt daraus eine Konfiguration.

    Args:
        values: Verschachteltes Dictionary wie von :func:`to_form_values`.

    Returns:
        Die validierte Konfiguration.

    Raises:
        ConfigValidationError: Mit einem Eintrag je fehlerhaftem Feld.
    """
    try:
        return AppConfig.model_validate(values)
    except ValidationError as exc:
        raise ConfigValidationError(_to_field_errors(exc)) from exc


def validate_raw(text: str) -> AppConfig:
    """Prueft den Inhalt der Rohansicht.

    Raises:
        ConfigError: Bei YAML-Syntaxfehlern.
        ConfigValidationError: Bei inhaltlichen Fehlern.
    """
    yaml = YAML(typ="safe")
    try:
        data = yaml.load(text)
    except YAMLError as exc:
        raise ConfigError(f"YAML-Syntaxfehler:\n{exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError("Die Datei enthaelt kein YAML-Objekt (erwartet Schluessel/Wert-Paare).")
    return validate(data)


# --------------------------------------------------------------------------
# Schreiben
# --------------------------------------------------------------------------


@dataclass(slots=True)
class SaveResult:
    """Ergebnis eines Speichervorgangs."""

    config: AppConfig
    """Die nun gueltige Konfiguration."""

    path: Path
    """Datei, die geschrieben wurde."""

    backup: Path | None = None
    """Angelegte Sicherungskopie, falls eine Vorgaengerdatei existierte."""

    warnings: list[str] = field(default_factory=list)
    """Hinweise, die keine Fehler sind (z.B. verlorene Kommentare in Listen)."""


def _round_trip_yaml() -> YAML:
    """YAML-Instanz, die Kommentare und Formatierung erhaelt."""
    yaml = YAML()
    yaml.preserve_quotes = True
    # Die ausgelieferte Vorlage nutzt zweistellige Einrueckung und Listen mit
    # Einzug - ohne diese Vorgaben formatiert ruamel beim Speichern alles um
    # und der Unterschied zur Vorlage waere unlesbar gross.
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 100
    # ruamel schreibt None standardmaessig als leeren Wert ("port:"). Das ist
    # zwar gueltiges YAML, aber ein unnoetiger Unterschied zur ausgelieferten
    # Datei - und ein leeres Feld sieht aus wie ein vergessener Eintrag.
    yaml.representer.add_representer(
        type(None),
        lambda representer, _: representer.represent_scalar("tag:yaml.org,2002:null", "null"),
    )
    return yaml


def _merge_into(
    target: Any,
    source: dict[str, Any],
    baseline: dict[str, Any] | None,
    warnings: list[str],
    prefix: str = "",
) -> None:
    """Uebertraegt geaenderte Werte in die geladene YAML-Struktur.

    Entscheidend ist der Vergleich mit ``baseline`` - dem Stand, wie er beim
    Laden der Datei galt. Nur tatsaechlich geaenderte Zweige werden angefasst;
    alles andere bleibt Zeichen fuer Zeichen so, wie der Nutzer es geschrieben
    hat.

    Ohne diesen Vergleich muesste jeder Wert neu gesetzt werden, denn das
    Formular sendet stets das vollstaendige Modell - einschliesslich aller
    Vorgabewerte, die in der Datei bewusst gar nicht auftauchen. Listen wuerden
    dann bei jedem Speichern ersetzt und ihre Kommentare jedes Mal verlieren.

    Args:
        target: Geladene YAML-Struktur (wird veraendert).
        source: Neue Werte.
        baseline: Bisherige Werte; ``None``, wenn es keinen Vorgaenger gibt.
        warnings: Sammelliste fuer Hinweise.
        prefix: Punktpfad der aktuellen Ebene (fuer die Hinweise).
    """
    for key, value in source.items():
        path = f"{prefix}{key}"
        previous = baseline.get(key, _MISSING) if baseline is not None else _MISSING
        if previous == value:
            continue

        current = target.get(key) if hasattr(target, "get") else None
        if isinstance(value, dict) and isinstance(current, dict):
            _merge_into(
                current,
                value,
                previous if isinstance(previous, dict) else None,
                warnings,
                prefix=f"{path}.",
            )
        elif isinstance(value, list) and isinstance(current, list) and current:
            # Eintraege koennen hinzukommen, wegfallen oder die Reihenfolge
            # wechseln - eine Zuordnung alter Kommentare waere Raterei.
            _replace_node(target, key, value)
            warnings.append(f"Kommentare innerhalb von '{path}' gehen beim Speichern verloren.")
        else:
            target[key] = value

    # Schluessel, die es im Modell nicht mehr gibt, entfernen - sonst schlaegt
    # das naechste Laden fehl (die Modelle verbieten unbekannte Schluessel).
    for key in [k for k in target if k not in source]:
        del target[key]


def save(
    paths: AppPaths, values: dict[str, Any], *, keep_password: str | None = None
) -> SaveResult:
    """Prueft Werte und schreibt sie unter Erhalt der Kommentare.

    Args:
        paths: Speicherorte.
        values: Formularwerte.
        keep_password: Bisheriges Passwort. Enthaelt ``values`` die Maske
            :data:`PASSWORD_MASK`, wird dieser Wert eingesetzt - sonst wuerde
            das Absenden des Formulars das Passwort loeschen.

    Returns:
        Das Ergebnis samt Sicherungskopie und Hinweisen.

    Raises:
        ConfigValidationError: Wenn die Werte ungueltig sind. Es wird in diesem
            Fall nichts geschrieben.
        ConfigError: Wenn die Datei nicht geschrieben werden kann.
    """
    values = _with_password(values, keep_password)
    config = validate(values)

    yaml = _round_trip_yaml()
    target = paths.config_file
    warnings: list[str] = []

    # Grundlage ist die bestehende Datei; existiert sie noch nicht, die
    # mitgelieferte Vorlage. So bekommt auch eine frisch eingerichtete
    # Installation die vollstaendig kommentierte Fassung.
    source_file = target if target.exists() else example_config_path()
    try:
        document = yaml.load(source_file.read_text(encoding="utf-8"))
    except (OSError, YAMLError):
        # Eine unlesbare Vorlage darf das Speichern nicht verhindern - dann
        # eben ohne Kommentare, aber mit korrekten Werten.
        document = {}
        warnings.append("Vorlage nicht lesbar - die Datei wird ohne Kommentare geschrieben.")

    _merge_into(document, values, _baseline(source_file), warnings)

    backup = _backup(target)
    buffer = io.StringIO()
    yaml.dump(document, buffer)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(buffer.getvalue(), encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Konfiguration nicht schreibbar ({target}): {exc}") from exc

    return SaveResult(config=config, path=target, backup=backup, warnings=warnings)


def save_raw(paths: AppPaths, text: str) -> SaveResult:
    """Schreibt den Inhalt der Rohansicht unveraendert.

    Der Text wird zuvor geprueft - eine ungueltige Datei wird nicht gespeichert.

    Raises:
        ConfigError: Bei Syntaxfehlern oder wenn nicht geschrieben werden kann.
        ConfigValidationError: Bei inhaltlichen Fehlern.
    """
    config = validate_raw(text)
    target = paths.config_file
    backup = _backup(target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Konfiguration nicht schreibbar ({target}): {exc}") from exc
    return SaveResult(config=config, path=target, backup=backup)


def _replace_node(target: Any, key: str, value: Any) -> None:
    """Ersetzt einen Teilbaum und loest die daran haengenden Kommentare.

    ruamel merkt sich Kommentare getrennt von den Werten. Wird nur der Wert
    ersetzt, bleiben die Kommentare des alten Knotens stehen und landen an
    einer Stelle, an der sie die Datei zerstoeren - beobachtet als
    ``profiles:`` gefolgt von einem Kommentar und ``[]``, wodurch der
    naechste Abschnitt in dieselbe Zeile rutschte und die Datei unlesbar wurde.
    """
    comments = getattr(getattr(target, "ca", None), "items", None)
    if comments is not None:
        comments.pop(key, None)
    target[key] = value


def _baseline(source_file: Path) -> dict[str, Any] | None:
    """Bisherige Werte als Vergleichsgrundlage fuer :func:`_merge_into`.

    Bewusst ueber das vollstaendige Modell und nicht ueber den rohen
    YAML-Inhalt: Nur so sind weggelassene Vorgabewerte mit den vom Formular
    gesendeten vergleichbar - andernfalls gaelte jeder nicht ausgeschriebene
    Wert als Aenderung.

    Returns:
        Die Werte oder ``None``, wenn die Datei nicht auswertbar ist.
    """
    from fbtest.config import load_config

    try:
        return load_config(source_file).model_dump(mode="json")
    except ConfigError:
        return None


def _with_password(values: dict[str, Any], keep_password: str | None) -> dict[str, Any]:
    """Setzt das bisherige Passwort ein, wenn das Formular nur die Maske sendet."""
    router = values.get("router")
    if not isinstance(router, dict) or router.get("password") != PASSWORD_MASK:
        return values

    merged = {**values, "router": {**router, "password": keep_password or ""}}
    return merged


def _backup(target: Path) -> Path | None:
    """Legt eine Sicherungskopie an, falls die Datei bereits existiert.

    Eine Fehlbedienung im Formular soll die von Hand gepflegte Konfiguration
    nicht unwiederbringlich ueberschreiben.
    """
    if not target.exists():
        return None
    backup = target.with_suffix(target.suffix + BACKUP_SUFFIX)
    try:
        shutil.copyfile(target, backup)
    except OSError:
        return None
    return backup
