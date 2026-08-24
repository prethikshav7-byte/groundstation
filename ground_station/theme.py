"""
ground_station/theme.py
═══════════════════════
Dark-mode colour palette and global stylesheet.

Every colour reference in the UI goes through ThemeManager.C() so that a
palette swap is a one-place edit and so that widgets can be built before
the QApplication stylesheet is applied.  Nothing here touches Qt directly
except the stylesheet generation, so the palette dataclass is importable
in tests.
"""
from __future__ import annotations

from dataclasses import dataclass


# ── Typographic scale (#2) ────────────────────────────────────────────────────
# Three levels, used everywhere a font size is needed.  Import these rather
# than hard-coding a number so the whole app stays on the same grid.
FS_HEADING: int = 11    # page titles, section headings
FS_BODY:    int = 9     # labels, button text, table cells
FS_CAPTION: int = 8     # dim secondary labels, timestamps, unit text
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Palette:
    """The full colour palette used across all widgets."""
    # Backgrounds
    BG:            str = "#0d1117"   # window / outermost layer
    BG_CARD:       str = "#161b22"   # card / panel backgrounds
    BG_CARD_HOVER: str = "#1f2937"   # card hover / highlight
    BG_PANEL:      str = "#13181f"   # pinned strip backgrounds (health bar)
    BG_INPUT:      str = "#21262d"   # input fields, button backgrounds

    # Text
    TEXT:      str = "#e6edf3"   # primary readable text
    TEXT_DIM:  str = "#8b949e"   # labels, secondary text
    TEXT_MUTED:str = "#484f58"   # disabled / placeholder text

    # Accents
    CYAN:   str = "#58a6ff"   # primary highlight / selection
    GREEN:  str = "#3fb950"   # success / connected / OK
    AMBER:  str = "#d29922"   # warning / pending
    RED:    str = "#f85149"   # error / danger / lost

    # Structural
    BORDER: str = "#30363d"   # widget borders / grid lines

    # Gauge-specific
    GAUGE_BG:   str = "#21262d"   # dial arc background track
    GAUGE_TICK: str = "#484f58"   # tick marks on the dial


class ThemeManager:
    """Central access point for the application palette and stylesheet.

    Usage::

        c = ThemeManager.C()
        widget.setStyleSheet(f"color: {c.CYAN};")

    The stylesheet is applied once at application start::

        app.setStyleSheet(ThemeManager.main_stylesheet())
    """

    _palette: Palette = Palette()

    @classmethod
    def C(cls) -> Palette:
        """Return the active colour palette."""
        return cls._palette

    @classmethod
    def sidebar_stylesheet(cls) -> str:
        """Stylesheet for the left navigation sidebar."""
        c = cls._palette
        return f"""
            QWidget {{
                background-color: {c.BG_CARD};
                border-right: 1px solid {c.BORDER};
            }}
            QPushButton {{
                background: transparent;
                border: none;
                border-radius: 4px;
                color: {c.TEXT_DIM};
                text-align: left;
                padding: 8px 12px;
                font-size: 10pt;
            }}
            QPushButton:hover {{
                background-color: {c.BG_CARD_HOVER};
                color: {c.TEXT};
            }}
            QPushButton:checked {{
                background-color: {c.BG_INPUT};
                color: {c.CYAN};
                font-weight: bold;
            }}
        """

    @classmethod
    def main_stylesheet(cls) -> str:
        """Global Qt stylesheet for the entire application."""
        c = cls._palette
        return f"""
            QMainWindow, QWidget {{
                background-color: {c.BG};
                color: {c.TEXT};
                font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
                font-size: 10pt;
            }}

            QScrollArea {{
                border: none;
                background-color: {c.BG};
            }}

            QScrollBar:vertical {{
                background: {c.BG};
                width: 8px;
                margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: {c.BORDER};
                border-radius: 4px;
                min-height: 24px;
            }}
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {{
                height: 0;
            }}

            QScrollBar:horizontal {{
                background: {c.BG};
                height: 8px;
                margin: 0;
            }}
            QScrollBar::handle:horizontal {{
                background: {c.BORDER};
                border-radius: 4px;
                min-width: 24px;
            }}
            QScrollBar::add-line:horizontal,
            QScrollBar::sub-line:horizontal {{
                width: 0;
            }}

            QTabWidget::pane {{
                border: 1px solid {c.BORDER};
                background-color: {c.BG_CARD};
            }}
            QTabBar::tab {{
                background-color: {c.BG_INPUT};
                color: {c.TEXT_DIM};
                border: 1px solid {c.BORDER};
                padding: 6px 14px;
                margin-right: 2px;
            }}
            QTabBar::tab:selected {{
                background-color: {c.BG_CARD};
                color: {c.CYAN};
                border-bottom: 2px solid {c.CYAN};
            }}
            QTabBar::tab:hover {{
                background-color: {c.BG_CARD_HOVER};
                color: {c.TEXT};
            }}

            QPushButton {{
                background-color: {c.BG_INPUT};
                color: {c.TEXT};
                border: 1px solid {c.BORDER};
                border-radius: 4px;
                padding: 5px 12px;
            }}
            QPushButton:hover {{
                background-color: {c.BG_CARD_HOVER};
            }}
            QPushButton:pressed {{
                background-color: {c.BG_CARD};
            }}
            QPushButton:disabled {{
                color: {c.TEXT_MUTED};
                border-color: {c.BG_INPUT};
            }}

            QComboBox {{
                background-color: {c.BG_INPUT};
                color: {c.TEXT};
                border: 1px solid {c.BORDER};
                border-radius: 4px;
                padding: 4px 8px;
            }}
            QComboBox::drop-down {{
                border: none;
            }}
            QComboBox QAbstractItemView {{
                background-color: {c.BG_CARD};
                color: {c.TEXT};
                selection-background-color: {c.BG_CARD_HOVER};
            }}

            QLabel {{
                background: transparent;
            }}

            QFrame[frameShape="4"],
            QFrame[frameShape="5"] {{
                color: {c.BORDER};
            }}

            QProgressBar {{
                background-color: {c.BG_INPUT};
                border: 1px solid {c.BORDER};
                border-radius: 3px;
                text-align: center;
                color: {c.TEXT};
            }}
            QProgressBar::chunk {{
                background-color: {c.CYAN};
                border-radius: 3px;
            }}

            QCheckBox {{
                color: {c.TEXT};
                spacing: 6px;
            }}
            QCheckBox::indicator {{
                width: 14px;
                height: 14px;
                border: 1px solid {c.BORDER};
                border-radius: 3px;
                background: {c.BG_INPUT};
            }}
            QCheckBox::indicator:checked {{
                background: {c.CYAN};
                border-color: {c.CYAN};
            }}

            QSplitter::handle {{
                background: {c.BORDER};
            }}
            QSplitter::handle:horizontal {{
                width: 1px;
            }}
            QSplitter::handle:vertical {{
                height: 1px;
            }}

            QStatusBar {{
                background-color: {c.BG_CARD};
                color: {c.TEXT_DIM};
                border-top: 1px solid {c.BORDER};
            }}

            QMenuBar {{
                background-color: {c.BG_CARD};
                color: {c.TEXT};
                border-bottom: 1px solid {c.BORDER};
            }}
            QMenuBar::item:selected {{
                background-color: {c.BG_CARD_HOVER};
            }}
            QMenu {{
                background-color: {c.BG_CARD};
                color: {c.TEXT};
                border: 1px solid {c.BORDER};
            }}
            QMenu::item:selected {{
                background-color: {c.BG_CARD_HOVER};
            }}

            QToolTip {{
                background-color: {c.BG_CARD};
                color: {c.TEXT};
                border: 1px solid {c.BORDER};
                padding: 4px;
            }}

            QLineEdit {{
                background-color: {c.BG_INPUT};
                color: {c.TEXT};
                border: 1px solid {c.BORDER};
                border-radius: 4px;
                padding: 4px 8px;
                selection-background-color: {c.CYAN};
                selection-color: {c.BG};
            }}
            QLineEdit:focus {{
                border-color: {c.CYAN};
            }}

            QTextEdit, QPlainTextEdit {{
                background-color: {c.BG_INPUT};
                color: {c.TEXT};
                border: 1px solid {c.BORDER};
                border-radius: 4px;
                selection-background-color: {c.CYAN};
                selection-color: {c.BG};
            }}
            QTextEdit:focus, QPlainTextEdit:focus {{
                border-color: {c.CYAN};
            }}
        """
