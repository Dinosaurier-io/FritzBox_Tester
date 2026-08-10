"""Programmsymbole - vollstaendig selbst gezeichnet.

Bewusst keine Herstellermarken und keine fremden Grafiken: Das Werkzeug misst
FRITZ!Box-Router, stammt aber nicht von AVM. Ein fremdes Logo waere rechtlich
heikel und sachlich irrefuehrend.

Das Motiv ist ein stilisierter Funkbogen ueber einer Grundlinie - Sinnbild fuer
Verbindungsqualitaet. Die Farbe des Bogens zeigt im Infobereich den Zustand an,
sodass ein Blick genuegt:

* blau  - bereit, kein Testlauf
* gruen - Testlauf laeuft, alles in Ordnung
* rot   - Ausfall aktiv

Die Symbole werden zur Laufzeit gezeichnet statt als Bilddateien mitgeliefert.
So gibt es keine Binaerdateien im Repository, jede Groesse ist verlustfrei
verfuegbar, und die Zustandsfarben lassen sich an einer Stelle aendern.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from PIL import Image, ImageDraw

#: Hintergrund des Symbols (dunkel, passend zur Oberflaeche).
_BACKGROUND = (18, 21, 26, 255)


class IconState(StrEnum):
    """Zustand, den das Symbol anzeigt."""

    IDLE = "idle"
    RUNNING = "running"
    OUTAGE = "outage"

    @property
    def color(self) -> tuple[int, int, int, int]:
        """Farbe des Funkbogens."""
        return {
            IconState.IDLE: (77, 163, 255, 255),
            IconState.RUNNING: (63, 185, 80, 255),
            IconState.OUTAGE: (240, 85, 58, 255),
        }[self]

    @property
    def tooltip(self) -> str:
        """Beschriftung fuer den Infobereich.

        Bewusst nicht ``title``: Diese Aufzaehlung erbt von ``str``, wo
        ``title()`` bereits eine Methode ist. Ein gleichnamiges Attribut haette
        sie ueberdeckt.
        """
        return {
            IconState.IDLE: "FRITZ!Box-Langzeittest - bereit",
            IconState.RUNNING: "FRITZ!Box-Langzeittest - Testlauf laeuft",
            IconState.OUTAGE: "FRITZ!Box-Langzeittest - AUSFALL",
        }[self]


def draw_icon(size: int = 64, state: IconState = IconState.IDLE) -> Image.Image:
    """Zeichnet das Programmsymbol.

    Args:
        size: Kantenlaenge in Pixeln.
        state: Zustand, der die Farbe bestimmt.

    Returns:
        Das fertige Bild mit Transparenz.
    """
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    unit = size / 64

    # Abgerundeter Hintergrund - hebt das Symbol auf hellen wie dunklen
    # Taskleisten ab.
    draw.rounded_rectangle(
        [(0, 0), (size - 1, size - 1)], radius=int(12 * unit), fill=_BACKGROUND
    )

    color = state.color
    center_x, base_y = size / 2, size * 0.72

    # Drei Funkboegen, nach aussen hin duenner und blasser.
    for index, radius_factor in enumerate((0.16, 0.28, 0.40)):
        radius = size * radius_factor
        width = max(1, int((3 - index * 0.6) * unit))
        alpha = 255 - index * 55
        draw.arc(
            [
                (center_x - radius, base_y - radius),
                (center_x + radius, base_y + radius),
            ],
            start=205,
            end=335,
            fill=(*color[:3], alpha),
            width=width,
        )

    # Sender am Fusspunkt.
    dot = size * 0.055
    draw.ellipse(
        [(center_x - dot, base_y - dot), (center_x + dot, base_y + dot)], fill=color
    )

    # Grundlinie - die "Messachse".
    draw.line(
        [(size * 0.22, base_y + 7 * unit), (size * 0.78, base_y + 7 * unit)],
        fill=(*color[:3], 150),
        width=max(1, int(2 * unit)),
    )
    return image


def write_png(path: Path, size: int = 256, state: IconState = IconState.IDLE) -> Path:
    """Schreibt das Symbol als PNG."""
    path.parent.mkdir(parents=True, exist_ok=True)
    draw_icon(size, state).save(path, format="PNG")
    return path


def write_ico(path: Path, state: IconState = IconState.IDLE) -> Path:
    """Schreibt das Symbol als Windows-ICO mit allen ueblichen Groessen.

    Windows waehlt je nach Anzeigeort eine andere Groesse; fehlt sie, skaliert
    es selbst und das Ergebnis wird unscharf.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    largest = draw_icon(256, state)
    largest.save(path, format="ICO", sizes=[(size, size) for size in sizes])
    return path
