"""
ground_station/pages/experiment.py
══════════════════════════════════
§11 — the CanSat Experiment dashboard.

§11.3 is the structural requirement and drives the whole layout: the two
halves are independent.

    Retrieval (§11.4–11.6)  needs a direct USB connection to the
                            recovered CanSat.
    Analysis  (§11.7–11.12) works on any local file, no hardware, in
                            either app mode.

"Usable separately. An operator with a file already on disk is never
forced through retrieval." So the file picker is always live, the
retrieval half degrades to a disabled panel with an explanation, and
nothing in the analysis half asks whether a vehicle is connected.

§11.9's parse report is given real space rather than a status line. "Data
quality must be visible before conclusions are drawn" — a report that has
to be opened is a report that gets skipped on the flight where it
mattered.
"""
from __future__ import annotations

import os
from typing import List, Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QVBoxLayout, QWidget,
)

from ..experiment.parser import (
    ExperimentDataset, ParseError, export_csv, parse_experiment_file,
)
from ..experiment.profile_plot import AerosolProfilePlot, SecondaryPlot
from ..experiment.transfer import (
    FileTransfer, RemoteFile, RetrievalNotifier, TransferState,
)
from ..models import FlightState
from ..theme import ThemeManager


def _btn_css(accent: str) -> str:
    c = ThemeManager.C()
    return (f"QPushButton {{ background-color: {c.BG_INPUT}; color: {accent}; "
            f"border: 1px solid {accent}; border-radius: 4px; "
            f"padding: 6px 12px; }}"
            f"QPushButton:hover {{ background-color: {c.BG_CARD_HOVER}; }}"
            f"QPushButton:disabled {{ color: {c.TEXT_MUTED}; "
            f"border-color: {c.BORDER}; }}")


def _heading(text: str) -> QLabel:
    c = ThemeManager.C()
    l = QLabel(text)
    l.setFont(QFont("monospace", 9, QFont.Weight.Bold))
    l.setStyleSheet(f"color: {c.CYAN};")
    return l


# ─────────────────────────────────────────────────────────────────────────────
#  §11.7 — parse off the UI thread
# ─────────────────────────────────────────────────────────────────────────────

class ParseWorker(QThread):
    """§11.7 — "Parse on a background thread with progress; a large file
    must never freeze the UI."

    A flight-length file is 100 KB–1 MB (§11.4). Parsing that inline would
    block the event loop for long enough to look like a hang, and the
    operator's reasonable response to a hung app is to kill it — losing
    the retrieved file if it has not been written yet.
    """

    finished_ok = pyqtSignal(object)        # ExperimentDataset
    failed = pyqtSignal(str)
    progress = pyqtSignal(int)              # percent

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.path = path

    def run(self) -> None:
        try:
            ds = parse_experiment_file(
                # parse_experiment_file reports 0.0–1.0, not a
                # percentage; scaling here rather than changing the
                # parser keeps that module free of UI assumptions.
                self.path,
                progress=lambda frac: self.progress.emit(int(frac * 100)))
            self.finished_ok.emit(ds)
        except ParseError as e:
            self.failed.emit(str(e))
        except Exception as e:                            # pragma: no cover
            self.failed.emit(f"Could not read the file: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  Retrieval half (§11.4–11.6)
# ─────────────────────────────────────────────────────────────────────────────

class RetrievalPanel(QWidget):
    """Direct-USB retrieval from the recovered CanSat."""

    file_retrieved = pyqtSignal(str)         # local path

    def __init__(self, parent=None):
        super().__init__(parent)
        self.transfer: Optional[FileTransfer] = None
        self.notifier = RetrievalNotifier()
        self._usb_direct = False
        self._state: Optional[FlightState] = None

        c = ThemeManager.C()
        self.setStyleSheet(
            f"background-color: {c.BG_CARD}; border: 1px solid {c.BORDER}; "
            f"border-radius: 6px;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)
        lay.addWidget(_heading("Retrieve Experiment Data"))

        self._why = QLabel("")
        self._why.setFont(QFont("monospace", 8))
        self._why.setStyleSheet(f"color: {c.AMBER};")
        self._why.setWordWrap(True)
        lay.addWidget(self._why)

        row = QHBoxLayout()
        self._list_button = QPushButton("List Files")
        self._list_button.setFont(QFont("monospace", 9))
        self._list_button.setStyleSheet(_btn_css(c.TEXT))
        self._list_button.clicked.connect(self._list_files)
        row.addWidget(self._list_button)

        self._files = QComboBox()
        self._files.setFont(QFont("monospace", 9))
        self._files.setStyleSheet(
            f"QComboBox {{ background-color: {c.BG_INPUT}; color: {c.TEXT}; "
            f"border: 1px solid {c.BORDER}; border-radius: 4px; padding: 5px; }}")
        row.addWidget(self._files, stretch=1)

        self._get_button = QPushButton("Retrieve")
        self._get_button.setFont(QFont("monospace", 9, QFont.Weight.Bold))
        self._get_button.setStyleSheet(_btn_css(c.CYAN))
        self._get_button.clicked.connect(self._retrieve)
        row.addWidget(self._get_button)

        self._abort_button = QPushButton("Abort")
        self._abort_button.setFont(QFont("monospace", 9))
        self._abort_button.setStyleSheet(_btn_css(c.RED))
        self._abort_button.clicked.connect(self._abort)
        self._abort_button.setVisible(False)
        row.addWidget(self._abort_button)
        lay.addLayout(row)

        self._bar = QProgressBar()
        self._bar.setVisible(False)
        self._bar.setStyleSheet(
            f"QProgressBar {{ background-color: {c.BG_INPUT}; "
            f"border: 1px solid {c.BORDER}; border-radius: 3px; "
            f"text-align: center; color: {c.TEXT}; }}"
            f"QProgressBar::chunk {{ background-color: {c.CYAN}; }}")
        lay.addWidget(self._bar)

        self._progress_text = QLabel("")
        self._progress_text.setFont(QFont("monospace", 8))
        self._progress_text.setStyleSheet(f"color: {c.TEXT_DIM};")
        lay.addWidget(self._progress_text)

        # §11.6 — non-blocking notification, never steals focus, never
        # blocks the dashboard.
        self._notice = QLabel("")
        self._notice.setFont(QFont("monospace", 8, QFont.Weight.Bold))
        self._notice.setStyleSheet(f"color: {c.GREEN};")
        self._notice.setWordWrap(True)
        self._notice.setVisible(False)
        lay.addWidget(self._notice)

        self._refresh_availability()

    # ── availability (§11.4) ─────────────────────────────────────────────

    def set_usb_direct(self, on: bool) -> None:
        self._usb_direct = on
        self._refresh_availability()

    def set_cansat_state(self, state: Optional[FlightState]) -> None:
        self._state = state
        self._refresh_availability()

    def _available(self) -> tuple[bool, str]:
        if not self._usb_direct:
            # §11.4 — "Disabled, with an explanatory message, whenever the
            # CanSat is reachable only via the LoRa relay." A flight-length
            # file over LoRa is tens of minutes to hours, starving
            # telemetry — and after recovery the CanSat is in hand anyway.
            return False, ("Retrieval needs a direct USB connection to the "
                           "recovered CanSat. Over the LoRa relay a "
                           "flight-length file would take tens of minutes "
                           "to hours and would starve telemetry.")
        if self._state is not FlightState.RECOVERY:
            # §11.5 — blocked outside RECOVERY. Never during flight.
            return False, ("Retrieval is available in RECOVERY only, never "
                           "during flight.")
        return True, ""

    def _refresh_availability(self) -> None:
        ok, why = self._available()
        self._list_button.setEnabled(ok)
        self._get_button.setEnabled(ok and self._files.count() > 0)
        self._files.setEnabled(ok)
        self._why.setText(why)
        self._why.setVisible(bool(why))

    # ── transfer (§11.5) ─────────────────────────────────────────────────

    def attach_transfer(self, transfer: FileTransfer) -> None:
        self.transfer = transfer

    def _list_files(self) -> None:
        if self.transfer is not None:
            self.transfer.request_list()

    def note_listing(self, files: List[RemoteFile]) -> None:
        self._files.clear()
        for f in files:
            self._files.addItem(f.label, userData=f)
        self._refresh_availability()

        # §11.6 — fires once per file per session. "A notification that
        # reappears gets dismissed reflexively, including on the flight
        # that mattered."
        for f in files:
            if self.notifier.should_announce(f):
                self._notice.setText(self.notifier.message(f))
                self._notice.setVisible(True)
                break

    def _retrieve(self) -> None:
        f: Optional[RemoteFile] = self._files.currentData()
        if f is None or self.transfer is None:
            return
        self.transfer.request_file(f.name)
        self._bar.setVisible(True)
        self._abort_button.setVisible(True)

    def _abort(self) -> None:
        if self.transfer is not None:
            self.transfer.abort()
        self._bar.setVisible(False)
        self._abort_button.setVisible(False)

    def refresh_progress(self) -> None:
        if self.transfer is None:
            return
        p = self.transfer.progress
        self._bar.setValue(int(p.percent))
        eta = p.eta_s()
        self._progress_text.setText(
            f"{p.percent:.0f}%   {p.received:,} / {p.total:,} bytes   "
            f"elapsed {p.elapsed_s:.0f} s   "
            + (f"remaining ~{eta:.0f} s" if eta is not None else "remaining —")
            + (f"   retried {p.chunks_retried}" if p.chunks_retried else ""))

        if self.transfer.state is TransferState.COMPLETE:
            self._bar.setVisible(False)
            self._abort_button.setVisible(False)
            # §11.5 — written to a persistent directory with flight date
            # and vehicle in the filename, only after whole-file checksum
            # verification. A silently truncated file that parses cleanly
            # is the worst failure here.
            path = self.transfer.save("experiment_data", vehicle="CANSAT")
            self.file_retrieved.emit(path)


# ─────────────────────────────────────────────────────────────────────────────
#  The page
# ─────────────────────────────────────────────────────────────────────────────

class ExperimentPage(QWidget):
    """§11 — retrieval and analysis, usable independently (§11.3)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.dataset: Optional[ExperimentDataset] = None
        self._worker: Optional[ParseWorker] = None
        self._build()

    def _build(self) -> None:
        c = ThemeManager.C()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        head = QLabel("CanSat Experiment")
        head.setFont(QFont("monospace", 13, QFont.Weight.Bold))
        head.setStyleSheet(f"color: {c.CYAN};")
        lay.addWidget(head)

        # ── retrieval half ───────────────────────────────────────────────
        self.retrieval = RetrievalPanel()
        self.retrieval.file_retrieved.connect(self.load_file)
        lay.addWidget(self.retrieval)

        # ── analysis half (§11.8) ────────────────────────────────────────
        lay.addWidget(self._file_panel())

        split = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        self.profile = AerosolProfilePlot()
        self.profile.setMinimumHeight(340)
        left_lay.addWidget(self._plot_controls())
        left_lay.addWidget(self.profile, stretch=1)

        secondaries = QWidget()
        sec_lay = QHBoxLayout(secondaries)
        sec_lay.setContentsMargins(0, 0, 0, 0)
        self.alt_time = SecondaryPlot("altitude")
        self.aero_time = SecondaryPlot("aerosol")
        for p in (self.alt_time, self.aero_time):
            p.setMinimumHeight(180)
            sec_lay.addWidget(p)
        left_lay.addWidget(secondaries)

        split.addWidget(left)
        split.addWidget(self._report_panel())
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        lay.addWidget(split, stretch=1)

        scroll.setWidget(body)
        outer.addWidget(scroll)

    def _file_panel(self) -> QWidget:
        c = ThemeManager.C()
        w = QWidget()
        w.setStyleSheet(
            f"background-color: {c.BG_CARD}; border: 1px solid {c.BORDER}; "
            f"border-radius: 6px;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)

        row = QHBoxLayout()
        row.addWidget(_heading("Analysis"))
        row.addStretch()

        # §11.3 — always available, with no hardware and in either app
        # mode. An operator with a file on disk is never routed through
        # retrieval to reach it.
        pick = QPushButton("Open CSV file…")
        pick.setFont(QFont("monospace", 9))
        pick.setStyleSheet(_btn_css(c.CYAN))
        pick.clicked.connect(self._pick_file)
        row.addWidget(pick)

        self._export_csv = QPushButton("Export data")
        self._export_csv.setFont(QFont("monospace", 9))
        self._export_csv.setStyleSheet(_btn_css(c.TEXT))
        self._export_csv.clicked.connect(self._do_export_csv)
        self._export_csv.setEnabled(False)
        row.addWidget(self._export_csv)

        self._export_png = QPushButton("Export figure")
        self._export_png.setFont(QFont("monospace", 9))
        self._export_png.setStyleSheet(_btn_css(c.TEXT))
        self._export_png.clicked.connect(self._do_export_png)
        self._export_png.setEnabled(False)
        row.addWidget(self._export_png)
        lay.addLayout(row)

        # §11.8 — name, path, size, row count and parsed time range, so
        # the operator can confirm this is the right flight.
        self._file_info = QLabel("No file loaded.")
        self._file_info.setFont(QFont("monospace", 8))
        self._file_info.setStyleSheet(f"color: {c.TEXT_DIM};")
        self._file_info.setWordWrap(True)
        lay.addWidget(self._file_info)

        self._parse_bar = QProgressBar()
        self._parse_bar.setVisible(False)
        lay.addWidget(self._parse_bar)
        return w

    def _plot_controls(self) -> QWidget:
        c = ThemeManager.C()
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 4)

        # §11.11 — ascent only / descent only / both.
        self._ascent = QCheckBox("Ascent")
        self._descent = QCheckBox("Descent")
        for cb in (self._ascent, self._descent):
            cb.setChecked(True)
            cb.setFont(QFont("monospace", 8))
            cb.setStyleSheet(f"color: {c.TEXT};")
            cb.stateChanged.connect(self._update_legs)
            row.addWidget(cb)

        row.addStretch()

        # §11.10 — axis-swap toggle, defaulting to the specified
        # orientation (aerosol on y, altitude on x).
        self._swap = QCheckBox("Altitude on Y (conventional profile)")
        self._swap.setFont(QFont("monospace", 8))
        self._swap.setStyleSheet(f"color: {c.TEXT};")
        self._swap.stateChanged.connect(
            lambda: self.profile.set_swapped(self._swap.isChecked()))
        row.addWidget(self._swap)

        reset = QPushButton("Reset view")
        reset.setFont(QFont("monospace", 8))
        reset.setStyleSheet(_btn_css(c.TEXT))
        reset.clicked.connect(self.profile.reset_view)
        row.addWidget(reset)
        return w

    def _report_panel(self) -> QWidget:
        c = ThemeManager.C()
        w = QWidget()
        w.setStyleSheet(
            f"background-color: {c.BG_CARD}; border: 1px solid {c.BORDER}; "
            f"border-radius: 6px;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.addWidget(_heading("Parse Report"))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._report = QLabel("No file loaded.")
        self._report.setFont(QFont("monospace", 8))
        self._report.setStyleSheet(f"color: {c.TEXT_DIM};")
        self._report.setWordWrap(True)
        self._report.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(self._report)
        lay.addWidget(scroll)
        return w

    # ── loading ──────────────────────────────────────────────────────────

    def _pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open experiment CSV", "", "CSV files (*.csv);;All files (*)")
        if path:
            self.load_file(path)

    def load_file(self, path: str) -> None:
        self._parse_bar.setVisible(True)
        self._parse_bar.setValue(0)
        self._worker = ParseWorker(path)
        self._worker.progress.connect(self._parse_bar.setValue)
        self._worker.finished_ok.connect(self._on_parsed)
        self._worker.failed.connect(self._on_parse_failed)
        self._worker.start()

    def _on_parsed(self, ds: ExperimentDataset) -> None:
        c = ThemeManager.C()
        self._parse_bar.setVisible(False)
        self.dataset = ds

        self.profile.set_dataset(ds)
        self.alt_time.set_dataset(ds)
        self.aero_time.set_dataset(ds)
        self._export_csv.setEnabled(True)
        self._export_png.setEnabled(True)

        r = ds.report
        size = os.path.getsize(r.path) if r.path and os.path.exists(r.path) else 0
        tr = r.time_range
        self._file_info.setText(
            f"{os.path.basename(r.path)}   {size / 1024:.0f} KB   "
            f"{len(ds):,} rows   "
            + (f"T+{tr[0]:.1f} s to T+{tr[1]:.1f} s" if tr else "no time range")
            + f"\n{r.path}")
        self._file_info.setStyleSheet(f"color: {c.TEXT_DIM};")
        self._report.setText("\n".join(r.summary_lines()))

    def _on_parse_failed(self, message: str) -> None:
        c = ThemeManager.C()
        self._parse_bar.setVisible(False)
        # §11.7 — "Reject the file with a clear message if no rows parse,
        # rather than showing an empty graph that reads as 'the experiment
        # recorded nothing'."
        self.dataset = None
        self.profile.set_dataset(None)
        self.alt_time.set_dataset(None)
        self.aero_time.set_dataset(None)
        self._export_csv.setEnabled(False)
        self._export_png.setEnabled(False)
        self._file_info.setText(message)
        self._file_info.setStyleSheet(f"color: {c.RED};")
        self._report.setText(message)

    def _update_legs(self) -> None:
        self.profile.set_legs(self._ascent.isChecked(),
                              self._descent.isChecked())

    # ── §11.12 export ────────────────────────────────────────────────────

    def _do_export_csv(self) -> None:
        if self.dataset is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export parsed data", "experiment_parsed.csv",
            "CSV files (*.csv)")
        if path:
            # §12.7 — exports go to a separate operator-chosen path; the
            # retrieved file itself is never modified.
            export_csv(self.dataset, path)

    def _do_export_png(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export figure", "aerosol_profile.png", "PNG image (*.png)")
        if path:
            self.profile.plot.grab().save(path)
