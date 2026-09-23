"""
ground_station/pages/com.py
═══════════════════════════
PHOENIX dB.V1 — COM / Telemetry Communication Console
═════════════════════════════════════════════════════

Three-Phase Aerospace Telemetry Workspace:
  Phase 01: Link Status & Packet Journey Pipeline
  Phase 02: Dual Live Packet Streams (Rocket & CanSat) + Raw/Decoded Inspector
  Phase 03: Integrated Simulation Engine & Communication Metrics

Consumes live parsed telemetry from the shared pipeline (_dispatch / Demux).
All simulation packets travel through the exact same CsvCodec / Demux / Graph
pipeline and are clearly labeled SIMULATED / [SIM].
"""
from __future__ import annotations

from collections import deque
import math
import time
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from PyQt6.QtCore import QAbstractListModel, QModelIndex, QPoint, QRect, QSize, Qt, QTimer
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QApplication, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
    QListView, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from ..codec import CsvCodec, encode_heartbeat
from ..graphs.trajectory import FlightProfile
from ..models import (
    FlightState, ReceiverState, SourceState, TelemetryPacket, VehicleID,
    VehicleLinkState,
)
from ..parser import IngestResult, LossTier
from ..simulator.engine import SimulatedFlight
from ..theme import ThemeManager, FS_BODY, FS_CAPTION, FS_HEADING


STATE_NAMES: Dict[int, str] = {
    0: "BOOT",
    1: "PRELAUNCH",
    2: "BOOST",
    3: "COAST",
    4: "APOGEE",
    5: "DESCENT",
    6: "LANDING",
    7: "RECOVERY",
}


# ── Packet Record ─────────────────────────────────────────────────────────────

class PacketRecord:
    """Stored packet with decoded attributes and raw wire frame."""
    __slots__ = (
        "packet", "received_at", "derived_v", "raw_line", "vehicle_id",
        "packet_count", "mission_time", "state", "altitude", "velocity",
        "pressure", "temperature", "voltage", "gnss_time", "gnss_lat",
        "gnss_lon", "gnss_alt", "gnss_sats", "accel", "spin", "rssi",
        "snr", "is_sim", "is_valid", "prefix", "prefix_desc", "team_id",
        "flight_state_code", "flight_state_name", "vehicle_code",
        "vehicle_name", "checksum",
    )

    def __init__(self, packet: TelemetryPacket, derived_v: Optional[float] = None,
                 is_valid: bool = True):
        self.packet = packet
        self.received_at = packet.received_at if packet.received_at > 0 else time.monotonic()
        self.derived_v = derived_v
        self.raw_line = packet.raw or ""
        self.vehicle_id = packet.vehicle_id
        self.packet_count = packet.packet_count
        self.mission_time = packet.mission_time
        self.state = packet.state
        self.altitude = packet.altitude

        v_val = packet.velocity if packet.velocity is not None else derived_v
        self.velocity = v_val

        self.pressure = packet.pressure
        self.temperature = packet.temperature
        self.voltage = packet.battery_voltage
        self.gnss_time = packet.gnss_time if packet.gnss_time and packet.gnss_time.strip() else "NOT AVAILABLE"
        self.gnss_lat = packet.gnss_latitude
        self.gnss_lon = packet.gnss_longitude
        self.gnss_alt = packet.gnss_altitude
        self.gnss_sats = packet.gnss_satellites

        # Accel magnitude / corrected
        accel_val = packet.accelerometer
        if accel_val is None and packet.raw_accelerometer is not None:
            try:
                accel_val = float(packet.raw_accelerometer)
            except ValueError:
                accel_val = None
        elif accel_val is None and (
            packet.accel_x != 0.0 or packet.accel_y != 0.0 or packet.accel_z != 0.0
        ):
            accel_val = math.sqrt(packet.accel_x ** 2 + packet.accel_y ** 2 + packet.accel_z ** 2)
        self.accel = accel_val

        self.spin = packet.gyro_spin_rate if packet.gyro_spin_rate != 0.0 else packet.gyro_z
        self.rssi = packet.rssi
        self.snr = packet.snr
        self.is_sim = getattr(packet, "is_simulation", False)
        self.is_valid = is_valid

        # Prefix ($T, $H, $C, $F, PKT)
        raw_text = self.raw_line.strip()
        if raw_text.startswith("$T"):
            self.prefix = "$T"
            self.prefix_desc = "Telemetry (rocket to ground)"
        elif raw_text.startswith("$H"):
            self.prefix = "$H"
            self.prefix_desc = "Status (receiver to ground)"
        elif raw_text.startswith("$C"):
            self.prefix = "$C"
            self.prefix_desc = "Command (ground to rocket)"
        elif raw_text.startswith("$F"):
            self.prefix = "$F"
            self.prefix_desc = "File-Transfer Frame (rocket to ground)"
        elif raw_text.startswith("PKT="):
            self.prefix = "PKT"
            self.prefix_desc = "Telemetry (KEY=VALUE)"
        else:
            self.prefix = "$T"
            self.prefix_desc = "Telemetry"

        # Team ID
        self.team_id = str(packet.team_id) if packet.team_id is not None and str(packet.team_id).strip() else "NOT AVAILABLE"

        # Flight State (0..7)
        if packet.state is not None:
            code = packet.state.ordinal if hasattr(packet.state, "ordinal") else getattr(packet, "flight_state_code", 0)
            self.flight_state_code = code
            self.flight_state_name = STATE_NAMES.get(code, packet.state.value)
        else:
            code = getattr(packet, "flight_state_code", 0)
            self.flight_state_code = code
            self.flight_state_name = STATE_NAMES.get(code, "UNKNOWN")

        # Vehicle (1 = ROCKET, 2 = CANSAT)
        if packet.vehicle_id is VehicleID.ROCKET:
            self.vehicle_code = 1
            self.vehicle_name = "ROCKET"
        elif packet.vehicle_id is VehicleID.CANSAT:
            self.vehicle_code = 2
            self.vehicle_name = "CANSAT"
        else:
            self.vehicle_code = getattr(packet, "vehicle_code", 1)
            self.vehicle_name = packet.vehicle_id.value if packet.vehicle_id else "ROCKET"

        # Checksum
        if packet.checksum:
            self.checksum = str(packet.checksum)
        elif self.raw_line and "," in self.raw_line:
            parts = [p.strip() for p in self.raw_line.split(",") if p.strip()]
            if len(parts) >= 2:
                last_tok = parts[-1].split("*")[-1].split("=")[-1].strip()
                self.checksum = last_tok if (len(last_tok) == 2 or last_tok.isalnum()) else "NOT AVAILABLE"
            else:
                self.checksum = "NOT AVAILABLE"
        else:
            self.checksum = "NOT AVAILABLE"


# ── Packet List Model ─────────────────────────────────────────────────────────

class PacketListModel(QAbstractListModel):
    """High-performance list model storing newest packets at index 0."""
    MAX_CAPACITY = 5000

    def __init__(self, vehicle_id: VehicleID, parent=None):
        super().__init__(parent)
        self.vehicle_id = vehicle_id
        self._packets: Deque[PacketRecord] = deque(maxlen=self.MAX_CAPACITY)

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._packets)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._packets)):
            return None
        if role == Qt.ItemDataRole.UserRole:
            return self._packets[index.row()]
        return None

    def insert_packet(self, record: PacketRecord) -> None:
        if len(self._packets) >= self.MAX_CAPACITY:
            self.beginRemoveRows(QModelIndex(), len(self._packets) - 1, len(self._packets) - 1)
            self._packets.pop()
            self.endRemoveRows()
        self.beginInsertRows(QModelIndex(), 0, 0)
        self._packets.appendleft(record)
        self.endInsertRows()

    def get_packet(self, row: int) -> Optional[PacketRecord]:
        if 0 <= row < len(self._packets):
            return self._packets[row]
        return None

    def clear(self) -> None:
        self.beginResetModel()
        self._packets.clear()
        self.endResetModel()


# ── Packet Card Delegate ──────────────────────────────────────────────────────

class PacketCardDelegate(QStyledItemDelegate):
    """Renders a compact technical aerospace packet telemetry card (half-height)."""

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(option.rect.width(), 36)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        record: Optional[PacketRecord] = index.data(Qt.ItemDataRole.UserRole)
        if not record:
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = option.rect.adjusted(2, 1, -2, -1)
        c = ThemeManager.C()

        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)

        # Background
        bg_color = QColor(c.BG_CARD_HOVER if is_selected else c.BG_CARD)
        border_color = QColor(c.CYAN if is_selected else c.BORDER)

        painter.setBrush(QBrush(bg_color))
        painter.setPen(QPen(border_color, 1.2 if is_selected else 0.8))
        painter.drawRoundedRect(rect, 3, 3)

        # ── Line 1: Header (Packet Count, Time, State, Badge) ─────────────────
        painter.setFont(QFont("monospace", 7, QFont.Weight.Bold))

        # Packet Count
        p_num = f"#{record.packet_count:04d}"
        painter.setPen(QColor(c.CYAN))
        painter.drawText(rect.x() + 6, rect.y() + 13, p_num)

        # Timestamp
        painter.setFont(QFont("monospace", 7))
        painter.setPen(QColor(c.TEXT_DIM))
        t_str = f"T+{record.mission_time:.2f}s"
        painter.drawText(rect.x() + 52, rect.y() + 13, t_str)

        # State Badge (Code + Name)
        st_text = f"{record.flight_state_code} — {record.flight_state_name}"
        st_color = QColor(
            c.CYAN if record.flight_state_name == "APOGEE"
            else (c.AMBER if record.flight_state_name in ("BOOST", "DESCENT")
                  else (c.GREEN if record.flight_state_name in ("LANDING", "RECOVERY") else c.TEXT))
        )
        painter.setFont(QFont("monospace", 7, QFont.Weight.Bold))
        painter.setPen(st_color)
        painter.drawText(
            QRect(rect.x() + 115, rect.y() + 1, rect.width() - 170, 14),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            st_text,
        )

        # Status / Sim Badge
        badge_text = "[SIM]" if record.is_sim else ("VALID" if record.is_valid else "ERR")
        badge_color = QColor("#8b5cf6" if record.is_sim else (c.GREEN if record.is_valid else c.RED))
        painter.setPen(badge_color)
        painter.drawText(
            QRect(rect.right() - 50, rect.y() + 1, 46, 14),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            badge_text,
        )

        # ── Line 2: Telemetry Stream Values (Alt, Vel, P, T, V, GNSS) ─────────
        painter.setFont(QFont("monospace", 7))
        painter.setPen(QColor(c.TEXT))

        vel_label = "DESCENT" if record.vehicle_code == 2 else "VEL"
        vel_str = f"{record.velocity:.1f}m/s" if record.velocity is not None else "—"
        alt_str = f"{record.altitude:.1f}m"
        p_str = f"{record.pressure:.0f}Pa"
        t_val = f"{record.temperature:.1f}°C"
        v_val = f"{record.voltage:.2f}V"

        t_line = f"ALT {alt_str} │ {vel_label} {vel_str} │ P {p_str} │ T {t_val} │ {v_val} │ {record.gnss_lat:.4f}°,{record.gnss_lon:.4f}°"
        painter.drawText(rect.x() + 6, rect.y() + 27, t_line)

        painter.restore()


# ── Packet Stream Widget (Rocket / CanSat) ────────────────────────────────────

class PacketStreamPanel(QWidget):
    """One live packet stream panel with header and scrollable packet list."""

    packet_selected = None  # Signal-like callback: Callable[[PacketRecord], None]

    def __init__(self, vehicle_id: VehicleID, on_select: Callable[[PacketRecord], None], parent=None):
        super().__init__(parent)
        self.vehicle_id = vehicle_id
        self.on_select = on_select
        self.model = PacketListModel(vehicle_id, self)
        self._build()

    def _build(self) -> None:
        c = ThemeManager.C()
        self.setStyleSheet(
            f"background-color: {c.BG_CARD}; border: 1px solid {c.BORDER}; "
            f"border-radius: 6px;"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        # Panel Header - Moderate size, no emojis
        hdr = QHBoxLayout()
        title = QLabel(f"{self.vehicle_id.value} TELEMETRY STREAM")
        title.setFont(QFont("monospace", 8, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {c.CYAN};")
        hdr.addWidget(title)

        hdr.addStretch()

        self.pkt_counter = QLabel("0000 PKTS")
        self.pkt_counter.setFont(QFont("monospace", 8, QFont.Weight.Bold))
        self.pkt_counter.setStyleSheet(
            f"background-color: {c.BG_INPUT}; color: {c.TEXT_DIM}; "
            f"padding: 2px 8px; border-radius: 3px; border: 1px solid {c.BORDER};"
        )
        hdr.addWidget(self.pkt_counter)

        lay.addLayout(hdr)

        # Stacked Widget (0 = Empty State, 1 = Packet List)
        self.stack = QStackedWidget()

        # Empty State
        empty_w = QWidget()
        e_lay = QVBoxLayout(empty_w)
        e_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        e_lbl = QLabel(f"WAITING FOR {self.vehicle_id.value} TELEMETRY...")
        e_lbl.setFont(QFont("monospace", FS_CAPTION, QFont.Weight.Bold))
        e_lbl.setStyleSheet(f"color: {c.TEXT_MUTED};")
        e_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        e_lay.addWidget(e_lbl)
        self.stack.addWidget(empty_w)

        # Live List View
        self.list_view = QListView()
        self.list_view.setModel(self.model)
        self.list_view.setItemDelegate(PacketCardDelegate(self))
        self.list_view.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.list_view.setStyleSheet(
            f"QListView {{ background-color: {c.BG}; border: none; }} "
            f"QListView::item {{ border: none; padding: 0; }}"
        )
        self.list_view.clicked.connect(self._on_item_clicked)
        self.stack.addWidget(self.list_view)

        lay.addWidget(self.stack, stretch=1)

    def insert_packet(self, record: PacketRecord) -> None:
        was_empty = self.model.rowCount() == 0
        v_scroll = self.list_view.verticalScrollBar()
        was_at_top = v_scroll.value() == 0

        self.model.insert_packet(record)
        count = self.model.rowCount()
        self.pkt_counter.setText(f"{count:04d} PKTS")

        if was_empty:
            self.stack.setCurrentIndex(1)
            # Auto-select the first packet
            self.list_view.setCurrentIndex(self.model.index(0, 0))
            if self.on_select:
                self.on_select(record)
        elif was_at_top:
            v_scroll.setValue(0)
        else:
            v_scroll.setValue(v_scroll.value() + 36)

    def _on_item_clicked(self, index: QModelIndex) -> None:
        record = self.model.get_packet(index.row())
        if record and self.on_select:
            self.on_select(record)


# ── Raw & Decoded Packet Inspector ────────────────────────────────────────────

class PacketInspector(QWidget):
    """Displays raw wire packet and decoded field-by-field breakdown."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_packet: Optional[PacketRecord] = None
        self._build()

    def _build(self) -> None:
        c = ThemeManager.C()
        self.setStyleSheet(
            f"background-color: {c.BG_CARD}; border: 1px solid {c.BORDER}; "
            f"border-radius: 6px;"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        # Header
        hdr = QHBoxLayout()
        title = QLabel("PACKET INSPECTOR")
        title.setFont(QFont("monospace", 9, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {c.CYAN};")
        hdr.addWidget(title)

        self.sub_info = QLabel("SELECT A PACKET TO INSPECT")
        self.sub_info.setFont(QFont("monospace", 8))
        self.sub_info.setStyleSheet(f"color: {c.TEXT_DIM};")
        hdr.addWidget(self.sub_info)

        hdr.addStretch()

        btn_copy = QPushButton("Copy Raw")
        btn_copy.setFont(QFont("monospace", 8))
        btn_copy.setStyleSheet(
            f"QPushButton {{ background-color: {c.BG_INPUT}; color: {c.TEXT}; "
            f"border: 1px solid {c.BORDER}; border-radius: 3px; padding: 2px 8px; }} "
            f"QPushButton:hover {{ background-color: {c.BG_CARD_HOVER}; border-color: {c.CYAN}; }}"
        )
        btn_copy.clicked.connect(self._copy_raw)
        hdr.addWidget(btn_copy)

        lay.addLayout(hdr)

        # Raw Packet View Box
        self.raw_box = QTextEdit()
        self.raw_box.setReadOnly(True)
        self.raw_box.setFixedHeight(48)
        self.raw_box.setFont(QFont("monospace", 8))
        self.raw_box.setStyleSheet(
            f"background-color: {c.BG}; color: {c.TEXT}; "
            f"border: 1px solid {c.BORDER}; border-radius: 4px; padding: 4px;"
        )
        self.raw_box.setPlaceholderText("Raw wire frame ($T,...) will appear here...")
        lay.addWidget(self.raw_box)

        # Decoded Telemetry Table (Field | Value | Unit)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["TELEMETRY FIELD", "VALUE", "UNIT / SPEC"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setFont(QFont("monospace", FS_BODY))
        self.table.setStyleSheet(
            f"QTableWidget {{ background-color: {c.BG}; color: {c.TEXT}; "
            f"border: 1px solid {c.BORDER}; gridline-color: {c.BORDER}; }} "
            f"QHeaderView::section {{ background-color: {c.BG_INPUT}; color: {c.TEXT_DIM}; "
            f"font-family: monospace; font-size: 8pt; font-weight: bold; border: none; "
            f"border-bottom: 1px solid {c.BORDER}; padding: 3px 6px; }}"
        )
        lay.addWidget(self.table, stretch=1)

    def display_packet(self, record: PacketRecord) -> None:
        self._current_packet = record
        c = ThemeManager.C()

        # Update raw frame
        raw = record.raw_line or (
            f"$T,{record.team_id},{record.vehicle_code},{record.mission_time:.2f},"
            f"{record.packet_count},{record.flight_state_code},{record.altitude:.2f}*"
        )
        self.raw_box.setPlainText(raw)

        tag = " [SIMULATED]" if record.is_sim else ""
        self.sub_info.setText(
            f"{record.vehicle_name} PACKET #{record.packet_count:04d} "
            f"AT T+{record.mission_time:.2f}s{tag}"
        )

        # 19 Reference Specification Fields in exact order:
        accel_str = f"{record.accel:.2f} m/s²" if record.accel is not None else "NOT AVAILABLE"
        vel_str = f"{record.velocity:.2f} m/s" if record.velocity is not None else "NOT AVAILABLE"
        gnss_time_str = record.gnss_time if record.gnss_time and record.gnss_time != "—" else "NOT AVAILABLE"

        fields = [
            ("Prefix",                 record.prefix,                                record.prefix_desc),
            ("Team ID",                record.team_id,                               "Fixed format string"),
            ("Time Stamp",             f"{record.mission_time:.2f} s",               "Time from boot (seconds)"),
            ("Packet Count",           str(record.packet_count),                     "Packets sent during telemetry"),
            ("Altitude",               f"{record.altitude:.2f} m",                   "0.1 m resolution, corrected"),
            ("Pressure",               f"{record.pressure:.1f} Pa",                  "1 Pa resolution"),
            ("Temperature",            f"{record.temperature:.2f} °C",               "0.1 °C resolution"),
            ("Voltage",                f"{record.voltage:.2f} V",                    "0.01 V resolution"),
            ("GNSS Time",              gnss_time_str,                                "seconds / UTC"),
            ("GNSS Latitude",          f"{record.gnss_lat:.6f} deg",                 "0.0001 deg resolution"),
            ("GNSS Longitude",         f"{record.gnss_lon:.6f} deg",                 "0.0001 deg resolution"),
            ("GNSS Altitude",          f"{record.gnss_alt:.2f} m",                   "0.1 m resolution"),
            ("GNSS Satellites",        str(record.gnss_sats),                        "Satellite count"),
            ("Accelerometer",          accel_str,                                    "m/s² corrected"),
            ("Gyro Spin Rate",         f"{record.spin:.2f} deg/s",                   "deg/s spin rate"),
            ("Flight Software State",  f"{record.flight_state_code} — {record.flight_state_name}", "0..7 flight state"),
            ("Velocity",               vel_str,                                      "m/s velocity"),
            ("Vehicle",                f"{record.vehicle_code} — {record.vehicle_name}", "1-ROCKET, 2-CANSAT"),
            ("Checksum",               record.checksum,                              "Fixed format checksum"),
        ]

        # Additional metadata extras if present
        if record.rssi is not None:
            fields.append(("RSSI", f"{record.rssi:.1f} dBm", "Receiver signal strength"))
        if record.snr is not None:
            fields.append(("SNR", f"{record.snr:.1f} dB", "Receiver signal-to-noise ratio"))
        fields.append(("Telemetry Stream", "SIMULATED ([SIM])" if record.is_sim else "LIVE HARDWARE", "Telemetry data source"))

        self.table.setRowCount(len(fields))
        for row, (name, val, unit) in enumerate(fields):
            item_name = QTableWidgetItem(name)
            item_val = QTableWidgetItem(val)
            item_unit = QTableWidgetItem(unit)

            item_val.setFont(QFont("monospace", 8, QFont.Weight.Bold))
            item_val.setForeground(QColor(
                c.CYAN if name in ("Altitude", "Velocity", "Flight Software State", "Vehicle")
                else (c.GREEN if val not in ("NOT AVAILABLE", "ERR", "—") else c.TEXT_MUTED)
            ))
            item_unit.setForeground(QColor(c.TEXT_DIM))

            self.table.setItem(row, 0, item_name)
            self.table.setItem(row, 1, item_val)
            self.table.setItem(row, 2, item_unit)
            self.table.setRowHeight(row, 22)

    def _copy_raw(self) -> None:
        text = self.raw_box.toPlainText()
        if text:
            clipboard = QApplication.clipboard()
            if clipboard:
                clipboard.setText(text)


# ── Phase 03: Simulation Controller ───────────────────────────────────────────

class VehicleSimulationControl(QWidget):
    """Controls simulation generation for one vehicle."""

    def __init__(self, vehicle_id: VehicleID, on_line_ready: Callable[[str], None], parent=None):
        super().__init__(parent)
        self.vehicle_id = vehicle_id
        self.on_line_ready = on_line_ready

        self.profile = FlightProfile()
        self.flight = SimulatedFlight(vehicle_id, self.profile)
        self.codec = CsvCodec()

        self.t = 0.0
        self.running = False

        self._timer = QTimer(self)
        self._timer.setInterval(100)  # 10 Hz nominal telemetry rate
        self._timer.timeout.connect(self._step)

        self._build()

    def _build(self) -> None:
        c = ThemeManager.C()
        self.setStyleSheet(
            f"background-color: {c.BG_CARD}; border: 1px solid {c.BORDER}; "
            f"border-radius: 6px;"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)

        hdr = QHBoxLayout()
        lbl = QLabel(f"{self.vehicle_id.value} SIMULATION")
        lbl.setFont(QFont("monospace", 8, QFont.Weight.Bold))
        lbl.setStyleSheet(f"color: {c.CYAN};")
        hdr.addWidget(lbl)

        hdr.addStretch()

        self.status_lbl = QLabel("IDLE")
        self.status_lbl.setFont(QFont("monospace", 7, QFont.Weight.Bold))
        self.status_lbl.setStyleSheet(
            f"background-color: {c.BG_INPUT}; color: {c.TEXT_MUTED}; "
            f"padding: 1px 6px; border-radius: 3px;"
        )
        hdr.addWidget(self.status_lbl)

        lay.addLayout(hdr)

        # Time & State Readout
        self.info_lbl = QLabel("T+0.0s │ STATE: BOOT │ ALT: 0.0m")
        self.info_lbl.setFont(QFont("monospace", 8))
        self.info_lbl.setStyleSheet(f"color: {c.TEXT_DIM};")
        lay.addWidget(self.info_lbl)

        # Control Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        self.btn_start = QPushButton("Start")
        self.btn_pause = QPushButton("Pause")
        self.btn_reset = QPushButton("Reset")

        for b, col in ((self.btn_start, c.GREEN), (self.btn_pause, c.AMBER), (self.btn_reset, c.TEXT)):
            b.setFont(QFont("monospace", 8, QFont.Weight.Bold))
            b.setStyleSheet(
                f"QPushButton {{ background-color: {c.BG_INPUT}; color: {col}; "
                f"border: 1px solid {c.BORDER}; border-radius: 4px; padding: 4px 8px; }} "
                f"QPushButton:hover {{ background-color: {c.BG_CARD_HOVER}; border-color: {col}; }}"
            )
            btn_row.addWidget(b)

        self.btn_start.clicked.connect(self.start)
        self.btn_pause.clicked.connect(self.pause)
        self.btn_reset.clicked.connect(self.reset)

        lay.addLayout(btn_row)

    def start(self) -> None:
        c = ThemeManager.C()
        self.running = True
        self.status_lbl.setText("RUNNING")
        self.status_lbl.setStyleSheet(
            f"background-color: {c.BG_INPUT}; color: {c.GREEN}; "
            f"padding: 1px 6px; border-radius: 3px; font-weight: bold;"
        )
        self._timer.start()

    def pause(self) -> None:
        c = ThemeManager.C()
        self.running = False
        self.status_lbl.setText("PAUSED")
        self.status_lbl.setStyleSheet(
            f"background-color: {c.BG_INPUT}; color: {c.AMBER}; "
            f"padding: 1px 6px; border-radius: 3px; font-weight: bold;"
        )
        self._timer.stop()

    def reset(self) -> None:
        c = ThemeManager.C()
        self._timer.stop()
        self.running = False
        self.t = 0.0
        self.flight = SimulatedFlight(self.vehicle_id, self.profile)
        self.status_lbl.setText("IDLE")
        self.status_lbl.setStyleSheet(
            f"background-color: {c.BG_INPUT}; color: {c.TEXT_MUTED}; "
            f"padding: 1px 6px; border-radius: 3px;"
        )
        self.info_lbl.setText("T+0.0s │ STATE: BOOT │ ALT: 0.0m")

    def _step(self) -> None:
        self.t += 0.1
        values = self.flight.values(self.t)
        base = self.codec.encode(values)
        # Append simulated marker SIM=1 so it is clearly identified downstream
        line = base + f",RSSI={self.flight.rssi(self.t):.1f},SNR=8.5,SIM=1\n"

        st = self.flight.state_at(self.t).value
        alt = self.flight.altitude_at(self.t)
        self.info_lbl.setText(f"T+{self.t:.1f}s │ STATE: {st} │ ALT: {alt:.1f}m")

        if self.on_line_ready:
            self.on_line_ready(line)


# ── COM Page Shell ────────────────────────────────────────────────────────────

class ComPage(QWidget):
    """Aerospace Mission Control COM / Telemetry Communication Console."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._supervisor = None
        self._inject_cb: Optional[Callable[[str], None]] = None

        self._total_packets = 0
        self._valid_packets = 0
        self._rejected_packets = 0
        self._packet_timestamps: Deque[float] = deque(maxlen=100)
        self._last_packet_time: Optional[float] = None
        self._source_mode = "LIVE"

        self._build()

    # ── Construction ─────────────────────────────────────────────────────

    def _build(self) -> None:
        c = ThemeManager.C()
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(8)

        # Header Title
        main_layout.addLayout(self._build_header())

        # Phase 01: Link Status Bar
        main_layout.addWidget(self._build_link_status_bar())

        # Phase 02: Main Splitter (Left: Half-Height Streams + Simulation | Right: Inspector)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left Column: Dual Stream Screens (Half Height) + Simulation Controls (Bottom)
        left_panel = QWidget()
        left_lay = QVBoxLayout(left_panel)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(6)

        streams_widget = QWidget()
        streams_lay = QHBoxLayout(streams_widget)
        streams_lay.setContentsMargins(0, 0, 0, 0)
        streams_lay.setSpacing(6)

        self.rocket_stream = PacketStreamPanel(VehicleID.ROCKET, self._on_packet_selected)
        self.cansat_stream = PacketStreamPanel(VehicleID.CANSAT, self._on_packet_selected)
        streams_lay.addWidget(self.rocket_stream)
        streams_lay.addWidget(self.cansat_stream)
        left_lay.addWidget(streams_widget, stretch=1)

        # Simulation Controls underneath streams on left
        sim_box = QWidget()
        sim_lay = QHBoxLayout(sim_box)
        sim_lay.setContentsMargins(0, 0, 0, 0)
        sim_lay.setSpacing(6)

        self.sim_rocket = VehicleSimulationControl(VehicleID.ROCKET, self._on_sim_line)
        self.sim_cansat = VehicleSimulationControl(VehicleID.CANSAT, self._on_sim_line)
        sim_lay.addWidget(self.sim_rocket)
        sim_lay.addWidget(self.sim_cansat)
        left_lay.addWidget(sim_box)

        splitter.addWidget(left_panel)

        # Right Column: Full-Height Packet Inspector
        self.inspector = PacketInspector()
        splitter.addWidget(self.inspector)

        splitter.setSizes([600, 800])
        main_layout.addWidget(splitter, stretch=1)

        # Bottom: Communication Metrics Bar
        main_layout.addWidget(self._build_metrics_bar())

    def _build_header(self) -> QHBoxLayout:
        c = ThemeManager.C()
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        title = QLabel("COMMUNICATION / TELEMETRY MONITOR")
        title.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {c.CYAN};")
        row.addWidget(title)

        row.addStretch()

        self.dev_badge = QLabel("DEV")
        self.dev_badge.setFont(QFont("monospace", 8, QFont.Weight.Bold))
        self.dev_badge.setStyleSheet(
            f"background-color: {c.BG_INPUT}; color: {c.CYAN}; "
            f"border: 1px solid {c.BORDER}; border-radius: 4px; padding: 3px 8px;"
        )
        row.addWidget(self.dev_badge)

        self.mode_badge = QLabel(" MODE: LIVE ")
        self.mode_badge.setFont(QFont("monospace", 8, QFont.Weight.Bold))
        self.mode_badge.setStyleSheet(
            f"background-color: {c.BG_INPUT}; color: {c.GREEN}; "
            f"border: 1px solid {c.GREEN}; border-radius: 4px; padding: 3px 10px;"
        )
        row.addWidget(self.mode_badge)

        return row

    def _build_link_status_bar(self) -> QWidget:
        """Phase 01: Top communication status cards (§5)."""
        c = ThemeManager.C()
        bar = QFrame()
        bar.setFixedHeight(46)
        bar.setStyleSheet(
            f"background-color: {c.BG_PANEL}; border: 1px solid {c.BORDER}; "
            f"border-radius: 6px;"
        )

        grid = QHBoxLayout(bar)
        grid.setContentsMargins(12, 4, 12, 4)
        grid.setSpacing(10)

        # Status
        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet(f"color: {c.TEXT_MUTED}; font-size: 14px;")
        grid.addWidget(self.status_dot)

        self.status_lbl = QLabel("DISCONNECTED")
        self.status_lbl.setFont(QFont("monospace", 8, QFont.Weight.Bold))
        self.status_lbl.setStyleSheet(f"color: {c.TEXT_MUTED};")
        self.status_lbl.setFixedWidth(110)
        grid.addWidget(self.status_lbl)

        grid.addWidget(self._vdivider())

        # COM Port
        grid.addWidget(self._stat_label("PORT:"))
        self.port_val = QLabel("—")
        self.port_val.setFont(QFont("monospace", 8, QFont.Weight.Bold))
        self.port_val.setStyleSheet(f"color: {c.CYAN};")
        self.port_val.setFixedWidth(80)
        grid.addWidget(self.port_val)

        grid.addWidget(self._vdivider())

        # Baud Rate
        grid.addWidget(self._stat_label("BAUD:"))
        self.baud_val = QLabel("115200")
        self.baud_val.setFont(QFont("monospace", 8, QFont.Weight.Bold))
        self.baud_val.setStyleSheet(f"color: {c.TEXT};")
        self.baud_val.setFixedWidth(65)
        grid.addWidget(self.baud_val)

        grid.addWidget(self._vdivider())

        # Packet Link
        grid.addWidget(self._stat_label("LINK:"))
        self.link_val = QLabel("STANDBY")
        self.link_val.setFont(QFont("monospace", 8, QFont.Weight.Bold))
        self.link_val.setStyleSheet(f"color: {c.TEXT_DIM};")
        self.link_val.setFixedWidth(70)
        grid.addWidget(self.link_val)

        grid.addWidget(self._vdivider())

        # Total Packets
        grid.addWidget(self._stat_label("PACKETS:"))
        self.total_pkts_val = QLabel("0000")
        self.total_pkts_val.setFont(QFont("monospace", 8, QFont.Weight.Bold))
        self.total_pkts_val.setStyleSheet(f"color: {c.TEXT};")
        self.total_pkts_val.setFixedWidth(55)
        grid.addWidget(self.total_pkts_val)

        grid.addWidget(self._vdivider())

        # Last Packet Timestamp
        grid.addWidget(self._stat_label("LAST PKT:"))
        self.last_pkt_val = QLabel("00:00:00")
        self.last_pkt_val.setFont(QFont("monospace", 8))
        self.last_pkt_val.setStyleSheet(f"color: {c.TEXT_DIM};")
        self.last_pkt_val.setFixedWidth(160)
        grid.addWidget(self.last_pkt_val)

        grid.addStretch()
        return bar

    def _build_metrics_bar(self) -> QWidget:
        """Bottom communication metrics section (§20) with fixed widths to prevent jitter/shaking."""
        c = ThemeManager.C()
        bar = QFrame()
        bar.setFixedHeight(34)
        bar.setStyleSheet(
            f"background-color: {c.BG_PANEL}; border: 1px solid {c.BORDER}; "
            f"border-radius: 4px;"
        )

        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 2, 12, 2)
        row.setSpacing(10)

        # Lift
        row.addWidget(self._stat_label("LIFT:"))
        self.lift_val = QLabel("STANDBY")
        self.lift_val.setFont(QFont("monospace", 8, QFont.Weight.Bold))
        self.lift_val.setStyleSheet(f"color: {c.CYAN};")
        self.lift_val.setFixedWidth(85)
        row.addWidget(self.lift_val)

        row.addWidget(self._vdivider())

        # Rate
        row.addWidget(self._stat_label("RATE:"))
        self.rate_val = QLabel("0.0 pkts/s")
        self.rate_val.setFont(QFont("monospace", 8, QFont.Weight.Bold))
        self.rate_val.setStyleSheet(f"color: {c.TEXT};")
        self.rate_val.setFixedWidth(95)
        row.addWidget(self.rate_val)

        row.addWidget(self._vdivider())

        # Valid
        row.addWidget(self._stat_label("VALID:"))
        self.valid_val = QLabel("0")
        self.valid_val.setFont(QFont("monospace", 8, QFont.Weight.Bold))
        self.valid_val.setStyleSheet(f"color: {c.GREEN};")
        self.valid_val.setFixedWidth(65)
        row.addWidget(self.valid_val)

        row.addWidget(self._vdivider())

        # Rejected
        row.addWidget(self._stat_label("REJECTED:"))
        self.reject_val = QLabel("0")
        self.reject_val.setFont(QFont("monospace", 8, QFont.Weight.Bold))
        self.reject_val.setStyleSheet(f"color: {c.RED};")
        self.reject_val.setFixedWidth(50)
        row.addWidget(self.reject_val)

        row.addWidget(self._vdivider())

        # Success Rate
        row.addWidget(self._stat_label("SUCCESS:"))
        self.success_val = QLabel("100.0%")
        self.success_val.setFont(QFont("monospace", 8, QFont.Weight.Bold))
        self.success_val.setStyleSheet(f"color: {c.TEXT};")
        self.success_val.setFixedWidth(65)
        row.addWidget(self.success_val)

        row.addStretch()
        return bar

    def _stat_label(self, text: str) -> QLabel:
        c = ThemeManager.C()
        lbl = QLabel(text)
        lbl.setFont(QFont("monospace", 7, QFont.Weight.Bold))
        lbl.setStyleSheet(f"color: {c.TEXT_DIM};")
        return lbl

    def _vdivider(self) -> QFrame:
        c = ThemeManager.C()
        f = QFrame()
        f.setFrameShape(QFrame.Shape.VLine)
        f.setStyleSheet(f"color: {c.BORDER};")
        return f

    # ── Ingestion & Wiring ───────────────────────────────────────────────

    def receive(self, packet: TelemetryPacket, result: IngestResult,
                derived_v: Optional[float] = None) -> None:
        """Receive live telemetry packet from the shared pipeline."""
        now = time.monotonic()
        self._total_packets += 1
        self._valid_packets += 1
        self._packet_timestamps.append(now)
        self._last_packet_time = now

        record = PacketRecord(packet, derived_v, is_valid=True)

        if packet.vehicle_id is VehicleID.ROCKET:
            self.rocket_stream.insert_packet(record)
        else:
            self.cansat_stream.insert_packet(record)

        # Update counters and metrics
        self._update_metrics(packet)

    def on_rejected_line(self, reason: str, detail: str, line: str) -> None:
        self._rejected_packets += 1
        self._update_metrics()

    def set_supervisor(self, supervisor) -> None:
        self._supervisor = supervisor
        self._update_connection_info()

    def set_source_state(self, state: SourceState) -> None:
        c = ThemeManager.C()
        colour = {
            SourceState.CONNECTED: c.GREEN,
            SourceState.CONNECTING: c.AMBER,
            SourceState.NO_DATA: c.AMBER,
            SourceState.LINK_ERROR: c.RED,
            SourceState.DISCONNECTED: c.TEXT_MUTED,
        }.get(state, c.TEXT_MUTED)

        self.status_dot.setStyleSheet(f"color: {colour}; font-size: 14px;")
        self.status_lbl.setText(state.value)
        self.status_lbl.setStyleSheet(f"color: {colour}; font-weight: bold;")
        self.link_val.setText("ACTIVE" if state is SourceState.CONNECTED else "IDLE")
        self.link_val.setStyleSheet(f"color: {colour};")
        self._update_connection_info()

    def set_inject_callback(self, cb: Callable[[str], None]) -> None:
        self._inject_cb = cb

    def _on_sim_line(self, line: str) -> None:
        """Feed simulated line into the shared supervisor pipeline."""
        c = ThemeManager.C()
        self.mode_badge.setText(" MODE: SIMULATION ")
        self.mode_badge.setStyleSheet(
            f"background-color: {c.BG_INPUT}; color: #8b5cf6; "
            f"border: 1px solid #8b5cf6; border-radius: 4px; padding: 3px 10px;"
        )

        if self._supervisor is not None and hasattr(self._supervisor, "inject_line"):
            self._supervisor.inject_line(line)
        elif self._inject_cb:
            self._inject_cb(line)

    def _on_packet_selected(self, record: PacketRecord) -> None:
        self.inspector.display_packet(record)

    def _update_connection_info(self) -> None:
        if self._supervisor is not None:
            port = getattr(self._supervisor, "port_name", "—")
            baud = getattr(self._supervisor, "baudrate", 115200)
            self.port_val.setText(str(port))
            self.baud_val.setText(str(baud))
            if port == "SIMULATOR":
                self.mode_badge.setText(" MODE: SIMULATOR ")

    def _update_metrics(self, packet: Optional[TelemetryPacket] = None) -> None:
        self.total_pkts_val.setText(f"{self._total_packets:04d}")
        self.valid_val.setText(str(self._valid_packets))
        self.reject_val.setText(str(self._rejected_packets))

        if packet:
            self.last_pkt_val.setText(
                f"T+{packet.mission_time:.1f}s ({packet.gnss_time})" if packet.gnss_time else f"T+{packet.mission_time:.1f}s"
            )
            if hasattr(self, "lift_val") and packet.state:
                st_name = packet.state.value
                self.lift_val.setText(st_name)
                c = ThemeManager.C()
                self.lift_val.setStyleSheet(
                    f"color: {c.CYAN if st_name in ('BOOST', 'APOGEE') else (c.GREEN if st_name in ('LANDING', 'RECOVERY') else c.TEXT)}; font-weight: bold;"
                )

        # Rolling packet rate
        if len(self._packet_timestamps) > 1:
            dt = self._packet_timestamps[-1] - self._packet_timestamps[0]
            if dt > 0:
                rate = (len(self._packet_timestamps) - 1) / dt
                self.rate_val.setText(f"{rate:.1f} pkts/s")

        total = self._valid_packets + self._rejected_packets
        if total > 0:
            success = (self._valid_packets / total) * 100.0
            self.success_val.setText(f"{success:.1f}%")
