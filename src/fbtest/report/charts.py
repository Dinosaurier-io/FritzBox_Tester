"""Diagramme fuer den Testbericht.

Die Grafiken werden als PNG erzeugt und anschliessend base64-kodiert direkt in
das HTML eingebettet. Der Bericht ist damit **eine einzige Datei**, die sich
weitergeben, archivieren und ausdrucken laesst, ohne dass Bilder verloren gehen.

Matplotlib laeuft im Backend ``Agg`` - ohne Fenster, ohne GUI-Abhaengigkeit.
"""

from __future__ import annotations

import base64
import io
import logging
from collections.abc import Sequence
from datetime import UTC, datetime

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.artist import Artist
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

log = logging.getLogger(__name__)

#: Farbe der hinterlegten Ausfallzeitraeume.
_OUTAGE_COLOR = "#d62728"
#: Farbe der senkrechten Marken (Router-Neustarts).
_MARKER_COLOR = "#8c564b"
#: Einheitliche Farbgebung aller Diagramme. Bewusst ohne die beiden Farben
#: darueber: Eine Kurve in genau dem Rot der Ausfallflaeche laesst sich von
#: ihr nicht mehr unterscheiden, und dann hilft auch die Legende nicht weiter.
COLORS = ["#1f77b4", "#2ca02c", "#ff7f0e", "#9467bd", "#17becf", "#e377c2"]
_FIGSIZE = (10.0, 3.6)
_DPI = 110


def _to_datetimes(timestamps: Sequence[float]) -> list[datetime]:
    """Wandelt Unix-Timestamps in lokale ``datetime``-Objekte."""
    return [datetime.fromtimestamp(ts, tz=UTC).astimezone() for ts in timestamps]


def _finish(fig: Figure, ax: Axes, ylabel: str) -> str:
    """Formatiert eine Achse einheitlich und gibt das Bild als data-URI zurueck."""
    ax.set_ylabel(ylabel)
    ax.grid(True, alpha=0.25, linestyle=":")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m.\n%H:%M"))
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=_DPI)
    plt.close(fig)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _empty_note(title: str, hint: str = "") -> str:
    """Erzeugt ein Platzhalterbild, wenn keine Daten vorliegen.

    Bewusst ein leeres Diagramm mit Hinweis statt erfundener Werte. Ohne
    Begruendung sieht ein leeres Diagramm allerdings nach Defekt aus, und der
    Leser sucht den Fehler im Programm statt in seinen Einstellungen - wie die
    Kennzahlen im Management-Summary nennt der Platzhalter deshalb den Grund.

    Args:
        title: Titel des Diagramms, das keine Daten hat.
        hint: Zusaetzliche Zeile darunter, die den haeufigsten Grund erklaert.
    """
    fig, ax = plt.subplots(figsize=_FIGSIZE)
    ax.text(
        0.5,
        0.58 if hint else 0.5,
        f"Keine Daten fuer '{title}' vorhanden.",
        ha="center",
        va="center",
        fontsize=11,
        color="#777777",
    )
    if hint:
        ax.text(
            0.5,
            0.40,
            hint,
            ha="center",
            va="center",
            fontsize=9,
            color="#999999",
            wrap=True,
        )
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    fig.tight_layout()

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=_DPI)
    plt.close(fig)
    return f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"


def timeseries_chart(
    series: dict[str, tuple[list[float], list[float]]],
    title: str,
    ylabel: str,
    outages: Sequence[tuple[float, float]] = (),
    markers: Sequence[tuple[float, str]] = (),
    empty_hint: str = "",
) -> str:
    """Zeichnet eine oder mehrere Zeitreihen.

    Args:
        series: Name -> (Zeitstempel, Werte).
        title: Diagrammtitel.
        ylabel: Beschriftung der Y-Achse.
        outages: Zeitraeume, die als rote Flaeche hinterlegt werden.
        markers: Einzelzeitpunkte mit Beschriftung (z.B. Router-Neustarts).
        empty_hint: Erklaerung, die im Platzhalter erscheint, falls keine
            Messwerte vorliegen.

    Returns:
        Das Diagramm als ``data:``-URI.
    """
    usable = {name: data for name, data in series.items() if data[0]}
    if not usable:
        return _empty_note(title, empty_hint)

    fig, ax = plt.subplots(figsize=_FIGSIZE)
    for index, (name, (timestamps, values)) in enumerate(sorted(usable.items())):
        ax.plot(
            _to_datetimes(timestamps),
            values,
            label=name,
            color=COLORS[index % len(COLORS)],
            linewidth=1.2,
        )

    for start, end in outages:
        ax.axvspan(
            *_to_datetimes([start, max(end, start + 1)]),
            color=_OUTAGE_COLOR,
            alpha=0.18,
            zorder=0,
        )

    for position, label in markers:
        moment = _to_datetimes([position])[0]
        ax.axvline(moment, color=_MARKER_COLOR, linestyle="--", linewidth=1.1, zorder=1)
        ax.annotate(
            label,
            xy=(moment, ax.get_ylim()[1]),
            xytext=(3, -10),
            textcoords="offset points",
            fontsize=7,
            color=_MARKER_COLOR,
            rotation=90,
            va="top",
        )

    ax.set_title(title, fontsize=11, loc="left")

    # Auch die Flaechen und Marken gehoeren in die Legende. Ohne sie muss man
    # aus der Bildunterschrift erraten, wofuer die rote Flaeche steht - und
    # rot ist ausserdem eine der Kurvenfarben.
    handles: list[Artist] = list(ax.get_legend_handles_labels()[0])
    if outages:
        handles.append(Patch(facecolor=_OUTAGE_COLOR, alpha=0.18, label="Ausfall"))
    if markers:
        handles.append(
            Line2D([], [], color=_MARKER_COLOR, linestyle="--", linewidth=1.1, label="Neustart")
        )
    if len(handles) > 1:
        ax.legend(handles=handles, loc="upper right", fontsize=8, framealpha=0.9)
    return _finish(fig, ax, ylabel)


def bar_chart(labels: Sequence[str], values: Sequence[float], title: str, ylabel: str) -> str:
    """Zeichnet ein einfaches Balkendiagramm (z.B. fuer den Vergleichsmodus)."""
    if not labels:
        return _empty_note(title)

    fig, ax = plt.subplots(figsize=(_FIGSIZE[0], 3.2))
    bars = ax.bar(list(labels), list(values), color=COLORS[: len(labels)], width=0.55)
    for bar, value in zip(bars, values, strict=True):
        ax.annotate(
            f"{value:.2f}",
            xy=(bar.get_x() + bar.get_width() / 2, value),
            xytext=(0, 3),
            textcoords="offset points",
            ha="center",
            fontsize=9,
        )
    ax.set_title(title, fontsize=11, loc="left")
    ax.set_ylabel(ylabel)
    ax.grid(True, axis="y", alpha=0.25, linestyle=":")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=_DPI)
    plt.close(fig)
    return f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"


def histogram_chart(values: Sequence[float], title: str, xlabel: str, bins: int = 40) -> str:
    """Zeichnet ein Histogramm (z.B. Latenzverteilung)."""
    if not values:
        return _empty_note(title)

    fig, ax = plt.subplots(figsize=(_FIGSIZE[0], 3.2))
    ax.hist(list(values), bins=bins, color=COLORS[0], alpha=0.85)
    ax.set_title(title, fontsize=11, loc="left")
    ax.set_xlabel(xlabel)
    ax.set_ylabel("Anzahl")
    ax.grid(True, axis="y", alpha=0.25, linestyle=":")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=_DPI)
    plt.close(fig)
    return f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"
