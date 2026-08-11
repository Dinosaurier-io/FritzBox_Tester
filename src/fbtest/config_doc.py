"""Erzeugt die Referenz aller Einstellungen aus den Konfigurationsmodellen.

Eine von Hand gepflegte Referenz ist nach dem dritten neuen Feld unvollstaendig,
und niemand merkt es - dieselbe Ueberlegung wie beim Einstellungsformular, das
sein Aussehen ebenfalls aus dem Schema bezieht. Hier stammen Feldname, Typ,
Wertebereich, Standard und Beschreibung aus :mod:`fbtest.config`; von Hand
kommt nur der Fliesstext, den ein Schema nicht kennen kann: wozu ein Abschnitt
da ist und was man sich beim Verstellen einhandelt.

``test_config_doc.py`` vergleicht die erzeugte Fassung mit der Datei im
Projekt. Ein neues Feld ohne Beschreibung oder eine veraltete Referenz fallen
damit in der Testsuite auf, nicht erst beim Leser.

Neu erzeugen::

    .\\.venv\\Scripts\\python.exe -m fbtest.config_doc
"""

from __future__ import annotations

import sys
import types
import typing
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from fbtest.config import AppConfig

#: Vorgabepfad der erzeugten Datei, relativ zum Projektordner.
DEFAULT_TARGET = Path("docs/EINSTELLUNGEN.md")

#: Ueberschrift je Abschnitt. Der Schluessel ist der Name in der ``config.yaml``.
SECTION_TITLES: dict[str, str] = {
    "router": "FRITZ!Box-Zugang",
    "ping": "Erreichbarkeit und Ausfallerkennung",
    "traffic": "Kuenstlich erzeugte Last",
    "speedtest": "Bandbreitenmessung",
    "wlan": "WLAN-Ueberwachung",
    "run": "Testlauf",
    "storage": "Speicherorte",
    "logging": "Protokollierung",
    "dashboard": "Dashboard",
    "desktop": "Desktop-Anwendung",
}

#: Einleitung je Abschnitt - das Warum, das im Schema nicht steht.
SECTION_INTROS: dict[str, str] = {
    "router": (
        "Ohne diesen Zugang misst das Programm weiterhin Erreichbarkeit und Bandbreite, "
        "aber es kann keinen Router-Neustart erkennen und keine Betriebsdauer auslesen - "
        "und damit den wichtigsten Befund eines Langzeittests nicht belegen.\n\n"
        "Das Passwort gehoert **nicht** in diese Datei. Die Rangfolge ist "
        "`FRITZ_PASSWORD` (Umgebungsvariable) vor dem Schluesselspeicher des Systems vor "
        "dem Klartexteintrag; die Umgebungsvariable gewinnt immer."
    ),
    "ping": (
        "Die Grundlage der Ausfallerkennung. Entscheidend ist die Mischung der Ziele: "
        "Antwortet das Gateway, das Internet aber nicht, liegt das Problem hinter dem "
        "Router; antwortet auch das Gateway nicht, davor. Ohne ein Ziel je `scope` laesst "
        "sich diese Unterscheidung nicht treffen.\n\n"
        "Gespeichert wird nicht jeder einzelne Ping, sondern je `aggregate_window_s` ein "
        "verdichteter Wert. Das haelt die Datenbank auch nach Tagen handlich - der "
        "Zeitpunkt eines Ausfalls bleibt trotzdem sekundengenau, weil Ausfaelle als "
        "eigene Ereignisse gefuehrt werden."
    ),
    "traffic": (
        "Ein Stabilitaetstest ohne Last ist wenig wert: Viele Firmware-Probleme zeigen "
        "sich erst unter Dauerbetrieb mit mehreren gleichzeitigen Verbindungen. Jedes "
        "Profil laeuft als eigener virtueller Client mit eigener Statistik und wird nach "
        "einem Netzwerkfehler mit wachsender Wartezeit neu gestartet, nie abgebrochen.\n\n"
        "**Zum Datenvolumen:** Eine Dauerlast von 1 Mbit/s ergibt rund 10,8 GB pro Tag. "
        "Die heruntergeladenen Daten werden nirgends gespeichert - sie laufen in "
        "64-KB-Haeppchen durch den Arbeitsspeicher und werden verworfen. Beim Provider "
        "faellt das Volumen trotzdem an."
    ),
    "speedtest": (
        "Misst in Abstaenden die tatsaechliche Bandbreite und zugleich die Latenz *unter "
        "Last* - die Differenz zur Ruhelatenz ist der Bufferbloat-Indikator.\n\n"
        "Diese Messung drosselt bewusst nicht und laeuft mit voller Leitungsgeschwindigkeit. "
        "Bei 500 Mbit/s und dem Standardabstand von 30 Minuten sind das rund 39 GB pro Tag. "
        "Waehrend der Messung pausieren die Traffic-Profile, damit die eigene "
        "Hintergrundlast das Ergebnis nicht verfaelscht."
    ),
    "wlan": (
        "Zwei Blickwinkel auf dasselbe Funknetz: Die Routersicht (TR-064) kennt alle "
        "verbundenen Geraete und Baender, die Clientsicht (`netsh` unter Windows, `iw` "
        "unter Linux) kennt Signalstaerke und Verbindungsrate des messenden Rechners. "
        "Erst zusammen zeigen sie, ob ein Abriss am Router oder am Endgeraet lag.\n\n"
        "Der zyklische Bandwechsel setzt **getrennte SSIDs je Band** voraus; in der "
        "FRITZ!Box muss dafuer das Band-Steering abgeschaltet sein. Sonst entscheidet der "
        "Router, in welchem Band der Rechner landet, und die Messung vergleicht nichts."
    ),
    "run": (
        "Rahmen eines einzelnen Laufs. `duration_s` nimmt auch Angaben mit Einheit "
        "entgegen (`90s`, `30m`, `24h`, `3d`).\n\n"
        "`time_gap_threshold_s` trennt zwei Faelle, die in den Rohdaten gleich aussehen: "
        "Ein Rechner im Standby hat nicht gemessen, das ist kein Ausfall der Leitung. "
        "Deshalb wird der Energiesparmodus waehrend eines Laufs unterdrueckt - gelingt "
        "das nicht, laeuft der Test weiter, haelt es aber als `POWER_KEEPALIVE` fest."
    ),
    "storage": (
        "Alle Pfade duerfen relativ sein; die Basis ist das Arbeitsverzeichnis, das "
        "`paths.py` nach fuenf Regeln ermittelt (`--config`, `FBTEST_HOME`, `config.yaml` "
        "im aktuellen Ordner, Projektordner, `%LOCALAPPDATA%\\fbtest`).\n\n"
        "Messwerte werden gesammelt und gebuendelt geschrieben, nicht einzeln. Die "
        "Datenbank laeuft im WAL-Modus: Wer sie kopiert, muss `fbtest.sqlite-wal` und "
        "`-shm` mitkopieren, sonst fehlen genau die neuesten Zeilen.\n\n"
        "**Waehrend eines laufenden Tests sind diese Felder gesperrt.** Ein Wechsel des "
        "Speicherorts mitten im Lauf wuerde die Messreihe zerreissen."
    ),
    "logging": (
        "Das Protokoll ist die zweite Spur neben der Datenbank und beantwortet Fragen, "
        "die keine Messreihe beantwortet - etwa warum ein Modul neu gestartet wurde. "
        "`DEBUG` protokolliert jeden Einzelaufruf und laesst die Dateien schnell "
        "wachsen; fuer die Fehlersuche ist das richtig, fuer einen mehrtaegigen Lauf "
        "nicht."
    ),
    "dashboard": (
        "Das Dashboard bleibt bewusst auf `127.0.0.1` und kennt keine Benutzeranmeldung. "
        "Gegen Zugriffe fremder Webseiten schuetzt ein Sitzungs-Token, das bei jedem "
        "Start neu vergeben wird - kein Cookie, denn ein Cookie wuerde der Browser auch "
        "fremden Seiten mitsenden.\n\n"
        "Eine Freigabe ins Netz ist nicht vorgesehen. Wer von aussen zusehen will, "
        "nimmt einen SSH-Tunnel."
    ),
    "desktop": (
        "Gilt nur fuer die gebuendelte Anwendung mit eigenem Fenster. Das Schliessen des "
        "Fensters beendet einen laufenden Test **nicht**, sondern versteckt es nur - ein "
        "abgewuergter Lauf ueber mehrere Stunden waere nicht wiederherstellbar."
    ),
}

#: Erlaeuterung je Untermodell, das nicht selbst ein Abschnitt ist.
NESTED_INTROS: dict[str, str] = {
    "ping.targets": (
        "Mindestens ein Ziel ist Pflicht. Sinnvoll sind drei: der Router selbst und zwei "
        "voneinander unabhaengige Ziele im Internet - faellt nur eines davon aus, liegt "
        "es an diesem Ziel und nicht an der Leitung."
    ),
    "ping.dns": (
        "Eine langsame Namensaufloesung fuehlt sich an wie «das Internet ist langsam», "
        "hat mit der Leitung aber nichts zu tun. Deshalb wird sie getrennt gemessen."
    ),
    "wlan.profiles": "Nur fuer den Bandwechsel-Test noetig, und dann mindestens zwei Eintraege.",
    "traffic.profiles": (
        "Welche Felder ein Profil hat, entscheidet sein `type`. Die folgenden Tabellen "
        "zeigen je Typ die zusaetzlichen Felder; die gemeinsamen stehen darueber."
    ),
}

_KIND_NAMES: dict[type, str] = {
    bool: "ja/nein",
    int: "Ganzzahl",
    float: "Zahl",
    str: "Text",
    Path: "Pfad",
}


def _unwrap_optional(annotation: Any) -> Any:
    """Entfernt ``| None`` aus einer Typangabe."""
    if get_origin(annotation) in (typing.Union, types.UnionType):
        rest = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(rest) == 1:
            return rest[0]
    return annotation


def _models_in(annotation: Any) -> list[type[BaseModel]]:
    """Sammelt alle pydantic-Modelle in einer Typangabe (auch in Listen/Unions)."""
    annotation = _unwrap_optional(annotation)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    found: list[type[BaseModel]] = []
    for arg in get_args(annotation):
        found.extend(_models_in(arg))
    return found


def _constraint(field: FieldInfo, name: str) -> Any:
    """Liest eine Randbedingung (``ge``, ``gt``, ``le``, ``lt``) eines Feldes."""
    for entry in field.metadata:
        value = getattr(entry, name, None)
        if value is not None:
            return value
    return None


def _type_text(field: FieldInfo) -> str:
    """Beschreibt Typ und Wertebereich eines Feldes in einem Satzteil."""
    annotation = _unwrap_optional(field.annotation)
    origin = get_origin(annotation)

    if origin is Literal:
        return " / ".join(f"`{value}`" for value in get_args(annotation))
    if origin in (list, set, tuple):
        if _models_in(annotation):
            return "Liste von Eintraegen"
        inner = _unwrap_optional(get_args(annotation)[0]) if get_args(annotation) else None
        return f"Liste von {_KIND_NAMES.get(inner, 'Werten') if inner else 'Werten'}"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "Unterabschnitt"

    text = _KIND_NAMES.get(annotation, getattr(annotation, "__name__", str(annotation)))
    lower = _constraint(field, "gt")
    if lower is not None:
        text += f" > {lower:g}"
    else:
        lower = _constraint(field, "ge")
        if lower is not None:
            text += f" ab {lower:g}"
    upper = _constraint(field, "le")
    if upper is not None:
        text += f" bis {upper:g}"
    return text


def _readable_duration(seconds: float) -> str:
    """Gibt eine Dauer in der groebsten passenden Einheit zurueck (leer unter 1 min).

    ``format_duration`` aus :mod:`fbtest.config` waere hier falsch: Es liefert
    ``00:30:00``, gedacht fuer eine mitlaufende Uhr. In einer Tabelle liest sich
    ``30 min`` besser.
    """
    for factor, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if seconds >= factor and seconds % factor == 0:
            return f"{seconds / factor:g} {unit}"
    return ""


def _default_text(name: str, field: FieldInfo) -> str:
    """Formatiert den Standardwert; Dauern zusaetzlich in lesbarer Form."""
    if field.default is PydanticUndefined and field.default_factory is None:
        return "**Pflicht**"
    value = field.default
    if field.default_factory is not None:
        factory: Any = field.default_factory
        value = factory()

    if value is None:
        return "keiner"
    if isinstance(value, BaseModel) or value == [] or value == {}:
        return "leer"
    if isinstance(value, bool):
        return "ja" if value else "nein"
    if isinstance(value, list):
        return ", ".join(f"`{entry}`" for entry in value)
    if value == "":
        return "leer"
    if isinstance(value, int | float) and name.endswith("_s"):
        readable = _readable_duration(float(value))
        return f"`{value:g}` s ({readable})" if readable else f"`{value:g}` s"
    if isinstance(value, float):
        return f"`{value:g}`"
    return f"`{value}`"


def _rows(model: type[BaseModel]) -> Iterator[str]:
    """Erzeugt die Tabellenzeilen eines Modells."""
    for name, field in model.model_fields.items():
        description = field.description or ""
        yield f"| `{name}` | {_type_text(field)} | {_default_text(name, field)} | {description} |"


def _table(model: type[BaseModel]) -> str:
    """Baut die vollstaendige Tabelle eines Modells."""
    head = "| Feld | Typ | Standard | Bedeutung |\n|---|---|---|---|"
    return "\n".join([head, *_rows(model)])


def _nested(path: str, model: type[BaseModel], level: int) -> Iterator[str]:
    """Gibt die Tabellen aller Untermodelle unterhalb von ``model`` aus."""
    for name, field in model.model_fields.items():
        children = _models_in(field.annotation)
        if not children:
            continue
        child_path = f"{path}.{name}"
        title = "Eintrag" if get_origin(_unwrap_optional(field.annotation)) is list else "Felder"
        yield f"\n{'#' * level} `{child_path}` – {title}\n"
        if child_path in NESTED_INTROS:
            yield f"{NESTED_INTROS[child_path]}\n"
        for child in children:
            if len(children) > 1:
                marker = child.model_fields.get("type")
                variant = marker.default if marker else child.__name__
                yield f"\n**`type: {variant}`** – {(child.__doc__ or '').strip()}\n"
            yield _table(child)
            yield ""
            yield from _nested(child_path, child, level + 1)


def render() -> str:
    """Baut die vollstaendige Referenz als Markdown."""
    parts: list[str] = [HEADER]

    parts.append("## Inhalt\n")
    for name in AppConfig.model_fields:
        parts.append(f"- [`{name}` – {SECTION_TITLES.get(name, name)}](#{name})")
    parts.append("")

    for name, field in AppConfig.model_fields.items():
        models = _models_in(field.annotation)
        if not models:
            continue
        model = models[0]
        parts.append(f'\n<a id="{name}"></a>\n')
        parts.append(f"## `{name}` – {SECTION_TITLES.get(name, name)}\n")
        intro = SECTION_INTROS.get(name)
        if intro:
            parts.append(f"{intro}\n")
        parts.append(_table(model))
        parts.extend(_nested(name, model, 3))

    parts.append(FOOTER)
    return "\n".join(parts).rstrip() + "\n"


HEADER = """<!-- Erzeugt aus den Modellen in src/fbtest/config.py.
     Nicht von Hand aendern: '.\\.venv\\Scripts\\python.exe -m fbtest.config_doc'
     schreibt diese Datei neu, und test_config_doc.py prueft sie gegen die Modelle. -->

# Einstellungen

Vollstaendige Referenz aller Felder der `config.yaml`. Sie entsteht aus denselben
Modellen, aus denen auch das Einstellungsformular und die Pruefung beim Speichern
gebaut werden - sie kann also nicht veralten.

Drei Wege fuehren zu denselben Werten:

| Weg | Wofuer |
|---|---|
| **Dashboard → Einstellungen** | Formular mit Suche, Einheiten und Standardwerten; empfohlen |
| **`config.yaml` im Texteditor** | volle Kontrolle; `fbtest init` legt eine kommentierte an |
| **Rohansicht im Dashboard** | dieselbe Datei im Browser, mit Pruefung vor dem Speichern |

Zum Lesen der Tabellen:

- **Pflicht** in der Spalte *Standard* heisst: ohne diesen Wert startet das Programm nicht.
- Felder auf `_s` sind Sekunden. Im Formular sind sie mit Einheit dargestellt, in der Datei
  bleiben es Zahlen; `run.duration_s` nimmt zusaetzlich Angaben wie `24h` entgegen.
- `enabled: nein` schaltet einen ganzen Abschnitt ab, ohne seine Einstellungen zu verlieren.
- Ein ungueltiger Wert wird beim Speichern **abgelehnt**, die bisherige Datei bleibt stehen.
  Vor jedem Schreiben entsteht zusaetzlich eine `config.yaml.bak`.
"""

FOOTER = """
---

## Was hier nicht steht

**Zugangsdaten.** Das Passwort der FRITZ!Box wird nicht in der `config.yaml` gefuehrt,
sondern ueber `FRITZ_PASSWORD` oder den Schluesselspeicher des Systems - siehe
«Passwort setzen» in der [README](../README.md#3-passwort-setzen--nicht-in-der-datei).

**Der Speicherort der `config.yaml` selbst.** Welche Datei benutzt wird, entscheiden fuenf
Regeln in der Reihenfolge `--config`, `FBTEST_HOME`, `config.yaml` im aktuellen Ordner,
Projektordner, `%LOCALAPPDATA%\\fbtest`. Die Diagnose im Dashboard zeigt an, welche Regel
gegriffen hat.

**Was die Werte bewirken.** Diese Referenz beschreibt die Felder; wie das Programm daraus
Ausfaelle, Neustarts und Kennzahlen ableitet, steht in der [README](../README.md).
"""


def main(argv: list[str] | None = None) -> int:
    """Schreibt die Referenz an den angegebenen Ort (Vorgabe: ``docs/EINSTELLUNGEN.md``)."""
    args = sys.argv[1:] if argv is None else argv
    target = Path(args[0]) if args else DEFAULT_TARGET
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(), encoding="utf-8", newline="\n")
    print(f"{target}: {len(render().splitlines())} Zeilen geschrieben.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
